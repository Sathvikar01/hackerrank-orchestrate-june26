"""Phase 3: Re-run the 20-row sample with the v3 VLM prompt.

Reads ``dataset/sample_claims.csv``, calls the VLM with
``code.prompts_v3.build_inspection_prompt_v3``, applies ``code.rules_v2``
(all layers on) to the raw VLM output, writes ``analysis/replay_v3_sample.csv``
(14 columns, schema.OUTPUT_COLUMNS order), scores against the ground truth,
and writes ``analysis/v3_sample_metrics.json``.

Cache behaviour
---------------
The script uses ``prompt_version="v3"`` so cache keys do not collide with
the existing v2 cache. Rows whose (model, prompt_version, prompt, image)
cache key already exists are read from disk without an API call; rows
without a cache entry are sent to the VLM. This is symmetric with
``code.pipeline.process_claim`` behaviour, and means the script is fast on
warm cache and full re-run on cold cache (estimated 5-6 minutes for 20
rows on first run).

Import strategy
---------------
``code/prompts_v3.py``, ``code/models.py``, and ``code/rules_v2.py`` all
import from sibling modules using the flat ``from schema import ...``
style, not ``from code.schema import ...``. We therefore add
``<repo>/code`` to ``sys.path`` BEFORE importing any of those modules.
Dummy env vars are set to avoid triggering ``code.config``'s API-key
check on import (the dummy keys are also safe enough for the VLM client
init, but the actual VLM call will fail if real keys are not present --
which is the correct behaviour for a CI run).

Usage
-----
::

    python analysis/rerun_v3_sample.py

Run from the repo root (``hackerrank-orchestrate-june26/``).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Path setup. Must happen BEFORE any ``code.*`` import.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"
ANALYSIS_DIR = REPO_ROOT / "analysis"
OUT_CSV = ANALYSIS_DIR / "replay_v3_sample.csv"
OUT_METRICS = ANALYSIS_DIR / "v3_sample_metrics.json"

os.environ.setdefault("NVIDIA_API_KEY", "dummy-rerun-v3")
os.environ.setdefault("MIMO_API_KEY", "dummy-rerun-v3")
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd  # noqa: E402

from prompts_v3 import build_inspection_prompt_v3  # noqa: E402
from rules_v2 import apply_rules_v2  # noqa: E402
from models import VLMClient, parse_json_from_text  # noqa: E402
from schema import OUTPUT_COLUMNS  # noqa: E402
from image_quality import analyze_images  # noqa: E402
from config import PRIMARY_VLM_MODEL, DATASET_DIR  # noqa: E402

PROMPT_VERSION = "v3"


# ---------------------------------------------------------------------------
# I/O helpers (mirror replay_harness.py / pipeline.py structure).
# ---------------------------------------------------------------------------


def _split_image_paths(image_paths_str: str) -> List[Path]:
    raw = [p.strip() for p in str(image_paths_str).split(";") if p.strip()]
    out: List[Path] = []
    for p in raw:
        path = Path(p)
        if not path.is_absolute():
            path = DATASET_DIR / path
        out.append(path.resolve())
    return out


def _filter_readable(paths: List[Path]) -> List[Path]:
    from PIL import Image, UnidentifiedImageError

    out: List[Path] = []
    for p in paths:
        try:
            Image.open(p).verify()
            out.append(p)
        except (UnidentifiedImageError, OSError, ValueError):
            continue
    return out


def _load_user_history() -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(REPO_ROOT / "dataset" / "user_history.csv", dtype=str).fillna("")
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def _load_evidence_requirements() -> Dict[str, List[str]]:
    df = pd.read_csv(REPO_ROOT / "dataset" / "evidence_requirements.csv", dtype=str).fillna("")
    result: Dict[str, List[str]] = {}
    for _, r in df.iterrows():
        obj = str(r["claim_object"])
        result.setdefault(obj, []).append(str(r["minimum_image_evidence"]))
    return result


def _applicable_requirements(claim_object: str, ev: Dict[str, List[str]]) -> List[str]:
    reqs: List[str] = []
    reqs.extend(ev.get("all", []))
    reqs.extend(ev.get(claim_object, []))
    return reqs


def _deterministic_quality_flags(paths: List[Path]) -> List[str]:
    if not paths:
        return []
    try:
        result = analyze_images(paths)
    except Exception:
        return []
    return [k for k, v in result.items() if v]


def _load_gt() -> pd.DataFrame:
    return pd.read_csv(REPO_ROOT / "dataset" / "sample_claims.csv", dtype=str).fillna("")


# ---------------------------------------------------------------------------
# Per-row processing.
# ---------------------------------------------------------------------------


def _process_row(
    row: Dict[str, Any],
    user_history_map: Dict[str, Dict[str, Any]],
    evidence_requirements: Dict[str, List[str]],
    vlm_client: VLMClient,
    model: str,
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    user_id = str(row["user_id"])
    claim_object = str(row["claim_object"]).lower().strip()
    user_claim = str(row["user_claim"])
    image_paths_str = str(row["image_paths"])

    uh = user_history_map.get(user_id, {"history_flags": "none"})
    uh.setdefault("history_flags", "none")

    declared_paths = _split_image_paths(image_paths_str)
    readable_paths = _filter_readable(declared_paths)

    metadata: Dict[str, Any] = {
        "user_id": user_id,
        "image_paths_declared": len(declared_paths),
        "image_paths_readable": len(readable_paths),
        "cache_hit": False,
        "latency": 0.0,
        "model": model,
        "prompt_version": PROMPT_VERSION,
    }

    if not readable_paths:
        # No readable images: fall back to the same NEI default the pipeline uses.
        vlm_output: Dict[str, Any] = {
            "evidence_standard_met": False,
            "evidence_standard_met_reason": "All submitted images are unreadable.",
            "issue_type": "unknown",
            "object_part": "unknown",
            "claim_status": "not_enough_information",
            "claim_status_justification": "None of the submitted images could be opened.",
            "supporting_image_ids": "none",
            "valid_image": "false",
            "severity": "unknown",
            "image_quality_flags": [],
            "claim_mismatch": False,
            "wrong_object": False,
            "wrong_object_part": False,
            "text_instruction_present": False,
            "possible_manipulation": False,
            "non_original_image": False,
            "visible_issues": [],
        }
        metadata["no_readable_images"] = True
        det_flags: List[str] = ["non_original_image"]
    else:
        reqs = _applicable_requirements(claim_object, evidence_requirements)
        prompt = build_inspection_prompt_v3(
            claim_object=claim_object,
            user_claim=user_claim,
            user_history=uh,
            evidence_requirements=reqs,
            image_count=len(readable_paths),
        )

        t0 = time.time()
        vlm_result = vlm_client.call(
            model=model,
            prompt=prompt,
            image_paths=readable_paths,
            prompt_version=PROMPT_VERSION,
        )
        metadata["latency"] = time.time() - t0
        metadata["cache_hit"] = bool(vlm_result.get("cache_hit", False))
        metadata["raw_content"] = vlm_result.get("content", "")

        parsed = parse_json_from_text(vlm_result.get("content", ""))
        if parsed is None:
            vlm_output = {
                "evidence_standard_met": False,
                "evidence_standard_met_reason": "Failed to parse model response.",
                "issue_type": "unknown",
                "object_part": "unknown",
                "claim_status": "not_enough_information",
                "claim_status_justification": "The model response could not be parsed; marking as insufficient evidence.",
                "supporting_image_ids": "none",
                "valid_image": "true",
                "severity": "unknown",
                "image_quality_flags": [],
                "claim_mismatch": False,
                "wrong_object": False,
                "text_instruction_present": False,
                "possible_manipulation": False,
                "non_original_image": False,
                "visible_issues": [],
            }
            metadata["parse_error"] = True
        else:
            vlm_output = parsed

        det_flags = _deterministic_quality_flags(readable_paths)
        metadata["deterministic_quality_flags"] = det_flags

    final = apply_rules_v2(
        claim_object=claim_object,
        vlm_output=vlm_output,
        user_history=uh,
        deterministic_quality_flags=det_flags,
        # all 6 layers on (matches analysis/ablation_rules_v2 "all_on")
        layer_severity=True,
        layer_visibility=True,
        layer_taxonomy=True,
        layer_evidence=True,
        layer_risk_flags=True,
        layer_supporting=True,
    )

    output_row = {col: "" for col in OUTPUT_COLUMNS}
    output_row.update({
        "user_id": user_id,
        "image_paths": image_paths_str,
        "user_claim": user_claim,
        "claim_object": claim_object,
        "evidence_standard_met": final["evidence_standard_met"],
        "evidence_standard_met_reason": final["evidence_standard_met_reason"],
        "risk_flags": final["risk_flags"],
        "issue_type": final["issue_type"],
        "object_part": final["object_part"],
        "claim_status": final["claim_status"],
        "claim_status_justification": final["claim_status_justification"],
        "supporting_image_ids": final["supporting_image_ids"],
        "valid_image": final["valid_image"],
        "severity": final["severity"],
    })
    return output_row, metadata


# ---------------------------------------------------------------------------
# Scoring (mirrors analysis/replay_harness.py scoring block).
# ---------------------------------------------------------------------------

SCORED_FIELDS = [
    "evidence_standard_met",
    "risk_flags",
    "issue_type",
    "object_part",
    "claim_status",
    "supporting_image_ids",
    "valid_image",
    "severity",
]


def _normalize_pred_field(field: str, value: Any) -> str:
    s = "" if value is None else str(value)
    if field == "supporting_image_ids":
        s = s.replace(";", "|")
    if field == "risk_flags":
        s = s.replace(";", "|")
    return s.strip().lower()


def _normalize_gt_field(field: str, value: Any) -> str:
    s = "" if value is None else str(value)
    if field == "supporting_image_ids":
        s = s.replace(";", "|")
    if field == "risk_flags":
        s = s.replace(";", "|")
    return s.strip().lower()


def _score(predicted: pd.DataFrame, gt: pd.DataFrame) -> Dict[str, Any]:
    per_field: Dict[str, Dict[str, int]] = {f: {"correct": 0, "total": 0} for f in SCORED_FIELDS}
    row_correct = 0
    row_errors: List[Dict[str, Any]] = []
    assert len(predicted) == len(gt), f"row count mismatch: pred={len(predicted)} gt={len(gt)}"
    for idx in range(len(predicted)):
        pred_row = predicted.iloc[idx]
        gt_row = gt.iloc[idx]
        all_ok = True
        diffs: Dict[str, Dict[str, str]] = {}
        for f in SCORED_FIELDS:
            p = _normalize_pred_field(f, pred_row.get(f, ""))
            g = _normalize_gt_field(f, gt_row.get(f, ""))
            per_field[f]["total"] += 1
            if p == g:
                per_field[f]["correct"] += 1
            else:
                all_ok = False
                diffs[f] = {"predicted": p, "ground_truth": g}
        if all_ok:
            row_correct += 1
        else:
            row_errors.append({
                "index": idx,
                "user_id": pred_row.get("user_id", "?"),
                "claim_object": pred_row.get("claim_object", "?"),
                "differences": diffs,
            })

    return {
        "total_rows": len(predicted),
        "row_accuracy": row_correct / max(1, len(predicted)),
        "per_field_accuracy": {f: per_field[f]["correct"] / max(1, per_field[f]["total"]) for f in SCORED_FIELDS},
        "per_field_correct": {f: per_field[f]["correct"] for f in SCORED_FIELDS},
        "per_field_total": {f: per_field[f]["total"] for f in SCORED_FIELDS},
        "row_errors": row_errors,
    }


def _print_metrics(metrics: Dict[str, Any]) -> None:
    print(f"Total rows: {metrics['total_rows']}")
    print(f"Row accuracy: {metrics['row_accuracy']:.2%}")
    print("Per-field accuracy:")
    for field in SCORED_FIELDS:
        acc = metrics["per_field_accuracy"][field]
        c = metrics["per_field_correct"][field]
        t = metrics["per_field_total"][field]
        print(f"  {field}: {acc:.2%} ({c}/{t})")
    if metrics["row_errors"]:
        print(f"Row errors ({len(metrics['row_errors'])}):")
        for re_ in metrics["row_errors"]:
            print(f"  idx={re_['index']} user_id={re_['user_id']} claim_object={re_['claim_object']}")
            for fname, diff in re_["differences"].items():
                print(f"    {fname}: pred={diff['predicted']!r} gt={diff['ground_truth']!r}")


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=PRIMARY_VLM_MODEL,
                        help="VLM model id (default: PRIMARY_VLM_MODEL from code/config.py)")
    parser.add_argument("--output-csv", default=str(OUT_CSV),
                        help="Path to write the 14-column output CSV")
    parser.add_argument("--output-metrics", default=str(OUT_METRICS),
                        help="Path to write the metrics JSON")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-row progress prints")
    args = parser.parse_args()

    out_csv = Path(args.output_csv)
    out_metrics = Path(args.output_metrics)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out_metrics.parent.mkdir(parents=True, exist_ok=True)

    user_history_map = _load_user_history()
    evidence_requirements = _load_evidence_requirements()
    gt = _load_gt()
    if len(gt) == 0:
        print("ERROR: dataset/sample_claims.csv is empty", file=sys.stderr)
        return 2

    vlm_client = VLMClient()

    rows: List[Dict[str, str]] = []
    metadata_rows: List[Dict[str, Any]] = []
    t_start = time.time()
    cache_hits = 0
    for idx in range(len(gt)):
        claim_row = gt.iloc[idx].to_dict()
        output_row, meta = _process_row(
            claim_row,
            user_history_map,
            evidence_requirements,
            vlm_client,
            args.model,
        )
        rows.append(output_row)
        metadata_rows.append(meta)
        if meta.get("cache_hit"):
            cache_hits += 1
        if not args.quiet:
            ch = "HIT " if meta.get("cache_hit") else "MISS"
            lat = meta.get("latency", 0.0)
            print(f"[{idx+1:2d}/{len(gt)}] {ch} {meta['user_id']} "
                  f"obj={meta.get('model', '?')} latency={lat:.1f}s "
                  f"issue={output_row['issue_type']!r} sev={output_row['severity']!r} "
                  f"status={output_row['claim_status']!r}")
        sys.stdout.flush()

    predicted = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    predicted.to_csv(out_csv, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Wrote predictions: {out_csv}")

    metrics = _score(predicted, gt)
    metrics["prompt_version"] = PROMPT_VERSION
    metrics["model"] = args.model
    metrics["wall_time_seconds"] = time.time() - t_start
    metrics["cache_hits"] = cache_hits
    metrics["api_calls"] = len(gt) - cache_hits
    metrics["per_row_metadata"] = metadata_rows

    with open(out_metrics, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"Wrote metrics: {out_metrics}")

    _print_metrics(metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
