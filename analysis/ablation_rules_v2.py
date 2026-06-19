"""
Phase 2 ablation driver for the rules_v2 engine.

Runs the offline replay harness with multiple layer combinations of
``apply_rules_v2`` against the 20-row ground truth, prints a per-attribute
comparison table, and writes a JSON report.

Configurations tested:
  - baseline      : the current code/rules.py apply_rules (reproduces v6)
  - all_on        : all 6 layers enabled
  - all_off       : all 6 layers disabled (engine is essentially a pass-through)
  - single_on_<L> : only layer <L> enabled
  - single_off_<L>: all layers on except <L>

The script is deterministic and does not call any VLM API.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")
sys.path.insert(0, str(REPO_ROOT / "code"))

import pandas as pd

from rules import apply_rules as apply_rules_v1
from rules_v2 import apply_rules_v2

REPORT_PATH = REPO_ROOT / "evaluation" / "v6_report.json"
GT_PATH = REPO_ROOT / "dataset" / "sample_claims.csv"
USER_HISTORY_PATH = REPO_ROOT / "dataset" / "user_history.csv"
OUT_DIR = REPO_ROOT / "analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

LAYER_NAMES = [
    "layer_severity",
    "layer_visibility",
    "layer_taxonomy",
    "layer_evidence",
    "layer_risk_flags",
    "layer_supporting",
]


def load_user_history() -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(USER_HISTORY_PATH, dtype=str).fillna("")
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def load_metadata() -> List[Dict[str, Any]]:
    with open(REPORT_PATH, "r", encoding="utf-8") as f:
        report = json.load(f)
    return report["metadata"]


def load_gt() -> pd.DataFrame:
    return pd.read_csv(GT_PATH, dtype=str).fillna("")


def parse_raw(raw: str) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        try:
            start = raw.index("{")
            end = raw.rindex("}")
            return json.loads(raw[start : end + 1])
        except Exception:
            return {}


def make_apply_v2(**layer_overrides):
    """Return a function with signature (vlm_output, user_history, deterministic_quality_flags, claim_object) -> dict."""
    def _f(vlm_output, user_history, deterministic_quality_flags, claim_object):
        kwargs = {n: True for n in LAYER_NAMES}
        kwargs.update(layer_overrides)
        return apply_rules_v2(
            claim_object=claim_object,
            vlm_output=vlm_output,
            user_history=user_history,
            deterministic_quality_flags=deterministic_quality_flags,
            **kwargs,
        )
    return _f


def apply_v1_adapter(vlm_output, user_history, deterministic_quality_flags, claim_object):
    return apply_rules_v1(
        claim_object=claim_object,
        vlm_output=vlm_output,
        user_history=user_history,
        deterministic_quality_flags=deterministic_quality_flags,
    )


def compute_metrics(predicted: pd.DataFrame, ground_truth: pd.DataFrame) -> Dict[str, Any]:
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

    def norm(s: str) -> str:
        return str(s).strip().lower()

    def parse_risk_flags(flags: str) -> set:
        if not flags or norm(flags) == "none":
            return set()
        return {norm(f) for f in str(flags).split(";") if f.strip()}

    per_field: Dict[str, Dict[str, int]] = {f: {"correct": 0, "total": 0} for f in fields}
    row_correct = 0
    row_errors: List[Dict[str, Any]] = []

    for idx in range(len(predicted)):
        p = predicted.iloc[idx]
        g = ground_truth.iloc[idx]
        all_ok = True
        diff: Dict[str, Any] = {"index": idx, "user_id": p.get("user_id", "?"), "differences": {}}
        for f in fields:
            pv = p.get(f, "")
            gv = g.get(f, "")
            ok = False
            if f == "risk_flags":
                ok = parse_risk_flags(pv) == parse_risk_flags(gv)
            else:
                ok = norm(pv) == norm(gv)
            per_field[f]["total"] += 1
            if ok:
                per_field[f]["correct"] += 1
            else:
                all_ok = False
                diff["differences"][f] = {"predicted": str(pv), "ground_truth": str(gv)}
        if all_ok:
            row_correct += 1
        else:
            row_errors.append(diff)

    return {
        "total_rows": len(predicted),
        "row_accuracy": row_correct / len(predicted),
        "per_field_accuracy": {f: per_field[f]["correct"] / per_field[f]["total"] for f in fields},
        "per_field_correct": {f: per_field[f]["correct"] for f in fields},
        "per_field_total": {f: per_field[f]["total"] for f in fields},
        "row_errors": row_errors,
    }


def run_config(name: str, apply_fn, metadata, gt_df, user_history) -> Dict[str, Any]:
    OUTPUT_COLUMNS = [
        "user_id", "image_paths", "user_claim", "claim_object",
        "evidence_standard_met", "evidence_standard_met_reason", "risk_flags",
        "issue_type", "object_part", "claim_status", "claim_status_justification",
        "supporting_image_ids", "valid_image", "severity",
    ]
    rows: List[Dict[str, str]] = []
    for idx, m in enumerate(metadata):
        user_id = m.get("user_id") or gt_df.iloc[idx]["user_id"]
        gt_row = gt_df.iloc[idx]
        user_history = user_history.get(str(user_id), {}) or {}
        vlm_output = parse_raw(m.get("raw_content", ""))
        det_flags = m.get("deterministic_quality_flags", []) or []
        claim_object = str(gt_row["claim_object"]).lower().strip()

        if not vlm_output:
            final = {
                "evidence_standard_met": "false",
                "evidence_standard_met_reason": "All submitted images are unreadable.",
                "risk_flags": "non_original_image",
                "issue_type": "unknown",
                "object_part": "unknown",
                "claim_status": "not_enough_information",
                "claim_status_justification": "None of the submitted images could be opened.",
                "supporting_image_ids": "none",
                "valid_image": "false",
                "severity": "unknown",
            }
        else:
            final = apply_fn(vlm_output, user_history, det_flags, claim_object)

        out = {
            "user_id": str(user_id),
            "image_paths": str(gt_row["image_paths"]),
            "user_claim": str(gt_row["user_claim"]),
            "claim_object": claim_object,
            "evidence_standard_met": str(final.get("evidence_standard_met", "")),
            "evidence_standard_met_reason": str(final.get("evidence_standard_met_reason", "")),
            "risk_flags": str(final.get("risk_flags", "none")),
            "issue_type": str(final.get("issue_type", "unknown")),
            "object_part": str(final.get("object_part", "unknown")),
            "claim_status": str(final.get("claim_status", "not_enough_information")),
            "claim_status_justification": str(final.get("claim_status_justification", "")),
            "supporting_image_ids": str(final.get("supporting_image_ids", "none")),
            "valid_image": str(final.get("valid_image", "false")),
            "severity": str(final.get("severity", "unknown")),
        }
        rows.append(out)

    pred_df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    metrics = compute_metrics(pred_df, gt_df[OUTPUT_COLUMNS])
    return {"name": name, "metrics": metrics, "pred_df": pred_df}


def main():
    metadata = load_metadata()
    gt_df = load_gt()
    user_history = load_user_history()

    configs: List[tuple] = []
    configs.append(("baseline_v1", apply_v1_adapter))
    configs.append(("v2_all_on", make_apply_v2()))
    configs.append(("v2_all_off", make_apply_v2(**{n: False for n in LAYER_NAMES})))
    for layer in LAYER_NAMES:
        only = {n: False for n in LAYER_NAMES}
        only[layer] = True
        configs.append((f"v2_only_{layer}", make_apply_v2(**only)))
    for layer in LAYER_NAMES:
        off = {n: True for n in LAYER_NAMES}
        off[layer] = False
        configs.append((f"v2_minus_{layer}", make_apply_v2(**off)))

    results: List[Dict[str, Any]] = []
    for name, fn in configs:
        r = run_config(name, fn, metadata, gt_df, user_history)
        results.append(r)
        m = r["metrics"]
        print(f"== {name} ==")
        for f, acc in m["per_field_accuracy"].items():
            print(f"  {f:>24s}: {acc:.2%} ({m['per_field_correct'][f]}/{m['per_field_total'][f]})")
        print(f"  {'row_accuracy':>24s}: {m['row_accuracy']:.2%}")
        print()

    # Persist the all_on result as the candidate v2 output for the report
    all_on = next(r for r in results if r["name"] == "v2_all_on")
    all_on["pred_df"].to_csv(OUT_DIR / "replay_v2_all_on.csv", index=False)

    summary = []
    for r in results:
        m = r["metrics"]
        summary.append({
            "config": r["name"],
            "row_accuracy": m["row_accuracy"],
            "issue_type": m["per_field_accuracy"]["issue_type"],
            "severity": m["per_field_accuracy"]["severity"],
            "risk_flags": m["per_field_accuracy"]["risk_flags"],
            "supporting_image_ids": m["per_field_accuracy"]["supporting_image_ids"],
            "evidence_standard_met": m["per_field_accuracy"]["evidence_standard_met"],
            "claim_status": m["per_field_accuracy"]["claim_status"],
            "object_part": m["per_field_accuracy"]["object_part"],
            "valid_image": m["per_field_accuracy"]["valid_image"],
        })
    with open(OUT_DIR / "ablation_v2_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print("=" * 72)
    print("SUMMARY (all 18 configs)")
    print("=" * 72)
    header = ["config", "row", "issue", "sev", "risk", "supp", "evid", "status", "part", "valid"]
    widths = [22, 6, 6, 6, 6, 6, 6, 6, 6, 6]
    line = "  ".join(h.ljust(w) for h, w in zip(header, widths))
    print(line)
    for s in summary:
        row = [
            s["config"],
            f"{s['row_accuracy']:.2f}",
            f"{s['issue_type']:.2f}",
            f"{s['severity']:.2f}",
            f"{s['risk_flags']:.2f}",
            f"{s['supporting_image_ids']:.2f}",
            f"{s['evidence_standard_met']:.2f}",
            f"{s['claim_status']:.2f}",
            f"{s['object_part']:.2f}",
            f"{s['valid_image']:.2f}",
        ]
        print("  ".join(c.ljust(w) for c, w in zip(row, widths)))


if __name__ == "__main__":
    main()
