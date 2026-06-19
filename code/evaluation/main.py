import argparse
import csv
import json
from pathlib import Path
from typing import Dict, List, Set, Tuple

import pandas as pd

# Add code/ to path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schema import OUTPUT_COLUMNS


def normalize(value: str) -> str:
    return str(value).strip().lower()


def parse_risk_flags(flags: str) -> Set[str]:
    if not flags or normalize(flags) == "none":
        return set()
    return {normalize(f) for f in str(flags).split(";") if f.strip()}


def compute_metrics(predicted: pd.DataFrame, ground_truth: pd.DataFrame) -> Dict:
    predicted = predicted[OUTPUT_COLUMNS].copy()
    ground_truth = ground_truth[OUTPUT_COLUMNS].copy()

    assert len(predicted) == len(ground_truth), "Row counts differ"

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

    per_field: Dict[str, Dict[str, int]] = {}
    for field in fields:
        per_field[field] = {"correct": 0, "total": 0, "errors": []}

    row_correct = 0
    row_errors: List[Dict] = []

    for idx in range(len(predicted)):
        pred_row = predicted.iloc[idx]
        gt_row = ground_truth.iloc[idx]

        row_all_correct = True
        row_diff = {
            "index": idx,
            "user_id": pred_row.get("user_id", "?"),
            "claim_object": pred_row.get("claim_object", "?"),
            "differences": {},
        }

        for field in fields:
            p = pred_row.get(field, "")
            g = gt_row.get(field, "")
            correct = False

            if field == "risk_flags":
                p_set = parse_risk_flags(str(p))
                g_set = parse_risk_flags(str(g))
                if p_set == g_set:
                    correct = True
                elif p_set.issubset(g_set) or g_set.issubset(p_set):
                    # Partial credit only for reporting
                    correct = False
            else:
                correct = normalize(str(p)) == normalize(str(g))

            per_field[field]["total"] += 1
            if correct:
                per_field[field]["correct"] += 1
            else:
                row_all_correct = False
                row_diff["differences"][field] = {"predicted": str(p), "ground_truth": str(g)}
                per_field[field]["errors"].append({
                    "index": idx,
                    "user_id": pred_row.get("user_id", "?"),
                    "predicted": str(p),
                    "ground_truth": str(g),
                })

        if row_all_correct:
            row_correct += 1
        else:
            row_errors.append(row_diff)

    metrics = {
        "total_rows": len(predicted),
        "row_accuracy": row_correct / len(predicted),
        "per_field_accuracy": {
            field: per_field[field]["correct"] / per_field[field]["total"]
            for field in fields
        },
        "per_field_correct": {field: per_field[field]["correct"] for field in fields},
        "per_field_total": {field: per_field[field]["total"] for field in fields},
        "row_errors": row_errors,
    }
    return metrics


def main():
    parser = argparse.ArgumentParser(description="Evaluate output.csv against sample_claims.csv")
    parser.add_argument("--predicted", type=str, required=True, help="Path to predicted output CSV")
    parser.add_argument("--ground-truth", type=str, required=True, help="Path to ground truth CSV")
    parser.add_argument("--report", type=str, default=None, help="Optional JSON report path")
    args = parser.parse_args()

    pred = pd.read_csv(args.predicted, dtype=str)
    gt = pd.read_csv(args.ground_truth, dtype=str)

    metrics = compute_metrics(pred, gt)

    print(f"Total rows: {metrics['total_rows']}")
    print(f"Row accuracy: {metrics['row_accuracy']:.2%}")
    print("Per-field accuracy:")
    for field, acc in metrics["per_field_accuracy"].items():
        print(f"  {field}: {acc:.2%} ({metrics['per_field_correct'][field]}/{metrics['per_field_total'][field]})")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)
        print(f"Report saved to {args.report}")


if __name__ == "__main__":
    main()
