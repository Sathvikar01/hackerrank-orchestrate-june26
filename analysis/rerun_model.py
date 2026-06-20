"""Phase 3 variant: Re-run the 20-row sample with a different VLM model.

Uses the v3 prompt (from code/prompts_v3.py) but with a different model
override. Writes results to analysis/replay_v3_<model_short>.csv and
analysis/v3_<model_short>_metrics.json.

Cache: uses prompt_version="v3-<model_short>" so it doesn't collide with
the existing mimo-v2.5 v3 cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
DATASET_DIR = REPO_ROOT / "dataset"
ANALYSIS_DIR = REPO_ROOT / "analysis"

try:
    from dotenv import load_dotenv
    _env_path = REPO_ROOT / ".env"
    if _env_path.exists():
        load_dotenv(_env_path, override=True)
except Exception:
    pass

os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(REPO_ROOT))

import pandas as pd

from prompts_v3 import build_inspection_prompt_v3
from rules_v2 import apply_rules_v2
from models import VLMClient, parse_json_from_text
from schema import OUTPUT_COLUMNS
from image_quality import analyze_images
from config import DATASET_DIR

USER_HISTORY_PATH = DATASET_DIR / "user_history.csv"


def load_user_history() -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(USER_HISTORY_PATH, dtype=str).fillna("")
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def split_image_paths(s: str) -> List[Path]:
    out = []
    for p in str(s).split(";"):
        p = p.strip()
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = DATASET_DIR / p
        out.append(path.resolve())
    return out


def filter_readable(paths: List[Path]) -> List[Path]:
    from PIL import Image, UnidentifiedImageError
    ok = []
    for p in paths:
        try:
            Image.open(p).verify()
            ok.append(p)
        except Exception:
            continue
    return ok


def compute_metrics(predicted: pd.DataFrame, ground_truth: pd.DataFrame) -> Dict[str, Any]:
    fields = [
        "evidence_standard_met", "risk_flags", "issue_type", "object_part",
        "claim_status", "supporting_image_ids", "valid_image", "severity",
    ]

    def norm(s): return str(s).strip().lower()
    def parse_rf(s):
        if not s or norm(s) == "none": return set()
        return {norm(f) for f in str(s).split(";") if f.strip()}

    pf = {f: {"correct": 0, "total": 0} for f in fields}
    rc = 0
    for idx in range(len(predicted)):
        p, g = predicted.iloc[idx], ground_truth.iloc[idx]
        ok_all = True
        for f in fields:
            pv, gv = p.get(f, ""), g.get(f, "")
            ok = (parse_rf(pv) == parse_rf(gv)) if f == "risk_flags" else (norm(pv) == norm(gv))
            pf[f]["total"] += 1
            if ok:
                pf[f]["correct"] += 1
            else:
                ok_all = False
        if ok_all:
            rc += 1
    return {
        "row_accuracy": rc / len(predicted),
        "per_field_accuracy": {f: pf[f]["correct"] / pf[f]["total"] for f in fields},
        "per_field_correct": {f: pf[f]["correct"] for f in fields},
        "per_field_total": {f: pf[f]["total"] for f in fields},
    }


def process_row(row, vlm_client, model, prompt_version, user_history_map):
    user_id = str(row["user_id"])
    claim_object = str(row["claim_object"]).lower().strip()
    user_claim = str(row["user_claim"])
    image_paths_str = str(row["image_paths"])
    user_history = user_history_map.get(user_id, {})

    abs_paths = split_image_paths(image_paths_str)
    readable = filter_readable(abs_paths)
    if not readable:
        return {
            "user_id": user_id, "image_paths": image_paths_str, "user_claim": user_claim,
            "claim_object": claim_object, "evidence_standard_met": "false",
            "evidence_standard_met_reason": "All submitted images are unreadable.",
            "risk_flags": "non_original_image", "issue_type": "unknown",
            "object_part": "unknown", "claim_status": "not_enough_information",
            "claim_status_justification": "None of the submitted images could be opened.",
            "supporting_image_ids": "none", "valid_image": "false", "severity": "unknown",
        }

    det_flags = []
    try:
        q = analyze_images(readable)
        for k, v in (q or {}).items():
            if v:
                det_flags.append(str(k))
    except Exception:
        pass

    prompt = build_inspection_prompt_v3(
        claim_object=claim_object, user_claim=user_claim,
        user_history=user_history, evidence_requirements=[],
        image_count=len(readable),
    )
    vlm_result = vlm_client.call(
        model=model, prompt=prompt, image_paths=readable,
        prompt_version=prompt_version,
    )
    vlm_out = parse_json_from_text(vlm_result.get("content", "")) or {}
    if not vlm_out:
        return {
            "user_id": user_id, "image_paths": image_paths_str, "user_claim": user_claim,
            "claim_object": claim_object, "evidence_standard_met": "false",
            "evidence_standard_met_reason": "Failed to parse model response.",
            "risk_flags": "none", "issue_type": "unknown", "object_part": "unknown",
            "claim_status": "not_enough_information",
            "claim_status_justification": "Failed to parse model response.",
            "supporting_image_ids": "none", "valid_image": "false", "severity": "unknown",
        }
    final = apply_rules_v2(
        claim_object=claim_object, vlm_output=vlm_out,
        user_history=user_history, deterministic_quality_flags=det_flags,
    )
    return {
        "user_id": user_id, "image_paths": image_paths_str, "user_claim": user_claim,
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--prompt-version", default=None)
    ap.add_argument("--prompt", choices=["v3", "v2"], default="v3")
    args = ap.parse_args()

    model_short = args.model.split("/")[-1].replace(".", "-")
    pv = args.prompt_version or f"{args.prompt}-{model_short}"
    out_csv = ANALYSIS_DIR / f"replay_{pv}_sample.csv"
    out_metrics = ANALYSIS_DIR / f"{pv}_sample_metrics.json"

    print(f"Model: {args.model}")
    print(f"Prompt version: {pv}")
    print(f"Output: {out_csv}")

    df = pd.read_csv(DATASET_DIR / "sample_claims.csv", dtype=str).fillna("")
    gt = df[OUTPUT_COLUMNS].copy()
    user_history_map = load_user_history()
    vlm = VLMClient()

    rows = []
    t0 = time.time()
    for i, row in df.iterrows():
        print(f"[{i+1}/{len(df)}] user={row['user_id']} ...", flush=True)
        r = process_row(row, vlm, args.model, pv, user_history_map)
        rows.append(r)
    dt = time.time() - t0

    pred = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    pred.to_csv(out_csv, index=False)
    metrics = compute_metrics(pred, gt)
    metrics["model"] = args.model
    metrics["prompt_version"] = pv
    metrics["total_time_seconds"] = dt
    metrics["total_rows"] = len(df)
    with open(out_metrics, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2)

    print(f"\nDone in {dt:.1f}s")
    print(f"row_accuracy: {metrics['row_accuracy']:.2%}")
    for f, a in metrics["per_field_accuracy"].items():
        print(f"  {f}: {a:.2%} ({metrics['per_field_correct'][f]}/{metrics['per_field_total'][f]})")


if __name__ == "__main__":
    main()
