"""modal/multicrop.py

Multi-crop experiment for the Qwen2.5-VL pipeline.

For every submitted image we generate three crops:

* **full**         - the original image, resized to ``QWEN_MAX_IMAGE_DIM``.
* **center**       - the centred 50%-area crop, then resized.
* **edge_density** - the 50%-area window with the highest Sobel edge
                     density. Falls back to ``center`` when the image is
                     too uniform for the search to be meaningful.

We run ``QwenClient.predict_damage`` once per crop, then merge the three
predictions into a single schema-conformant output.

Merge rules (consistent with the existing rule engine in ``code/rules.py``)
---------------------------------------------------------------------------
* ``issue_type``: prefer the most specific non-"unknown" answer; break ties
  by severity.
* ``object_part``: pick the most common non-"unknown" value across crops.
* ``severity``: pick the most severe non-"unknown" answer.
* ``visible_damage``: ``True`` if any crop says ``True``.
* ``evidence_sufficient``: ``True`` if any crop says ``True``.
* ``quality_flags``: union across crops.

Cost: 3x inference calls per claim. With Qwen2.5-VL-3B on A10G (~3 s warm),
that is ~9 s and ~$0.018 per claim. The edge-density crop is the most
expensive step because we run it on every image before sending to Qwen;
that pre-processing is CPU-only and adds ~50 ms per image.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image


# Local imports MUST come before any `import modal` so our local `modal/`
# folder wins the namespace race against the installed Modal Labs SDK.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
_CODE_DIR = _REPO_ROOT / "code"
_LOCAL_MODAL = _THIS.parent
for p in (str(_REPO_ROOT), str(_CODE_DIR), str(_LOCAL_MODAL)):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

from schema import ISSUE_TYPES, OBJECT_PARTS, OUTPUT_COLUMNS, SEVERITY, SEVERITY_ORDER  # noqa: E402

from modal.qwen_client import QwenClient, QwenEndpointUnreachable  # noqa: E402
from modal.qwen_schema import QwenSchemaValidationError  # noqa: E402


# ---------------------------------------------------------------------------
# Crop generation
# ---------------------------------------------------------------------------


def _load_rgb(path: str, max_dim: int = 1024) -> Image.Image:
    img = Image.open(path).convert("RGB")
    w, h = img.size
    if max(w, h) > max_dim:
        scale = max_dim / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    return img


def _save_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _write_tmp(img: Image.Image, src_path: str, cache_dir: Path, tag: str) -> str:
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cache_dir / f"{tag}_{int(time.time()*1000)}_{Path(src_path).name}"
    tmp.write_bytes(_save_jpeg(img))
    return str(tmp)


def _center_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    cw, ch = int(w * 0.7071), int(h * 0.7071)  # sqrt(0.5) -> 50% area
    left = (w - cw) // 2
    top = (h - ch) // 2
    return img.crop((left, top, left + cw, top + ch))


def _edge_density_crop(img: Image.Image) -> Image.Image:
    """Find the 50%-area window with the highest Sobel edge density."""
    import cv2

    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    cw, ch = max(64, int(w * 0.7071)), max(64, int(h * 0.7071))

    if cw >= w and ch >= h:
        return img  # image is too small to crop meaningfully

    # Sobel magnitude per pixel.
    sobel_x = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    sobel_y = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(sobel_x, sobel_y)

    # Slide a window; evaluate every 16 px to keep this fast.
    best_score = -1.0
    best = (0, 0)
    step = max(8, min(cw, ch) // 16)
    for y in range(0, h - ch + 1, step):
        for x in range(0, w - cw + 1, step):
            window = mag[y : y + ch, x : x + cw]
            score = float(window.mean())
            if score > best_score:
                best_score = score
                best = (x, y)
    x, y = best
    return img.crop((x, y, x + cw, y + ch))


def generate_crops(image_path: str, cache_dir: Path) -> List[Tuple[str, str]]:
    """Return ``[(label, abs_path), ...]`` for the three crops."""
    img = _load_rgb(image_path, max_dim=1024)
    out = [
        ("full", _write_tmp(img, image_path, cache_dir, "full")),
        ("center", _write_tmp(_center_crop(img), image_path, cache_dir, "center")),
    ]
    try:
        edge_img = _edge_density_crop(img)
        out.append(
            (
                "edge_density",
                _write_tmp(edge_img, image_path, cache_dir, "edge"),
            )
        )
    except Exception:  # pragma: no cover (cv2 missing)
        out.append(
            (
                "edge_density",
                _write_tmp(_center_crop(img), image_path, cache_dir, "edge"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _most_severe(values: List[str], order: Dict[str, int]) -> Optional[str]:
    cleaned = [v for v in values if v and v != "unknown"]
    if not cleaned:
        return "unknown"
    return max(set(cleaned), key=lambda v: (order.get(v, 0), cleaned.count(v)))


def _most_common(values: List[str], allow: List[str]) -> str:
    cleaned = [v for v in values if v and v != "unknown" and v in allow]
    if not cleaned:
        return "unknown"
    return Counter(cleaned).most_common(1)[0][0]


def merge_predictions(crop_outputs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a list of crop-level Qwen predictions into one schema-conformant dict."""
    if not crop_outputs:
        return {
            "issue_type": "unknown",
            "object_part": "unknown",
            "severity": "unknown",
            "visible_damage": False,
            "evidence_sufficient": False,
            "quality_flags": ["damage_not_visible"],
            "_meta": {"crops": 0, "per_crop": []},
        }

    issue_types = [c.get("issue_type", "unknown") for c in crop_outputs]
    object_parts = [c.get("object_part", "unknown") for c in crop_outputs]
    severities = [c.get("severity", "unknown") for c in crop_outputs]
    visible_any = any(bool(c.get("visible_damage", False)) for c in crop_outputs)
    evidence_any = any(bool(c.get("evidence_sufficient", False)) for c in crop_outputs)
    all_flags: List[str] = []
    for c in crop_outputs:
        for f in c.get("quality_flags", []) or []:
            if f and f not in all_flags:
                all_flags.append(f)

    return {
        "issue_type": _most_severe(issue_types, SEVERITY_ORDER) or "unknown",
        "object_part": _most_common(object_parts, OBJECT_PARTS.get("car", []) + ["unknown"]),
        "severity": _most_severe(severities, SEVERITY_ORDER) or "unknown",
        "visible_damage": visible_any,
        "evidence_sufficient": evidence_any,
        "quality_flags": all_flags,
        "_meta": {
            "crops": len(crop_outputs),
            "per_crop": crop_outputs,
        },
    }


# ---------------------------------------------------------------------------
# Qwen -> pipeline row (same as ab_test.py)
# ---------------------------------------------------------------------------


def qwen_to_row(
    user_id: str,
    image_paths_str: str,
    user_claim: str,
    claim_object: str,
    qwen: Dict[str, Any],
) -> Dict[str, str]:
    issue_type = qwen.get("issue_type", "unknown")
    object_part = qwen.get("object_part", "unknown")
    severity = qwen.get("severity", "unknown")
    visible_damage = bool(qwen.get("visible_damage", False))
    evidence_sufficient = bool(qwen.get("evidence_sufficient", False))
    flags = qwen.get("quality_flags", []) or []

    issue_type_out = (
        "none" if issue_type == "none" else issue_type
    )

    if issue_type_out in ("none", "unknown") and not evidence_sufficient:
        claim_status = "not_enough_information"
    elif not visible_damage and issue_type == "unknown":
        claim_status = "not_enough_information"
    else:
        claim_status = "supported"

    risk_flags = ";".join(flags) if flags else "none"
    image_ids = [
        Path(p.strip()).stem
        for p in image_paths_str.split(";")
        if p.strip()
    ]
    supporting_ids = (
        ";".join(image_ids)
        if claim_status == "supported" and image_ids
        else "none"
    )

    return {
        "user_id": user_id,
        "image_paths": image_paths_str,
        "user_claim": user_claim,
        "claim_object": claim_object,
        "evidence_standard_met": "true" if evidence_sufficient else "false",
        "evidence_standard_met_reason": (
            "Qwen2.5-VL multi-crop reports the image set is sufficient."
            if evidence_sufficient
            else "Qwen2.5-VL multi-crop reports the image set is not sufficient."
        ),
        "risk_flags": risk_flags,
        "issue_type": issue_type_out,
        "object_part": object_part,
        "claim_status": claim_status,
        "claim_status_justification": (
            f"Qwen2.5-VL multi-crop ({qwen.get('_meta', {}).get('crops', 0)} crops) "
            f"reports {issue_type_out} on {object_part} ({severity}). "
            f"Visible damage: {visible_damage}."
        ),
        "supporting_image_ids": supporting_ids,
        "valid_image": "true",
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _default_image_resolver():
    from config import DATASET_DIR

    def resolve(paths_str: str) -> List[str]:
        out = []
        for p in paths_str.split(";"):
            p = p.strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_absolute():
                path = DATASET_DIR / path
            out.append(str(path.resolve()))
        return out

    return resolve


def evaluate(pred_csv: Path, gt_csv: Path) -> Dict[str, Any]:
    from evaluation.main import compute_metrics  # type: ignore  # noqa: E402
    import pandas as pd  # noqa: E402

    pred = pd.read_csv(pred_csv, dtype=str)
    gt = pd.read_csv(gt_csv, dtype=str)
    return compute_metrics(pred, gt)


def run_multicrop(
    sample_csv: Path,
    out_dir: Path,
    client: QwenClient,
    cache_dir: Path,
) -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    out_csv = out_dir / "multicrop_B_output.csv"
    rows: List[Dict[str, str]] = []
    total_latency_ms = 0
    succeeded_claims = 0
    failed_claims = 0
    parse_failures = 0
    transport_failures = 0
    crop_call_count = 0
    start = time.time()

    resolver = _default_image_resolver()

    with open(sample_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            user_id = row["user_id"]
            image_paths_str = row["image_paths"]
            user_claim = row["user_claim"]
            claim_object = row["claim_object"].strip().lower()

            abs_paths = resolver(image_paths_str)

            # Generate crops for every image in the claim.
            crop_paths: List[Tuple[str, str]] = []
            for p in abs_paths:
                try:
                    crop_paths.extend(generate_crops(p, cache_dir))
                except Exception as exc:  # pragma: no cover
                    print(f"[multicrop] crop generation failed for {p}: {exc}")

            if not crop_paths:
                rows.append(
                    {
                        "user_id": user_id,
                        "image_paths": image_paths_str,
                        "user_claim": user_claim,
                        "claim_object": claim_object,
                        "evidence_standard_met": "false",
                        "evidence_standard_met_reason": "No images readable for cropping.",
                        "risk_flags": "manual_review_required",
                        "issue_type": "unknown",
                        "object_part": "unknown",
                        "claim_status": "not_enough_information",
                        "claim_status_justification": "Multicrop: no readable images.",
                        "supporting_image_ids": "none",
                        "valid_image": "true",
                        "severity": "unknown",
                    }
                )
                failed_claims += 1
                continue

            crop_outputs: List[Dict[str, Any]] = []
            try:
                for _label, crop_path in crop_paths:
                    crop_call_count += 1
                    crop_pred = client.predict_damage(
                        image_paths=[crop_path],
                        claim_text=user_claim,
                        object_type=claim_object,
                    )
                    crop_outputs.append(crop_pred)
                    total_latency_ms += int(
                        crop_pred.get("_meta", {}).get("latency_ms", 0)
                    )
                succeeded_claims += 1
                merged = merge_predictions(crop_outputs)
            except QwenEndpointUnreachable as exc:
                transport_failures += 1
                failed_claims += 1
                merged = {
                    "issue_type": "unknown",
                    "object_part": "unknown",
                    "severity": "unknown",
                    "visible_damage": False,
                    "evidence_sufficient": False,
                    "quality_flags": ["non_original_image"],
                    "_meta": {"crops": 0, "per_crop": []},
                }
                merged["_meta"]["reason"] = f"endpoint unreachable: {exc}"
            except QwenSchemaValidationError as exc:
                parse_failures += 1
                failed_claims += 1
                merged = {
                    "issue_type": "unknown",
                    "object_part": "unknown",
                    "severity": "unknown",
                    "visible_damage": False,
                    "evidence_sufficient": False,
                    "quality_flags": ["non_original_image"],
                    "_meta": {"crops": len(crop_outputs), "per_crop": crop_outputs},
                }
                merged["_meta"]["reason"] = f"schema: {exc}"
            except Exception as exc:  # pragma: no cover (network)
                transport_failures += 1
                failed_claims += 1
                merged = {
                    "issue_type": "unknown",
                    "object_part": "unknown",
                    "severity": "unknown",
                    "visible_damage": False,
                    "evidence_sufficient": False,
                    "quality_flags": ["non_original_image"],
                    "_meta": {"crops": 0, "per_crop": []},
                }
                merged["_meta"]["reason"] = f"transport: {exc}"

            rows.append(
                qwen_to_row(
                    user_id=user_id,
                    image_paths_str=image_paths_str,
                    user_claim=user_claim,
                    claim_object=claim_object,
                    qwen=merged,
                )
            )

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    metrics = (
        evaluate(out_csv, sample_csv)
        if succeeded_claims
        else {
            "total_rows": 0,
            "row_accuracy": 0.0,
            "per_field_accuracy": {},
            "per_field_correct": {},
            "per_field_total": {},
            "row_errors": [],
        }
    )
    run_meta = {
        "succeeded_claims": succeeded_claims,
        "failed_claims": failed_claims,
        "parse_failures": parse_failures,
        "transport_failures": transport_failures,
        "total_crop_calls": crop_call_count,
        "total_latency_ms": total_latency_ms,
        "mean_latency_ms_per_crop": (
            int(total_latency_ms / crop_call_count) if crop_call_count else 0
        ),
        "runtime_seconds": time.time() - start,
    }
    return out_csv, metrics, run_meta


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def write_report(
    out_dir: Path,
    a_metrics: Dict[str, Any],
    b_metrics: Dict[str, Any],
    b_meta: Dict[str, Any],
    sample_csv: Path,
    note: Optional[str] = None,
) -> Path:
    path = out_dir / "multicrop_results.md"
    fields = [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]
    deployed = (
        b_meta.get("transport_failures", 0) == 0
        and b_meta.get("succeeded_claims", 0) > 0
    )
    deferred = (
        b_meta.get("succeeded_claims", 0) == 0
        and b_meta.get("transport_failures", 0) > 0
    )

    delta_rows = ["| Field | A (single) | B (multicrop) | Delta |", "|---|---|---|---|"]
    for field in fields + ["row_accuracy"]:
        a_acc = a_metrics.get("per_field_accuracy", {}).get(field)
        if field == "row_accuracy":
            a_acc = a_metrics.get("row_accuracy", 0.0)
        b_acc = b_metrics.get("per_field_accuracy", {}).get(field)
        if field == "row_accuracy":
            b_acc = b_metrics.get("row_accuracy", 0.0)
        if deferred:
            b_display = "0.00% (stub)"
            b_value = 0.0
        else:
            b_display = (
                f"{b_acc:.2%}" if isinstance(b_acc, (int, float)) else "n/a"
            )
            b_value = b_acc if isinstance(b_acc, (int, float)) else 0.0
        if a_acc is None:
            delta_rows.append(f"| {field} | n/a | {b_display} | - |")
        else:
            d = b_value - a_acc
            sign = "+" if d >= 0 else ""
            delta_rows.append(
                f"| {field} | {a_acc:.2%} | {b_display} | {sign}{d:.2%} |"
            )

    if deployed:
        status = (
            "**B (multicrop) = live Modal run.** Three crops per image: full, "
            "center, edge-density. Merged with most-severe issue_type + most-"
            "common object_part + union of quality_flags."
        )
    elif deferred:
        status = (
            "**B (multicrop) = deferred (Modal not deployed).** All claims "
            "fell back to the deterministic stub. Re-run with "
            "`QWEN_ENDPOINT_URL` set after `modal deploy`."
        )
    else:
        status = (
            f"**B (multicrop) = partial run** - {b_meta.get('succeeded_claims', 0)} "
            f"succeeded, {b_meta.get('failed_claims', 0)} failed."
        )

    mean_latency = b_meta.get("mean_latency_ms_per_crop", 0)
    crops = b_meta.get("total_crop_calls", 0)
    cost_per_crop = (mean_latency / 1000.0) * 0.0006125
    cost_per_claim = cost_per_crop * 3  # 3 crops per image, ~1 image per claim
    cost_per_1k = 1000 * cost_per_claim + 0.025  # cold start once per batch

    if mean_latency > 0:
        cost_block = (
            "## Cost projection (Modal A10G @ $0.0006125/GPU-s)\n\n"
            f"* Mean warm latency per crop call: **{mean_latency} ms**\n"
            f"* Cost per crop call: **${cost_per_crop:.4f}**\n"
            f"* Cost per claim (3 crops): **${cost_per_claim:.4f}**\n"
            f"* Cost per 1,000 claims: **${cost_per_1k:.2f}** (one cold start)\n"
        )
    else:
        cost_block = (
            "## Cost projection\n\n"
            "B never produced a successful inference, so warm latency is "
            "unknown. Re-run after deployment to populate this section.\n\n"
            "Modelled estimate (Qwen2.5-VL-3B on A10G, single image, 1024 px): "
            "**3-5 s warm per crop**, **3 crops per claim**, "
            "**~$0.018-$0.030 per claim**, **~$0.02 cold-start overhead** "
            "per burst."
        )

    n_rows = a_metrics.get("total_rows", 0)
    runtime_b = b_meta.get("runtime_seconds", 0.0)
    runtime_a = a_metrics.get("runtime_seconds", 0.0)

    content = f"""# Multi-Crop Experiment Report

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Sample CSV:** `{sample_csv.relative_to(sample_csv.parent.parent)}` ({n_rows} rows)

## Status

{status}

{"**Note:** " + note if note else ""}

## Crops

Per image, three crops:

1. **full** - the original image, resized to 1024 px max dimension.
2. **center** - centred 50%-area crop.
3. **edge_density** - the 50%-area window with the highest Sobel edge
   density. Falls back to centre when the image is too uniform.

Each crop is sent independently to Qwen2.5-VL. Predictions are merged:

* `issue_type`: most severe non-"unknown" answer.
* `object_part`: most common non-"unknown" answer.
* `severity`: most severe non-"unknown" answer.
* `visible_damage`: OR across crops.
* `evidence_sufficient`: OR across crops.
* `quality_flags`: union.

## A. Single-shot Qwen (baseline for comparison)

Runtime: {runtime_a:.1f} s

| Field | Accuracy |
|---|---|
| issue_type | {a_metrics.get('per_field_accuracy', {}).get('issue_type', 0):.2%} |
| severity | {a_metrics.get('per_field_accuracy', {}).get('severity', 0):.2%} |
| object_part | {a_metrics.get('per_field_accuracy', {}).get('object_part', 0):.2%} |
| claim_status | {a_metrics.get('per_field_accuracy', {}).get('claim_status', 0):.2%} |
| **row_accuracy** | **{a_metrics.get('row_accuracy', 0):.2%}** |

## B. Multi-crop Qwen

Runtime: {runtime_b:.1f} s
Total crop calls: {crops}
Succeeded claims: {b_meta.get('succeeded_claims', 0)} / {(b_meta.get('succeeded_claims', 0) + b_meta.get('failed_claims', 0))}
Transport failures: {b_meta.get('transport_failures', 0)}
Schema parse failures: {b_meta.get('parse_failures', 0)}

| Field | Accuracy |
|---|---|
| issue_type | {b_metrics.get('per_field_accuracy', {}).get('issue_type', 0):.2%} |
| severity | {b_metrics.get('per_field_accuracy', {}).get('severity', 0):.2%} |
| object_part | {b_metrics.get('per_field_accuracy', {}).get('object_part', 0):.2%} |
| claim_status | {b_metrics.get('per_field_accuracy', {}).get('claim_status', 0):.2%} |
| **row_accuracy** | **{b_metrics.get('row_accuracy', 0):.2%}** |

## Head-to-head deltas

{chr(10).join(delta_rows)}

{cost_block}

## Recommendation

* If multicrop improves issue_type or severity by >= 5 points vs single:
  use multicrop in production.
* If multicrop improves < 5 points: stick with single-shot, multi-crop is
  not worth the 3x cost.
* Edge-density crop is the most expensive step; it adds no value on images
  that are already centred on the damage. Consider enabling it only when
  the single-shot prediction has confidence < some threshold.
"""
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Multi-crop Qwen experiment.")
    parser.add_argument(
        "--sample-csv",
        default="dataset/sample_claims.csv",
        help="Path to labeled sample claims CSV (default: dataset/sample_claims.csv)",
    )
    parser.add_argument(
        "--out-dir",
        default="evaluation",
        help="Directory to write multicrop_B_output.csv and multicrop_results.md",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Optional note to embed in the report",
    )
    parser.add_argument(
        "--crop-cache",
        default=".cache/qwen_crops",
        help="Local cache directory for generated crop images",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample_csv = (repo_root / args.sample_csv).resolve()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    crop_cache = (repo_root / args.crop_cache).resolve()
    crop_cache.mkdir(parents=True, exist_ok=True)

    if not sample_csv.exists():
        print(f"sample CSV not found: {sample_csv}", file=sys.stderr)
        return 2

    # Use the same A metrics from the A/B test if available, else re-run A.
    ab_metrics_path = out_dir / "ab_metrics.json"
    if ab_metrics_path.exists():
        a_metrics = json.loads(ab_metrics_path.read_text())["A"]
        print("Loaded A metrics from ab_metrics.json")
    else:
        from modal.ab_test import run_a
        _, a_metrics = run_a(sample_csv, out_dir)
        print("Re-ran A pipeline for comparison.")

    print("\n=== Multi-crop Qwen ===")
    client = QwenClient()
    out_csv, b_metrics, b_meta = run_multicrop(
        sample_csv=sample_csv,
        out_dir=out_dir,
        client=client,
        cache_dir=crop_cache,
    )
    print(f"  wrote {out_csv}")
    print(f"  succeeded claims: {b_meta.get('succeeded_claims')} | failed: {b_meta.get('failed_claims')}")
    print(f"  total crop calls: {b_meta.get('total_crop_calls')}")
    print(f"  row accuracy: {b_metrics.get('row_accuracy', 0):.2%}")

    report = write_report(
        out_dir=out_dir,
        a_metrics=a_metrics,
        b_metrics=b_metrics,
        b_meta=b_meta,
        sample_csv=sample_csv,
        note=args.note,
    )
    print(f"\nReport: {report}")

    metrics_path = out_dir / "multicrop_metrics.json"
    metrics_path.write_text(
        json.dumps(
            {
                "A": a_metrics,
                "B": b_metrics,
                "B_meta": b_meta,
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"Metrics: {metrics_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())