"""Phase 4: Generate output_v2.csv for the 44-row test set using rules_v2.

Runs the pipeline (same VLM model and prompt as v1) but applies
code/rules_v2.apply_rules_v2 instead of code/rules.apply_rules.
Writes analysis/output_v2.csv with the 14-column schema.
"""
from __future__ import annotations

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

from prompts import build_inspection_prompt
from rules_v2 import apply_rules_v2
from models import VLMClient, parse_json_from_text
from schema import OUTPUT_COLUMNS
from image_quality import analyze_images
from config import DATASET_DIR, PRIMARY_VLM_MODEL, CACHE_DIR

EVIDENCE_PATH = DATASET_DIR / "evidence_requirements.csv"
USER_HISTORY_PATH = DATASET_DIR / "user_history.csv"
INPUT_CSV = DATASET_DIR / "claims.csv"
OUT_CSV = ANALYSIS_DIR / "output_v2.csv"

PROMPT_VERSION = "v1"


def load_user_history() -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(USER_HISTORY_PATH, dtype=str).fillna("")
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def load_evidence_requirements() -> Dict[str, List[str]]:
    df = pd.read_csv(EVIDENCE_PATH, dtype=str).fillna("")
    result: Dict[str, List[str]] = {}
    for _, r in df.iterrows():
        result.setdefault(r["claim_object"], []).append(r["minimum_image_evidence"])
    return result


def get_applicable_requirements(claim_object: str, evidence_requirements: Dict[str, List[str]]) -> List[str]:
    reqs = []
    reqs.extend(evidence_requirements.get("all", []))
    reqs.extend(evidence_requirements.get(claim_object, []))
    return reqs


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


def process_row(row, vlm_client, evidence_requirements, user_history_map, model):
    user_id = str(row["user_id"])
    claim_object = str(row["claim_object"]).lower().strip()
    user_claim = str(row["user_claim"])
    image_paths_str = str(row["image_paths"])
    user_history = user_history_map.get(user_id, {})
    reqs = get_applicable_requirements(claim_object, evidence_requirements)

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

    prompt = build_inspection_prompt(
        claim_object=claim_object, user_claim=user_claim,
        user_history=user_history, evidence_requirements=reqs,
        image_count=len(readable), prompt_version="v2",
    )
    vlm_result = vlm_client.call(
        model=model, prompt=prompt, image_paths=readable,
        prompt_version=PROMPT_VERSION,
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
    print(f"Input: {INPUT_CSV}")
    print(f"Output: {OUT_CSV}")
    print(f"Model: {PRIMARY_VLM_MODEL}")

    df = pd.read_csv(INPUT_CSV, dtype=str).fillna("")
    evidence_requirements = load_evidence_requirements()
    user_history_map = load_user_history()
    vlm = VLMClient()

    rows = []
    t0 = time.time()
    for i, row in df.iterrows():
        print(f"[{i+1}/{len(df)}] user={row['user_id']} ...", flush=True)
        r = process_row(row, vlm, evidence_requirements, user_history_map, PRIMARY_VLM_MODEL)
        rows.append(r)
    dt = time.time() - t0

    pred = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    pred.to_csv(OUT_CSV, index=False)
    print(f"\nDone in {dt:.1f}s")
    print(f"Wrote {len(pred)} rows to {OUT_CSV}")


if __name__ == "__main__":
    main()
