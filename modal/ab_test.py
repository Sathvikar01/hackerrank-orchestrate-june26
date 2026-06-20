"""modal/ab_test.py

A/B test harness.

    A = current production pipeline (code.pipeline.run_pipeline)
    B = Qwen2.5-VL on Modal (modal.qwen_client.QwenClient) -> rules.apply_rules

The runner produces three artifacts in ``evaluation/``:

* ``ab_A_output.csv`` - the same rows the current pipeline produced
* ``ab_B_output.csv`` - Qwen-backed predictions (or stubs if Modal is not
  configured)
* ``ab_metrics.json`` - per-field accuracy for both pipelines + deltas
* ``qwen_ab_test.md``  - human-readable A/B report

Usage
-----
::

    # After deploying the Modal service
    modal deploy modal/qwen_service.py
    export QWEN_ENDPOINT_URL=https://<workspace>--qwen-vl-predict-damage-http.modal.run

    python modal/ab_test.py --sample-csv dataset/sample_claims.csv --out-dir evaluation

    # Or, without deploying (records deferred B):
    python modal/ab_test.py --sample-csv dataset/sample_claims.csv --out-dir evaluation \\
        --note "Modal not deployed - B is projected"

The runner always evaluates A against ``sample_claims.csv``. For B it tries
the Modal endpoint; if ``QWEN_ENDPOINT_URL`` is unset or the request fails it
records an explicit "B = deferred / stub" section in the report so the user
can re-run after deployment to fill in real numbers.

Note
----
The local ``modal/`` folder is shadowed by the installed Modal Labs SDK on
the import path. We force the local folder to win by inserting the repo
root + the local ``modal/`` directory at the front of ``sys.path``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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

from schema import OUTPUT_COLUMNS  # noqa: E402  (intentional, after sys.path fix)

# Importing `modal.*` now resolves to local files, not the SDK.
from modal.qwen_client import QwenClient, QwenEndpointUnreachable  # noqa: E402
from modal.qwen_schema import QwenSchemaValidationError  # noqa: E402


# ---------------------------------------------------------------------------
# Qwen output -> pipeline row
# ---------------------------------------------------------------------------


def qwen_to_row(
    user_id: str,
    image_paths_str: str,
    user_claim: str,
    claim_object: str,
    qwen: Dict[str, Any],
) -> Dict[str, str]:
    """Translate a Qwen schema-conformant dict into the project output row.

    Uses the same enum vocabulary as ``code/schema.py`` so the evaluator can
    diff against the ground-truth rows without remapping.
    """
    issue_type = qwen.get("issue_type", "unknown")
    object_part = qwen.get("object_part", "unknown")
    severity = qwen.get("severity", "unknown")
    visible_damage = bool(qwen.get("visible_damage", False))
    evidence_sufficient = bool(qwen.get("evidence_sufficient", False))
    flags = qwen.get("quality_flags", []) or []

    # Translate to the production schema.
    if not visible_damage or issue_type in ("none", "unknown"):
        issue_type_out = "none" if issue_type == "none" else "unknown"
    else:
        issue_type_out = issue_type

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
            "Qwen2.5-VL on Modal reports the image set is sufficient to "
            "evaluate the claim."
            if evidence_sufficient
            else "Qwen2.5-VL on Modal reports the image set is not sufficient."
        ),
        "risk_flags": risk_flags,
        "issue_type": issue_type_out,
        "object_part": object_part,
        "claim_status": claim_status,
        "claim_status_justification": (
            f"Qwen2.5-VL reports {issue_type_out} on {object_part} "
            f"({severity}). Visible damage: {visible_damage}."
        ),
        "supporting_image_ids": supporting_ids,
        "valid_image": "true",
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# A: current pipeline
# ---------------------------------------------------------------------------


def run_a(sample_csv: Path, out_dir: Path) -> Tuple[Path, Dict[str, Any]]:
    """Run the current production pipeline and evaluate it."""
    from pipeline import run_pipeline

    out_csv = out_dir / "ab_A_output.csv"
    stats = run_pipeline(
        input_csv=sample_csv,
        output_csv=out_csv,
        prompt_version="v2",
    )
    metrics = evaluate(out_csv, sample_csv)
    metrics["runtime_seconds"] = stats["total_time_seconds"]
    metrics["tokens"] = stats["total_tokens"]
    metrics["cache_hits"] = stats["cache_hits"]
    return out_csv, metrics


# ---------------------------------------------------------------------------
# B: Qwen
# ---------------------------------------------------------------------------


def run_b(
    sample_csv: Path,
    out_dir: Path,
    client: QwenClient,
    image_paths_resolver,
) -> Tuple[Path, Dict[str, Any], Dict[str, Any]]:
    """Run the Qwen pipeline. Returns (csv, metrics, run_meta)."""
    out_csv = out_dir / "ab_B_output.csv"
    rows: List[Dict[str, str]] = []
    total_latency_ms = 0
    succeeded = 0
    failed = 0
    parse_failures = 0
    transport_failures = 0
    start = time.time()

    with open(sample_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            user_id = row["user_id"]
            image_paths_str = row["image_paths"]
            user_claim = row["user_claim"]
            claim_object = row["claim_object"].strip().lower()

            abs_paths = image_paths_resolver(image_paths_str)
            try:
                qwen = client.predict_damage(
                    image_paths=abs_paths,
                    claim_text=user_claim,
                    object_type=claim_object,
                )
                succeeded += 1
                total_latency_ms += int(
                    qwen.get("_meta", {}).get("latency_ms", 0)
                )
                out_row = qwen_to_row(
                    user_id=user_id,
                    image_paths_str=image_paths_str,
                    user_claim=user_claim,
                    claim_object=claim_object,
                    qwen=qwen,
                )
            except QwenEndpointUnreachable as exc:
                transport_failures += 1
                out_row = _stub_row(
                    user_id, image_paths_str, user_claim, claim_object,
                    reason=str(exc),
                )
            except QwenSchemaValidationError as exc:
                parse_failures += 1
                out_row = _stub_row(
                    user_id, image_paths_str, user_claim, claim_object,
                    reason=f"schema: {exc}",
                )
            except Exception as exc:  # pragma: no cover (network)
                transport_failures += 1
                out_row = _stub_row(
                    user_id, image_paths_str, user_claim, claim_object,
                    reason=f"transport: {exc}",
                )
            rows.append(out_row)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    metrics = evaluate(out_csv, sample_csv) if succeeded else _empty_metrics()
    run_meta = {
        "succeeded": succeeded,
        "failed": failed + parse_failures + transport_failures,
        "parse_failures": parse_failures,
        "transport_failures": transport_failures,
        "total_latency_ms": total_latency_ms,
        "mean_latency_ms": (
            int(total_latency_ms / succeeded) if succeeded else 0
        ),
        "runtime_seconds": time.time() - start,
    }
    return out_csv, metrics, run_meta


def _stub_row(
    user_id: str,
    image_paths_str: str,
    user_claim: str,
    claim_object: str,
    reason: str,
) -> Dict[str, str]:
    """Fallback row used when Qwen cannot be reached."""
    return {
        "user_id": user_id,
        "image_paths": image_paths_str,
        "user_claim": user_claim,
        "claim_object": claim_object,
        "evidence_standard_met": "false",
        "evidence_standard_met_reason": f"Qwen fallback: {reason}",
        "risk_flags": "manual_review_required",
        "issue_type": "unknown",
        "object_part": "unknown",
        "claim_status": "not_enough_information",
        "claim_status_justification": (
            f"Qwen2.5-VL could not be reached; manual review required. "
            f"Reason: {reason}"
        ),
        "supporting_image_ids": "none",
        "valid_image": "true",
        "severity": "unknown",
    }


def _empty_metrics() -> Dict[str, Any]:
    pf = {}
    pc = {}
    pt = {}
    for f in [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]:
        pf[f] = 0.0
        pc[f] = 0
        pt[f] = 0
    return {
        "total_rows": 0,
        "row_accuracy": 0.0,
        "per_field_accuracy": pf,
        "per_field_correct": pc,
        "per_field_total": pt,
        "row_errors": [],
    }


# ---------------------------------------------------------------------------
# Re-use the existing evaluator
# ---------------------------------------------------------------------------


def evaluate(pred_csv: Path, gt_csv: Path) -> Dict[str, Any]:
    """Delegate to ``code/evaluation/main.py``'s pure function."""
    from evaluation.main import compute_metrics  # type: ignore  # noqa: E402
    import pandas as pd  # noqa: E402

    pred = pd.read_csv(pred_csv, dtype=str)
    gt = pd.read_csv(gt_csv, dtype=str)
    return compute_metrics(pred, gt)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def _format_field_table(metrics: Dict[str, Any], suffix: str = "") -> str:
    pf = metrics.get("per_field_accuracy", {})
    rows = ["| Field | Accuracy |", "|---|---|"]
    for field in [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]:
        acc = pf.get(field)
        if acc is None:
            rows.append(f"| {field} | n/a |")
        else:
            label = f"{acc:.2%}"
            if suffix:
                label = f"{label} {suffix}"
            rows.append(f"| {field} | {label} |")
    row_acc = metrics.get("row_accuracy", 0.0)
    row_label = f"**{row_acc:.2%}**"
    if suffix:
        row_label = f"**{row_acc:.2%} {suffix}**"
    rows.append(f"| **row_accuracy** | {row_label} |")
    return "\n".join(rows)


def _format_confusion(
    metrics: Dict[str, Any], field: str
) -> str:
    """Render a per-row confusion breakdown for the given field."""
    rows = metrics.get("row_errors", []) or []
    cells = ["| user_id | claim_object | predicted | ground_truth |", "|---|---|---|---|"]
    for r in rows:
        diff = r.get("differences", {}).get(field)
        if not diff:
            continue
        cells.append(
            f"| {r.get('user_id', '?')} | {r.get('claim_object', '?')} | "
            f"{diff.get('predicted', '?')} | {diff.get('ground_truth', '?')} |"
        )
    if len(cells) == 2:
        cells.append("| - | - | (no errors) | - |")
    return "\n".join(cells)


def write_report(
    out_dir: Path,
    a_metrics: Dict[str, Any],
    b_metrics: Dict[str, Any],
    b_meta: Dict[str, Any],
    sample_csv: Path,
    note: Optional[str] = None,
) -> Path:
    """Write evaluation/qwen_ab_test.md"""
    path = out_dir / "qwen_ab_test.md"
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
        and b_meta.get("succeeded", 0) > 0
    )
    b_deferred = (
        b_meta.get("succeeded", 0) == 0
        and b_meta.get("transport_failures", 0) > 0
    )

    delta_rows = ["| Field | A | B | Delta |", "|---|---|---|---|"]
    for field in fields + ["row_accuracy"]:
        a_acc = a_metrics.get("per_field_accuracy", {}).get(field)
        if field == "row_accuracy":
            a_acc = a_metrics.get("row_accuracy", 0.0)
        if b_deferred:
            # Show 0.00% (the stub rows literally never matched the ground
            # truth on any field).
            b_acc = 0.0
            display = "0.00% (stub)"
        else:
            b_acc = b_metrics.get("per_field_accuracy", {}).get(field)
            if field == "row_accuracy":
                b_acc = b_metrics.get("row_accuracy", 0.0)
            display = (
                f"{b_acc:.2%}" if b_acc is not None else "n/a"
            )
        if a_acc is None:
            delta_rows.append(f"| {field} | n/a | {display} | - |")
        else:
            if isinstance(b_acc, (int, float)):
                d = b_acc - a_acc
                sign = "+" if d >= 0 else ""
                delta_rows.append(
                    f"| {field} | {a_acc:.2%} | {display} | {sign}{d:.2%} |"
                )
            else:
                delta_rows.append(f"| {field} | {a_acc:.2%} | {display} | - |")

    if deployed:
        status_block = (
            "**B = live Modal run** - all rows evaluated via deployed "
            "Qwen2.5-VL."
        )
    elif b_deferred:
        status_block = (
            "**B = deferred (Modal not deployed)** - every row returned the "
            "deterministic fallback (all fields = `unknown`/0%). Deploy "
            "`modal/qwen_service.py` and re-run to fill in real metrics."
        )
    else:
        status_block = (
            f"**B = partial run** - {b_meta.get('succeeded', 0)} succeeded, "
            f"{b_meta.get('failed', 0)} failed."
        )

    mean_latency = b_meta.get("mean_latency_ms", 0)
    runtime_b = b_meta.get("runtime_seconds", 0.0)
    runtime_a = a_metrics.get("runtime_seconds", 0.0)
    n_rows = a_metrics.get("total_rows", 0)

    if mean_latency > 0:
        cost_per_claim = (mean_latency / 1000.0) * 0.0006125
        cost_per_1k_warm = 1000 * cost_per_claim
        cost_per_1k_with_cold = cost_per_1k_warm + 0.025
        cost_test_set = (n_rows * cost_per_claim) + 0.025
        cost_block = (
            "## Cost projection (Modal A10G @ $0.0006125/GPU-s)\n\n"
            f"* Mean warm latency: **{mean_latency} ms**\n"
            f"* Cost per claim (warm): **${cost_per_claim:.4f}**\n"
            f"* Cost per 1,000 claims (warm, no cold start): "
            f"**${cost_per_1k_warm:.2f}**\n"
            f"* Cost per 1,000 claims including one cold start: "
            f"**${cost_per_1k_with_cold:.2f}**\n"
            f"* {n_rows}-row sample projected cost (one cold start): "
            f"**${cost_test_set:.4f}**"
        )
    else:
        cost_block = (
            "## Cost projection\n\n"
            "B never produced a successful inference, so warm latency is "
            "unknown. Re-run after deployment to populate this section.\n\n"
            "Modelled estimate (Qwen2.5-VL-3B on A10G, single image, "
            "1024 px): **3-5 s warm**, **~$0.002-$0.003 per claim**, "
            "**~$0.02 cold-start overhead** per burst. 44-row test set "
            "fits in a single cold start."
        )

    b_table_suffix = " (stub fallback)" if b_deferred else ""
    a_table = _format_field_table(a_metrics)
    b_table = _format_field_table(b_metrics, suffix=b_table_suffix)

    content = f"""# Qwen2.5-VL A/B Test Report

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Sample CSV:** `{sample_csv.relative_to(sample_csv.parent.parent)}` ({n_rows} rows)

## Status

{status_block}

{"**Note:** " + note if note else ""}

## A. Current production pipeline (mimo-v2.5 + rule engine)

Runtime: {runtime_a:.1f} s
Tokens consumed: {a_metrics.get('tokens', 0):,}
Cache hits: {a_metrics.get('cache_hits', 0)}

{a_table}

## B. Qwen2.5-VL pipeline (Modal A10G)

Runtime: {runtime_b:.1f} s
Succeeded: {b_meta.get('succeeded', 0)} / {(b_meta.get('succeeded', 0) + b_meta.get('failed', 0))}
Transport failures: {b_meta.get('transport_failures', 0)}
Schema parse failures: {b_meta.get('parse_failures', 0)}
Mean per-request latency: {mean_latency} ms (warm only)

{b_table}

## Head-to-head deltas

{chr(10).join(delta_rows)}

## B confusion matrix (issue_type)

{_format_confusion(b_metrics, 'issue_type')}

## B confusion matrix (severity)

{_format_confusion(b_metrics, 'severity')}

{cost_block}

## Recommendation

* If B improves issue_type or severity by >= 15 points vs A: replace the
  primary VLM with Qwen2.5-VL on Modal.
* If B improves 5-15 points: keep MIMO v2.5 as ensemble member and use Qwen
  for tie-breaking.
* If B improves < 5 points: keep current pipeline. Qwen still adds value as
  a verification cross-check.
* If Modal is not deployed: re-run this script after `modal deploy
  modal/qwen_service.py` to replace the deferred block above with measured
  numbers.
"""
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# CLI
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Qwen2.5-VL A/B test harness.")
    parser.add_argument(
        "--sample-csv",
        default="dataset/sample_claims.csv",
        help="Path to labeled sample claims CSV (default: dataset/sample_claims.csv)",
    )
    parser.add_argument(
        "--out-dir",
        default="evaluation",
        help="Directory to write ab_*.csv and qwen_ab_test.md (default: evaluation)",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Optional note to embed in the report (e.g. 'Modal not deployed')",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample_csv = (repo_root / args.sample_csv).resolve()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not sample_csv.exists():
        print(f"sample CSV not found: {sample_csv}", file=sys.stderr)
        return 2

    print("=== A: current production pipeline ===")
    a_csv, a_metrics = run_a(sample_csv, out_dir)
    print(f"  wrote {a_csv}")
    print(f"  row accuracy: {a_metrics.get('row_accuracy', 0):.2%}")

    print("\n=== B: Qwen2.5-VL on Modal ===")
    client = QwenClient()
    b_csv, b_metrics, b_meta = run_b(
        sample_csv=sample_csv,
        out_dir=out_dir,
        client=client,
        image_paths_resolver=_default_image_resolver(),
    )
    print(f"  wrote {b_csv}")
    print(f"  succeeded: {b_meta.get('succeeded')} | failed: {b_meta.get('failed')}")
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

    metrics_path = out_dir / "ab_metrics.json"
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