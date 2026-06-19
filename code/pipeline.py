import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from tqdm import tqdm

from config import DATASET_DIR, PRIMARY_VLM_MODEL, CACHE_DIR
from schema import EVIDENCE_REQUIREMENTS_BY_OBJECT, OUTPUT_COLUMNS
from prompts import build_inspection_prompt
from models import VLMClient, parse_json_from_text
from rules import apply_rules, normalize_supporting_ids
from image_quality import analyze_images


def load_user_history(path: Path) -> Dict[str, Dict[str, Any]]:
    df = pd.read_csv(path, dtype=str)
    return {str(row["user_id"]): row.to_dict() for _, row in df.iterrows()}


def load_evidence_requirements(path: Path) -> Dict[str, List[str]]:
    df = pd.read_csv(path, dtype=str)
    result: Dict[str, List[str]] = {}
    for _, row in df.iterrows():
        obj = row["claim_object"]
        req = row["minimum_image_evidence"]
        result.setdefault(obj, []).append(req)
    return result


def get_applicable_requirements(claim_object: str, evidence_requirements: Dict[str, List[str]]) -> List[str]:
    reqs = []
    reqs.extend(evidence_requirements.get("all", []))
    reqs.extend(evidence_requirements.get(claim_object, []))
    return reqs


def image_paths_to_abs(image_paths: str) -> List[Path]:
    paths = [p.strip() for p in image_paths.split(";") if p.strip()]
    abs_paths = []
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = DATASET_DIR / path
        abs_paths.append(path.resolve())
    return abs_paths


def image_ids_from_paths(image_paths: List[Path]) -> List[str]:
    return [p.stem for p in image_paths]


def process_claim(
    row: Dict[str, Any],
    user_history_map: Dict[str, Dict[str, Any]],
    evidence_requirements: Dict[str, List[str]],
    vlm_client: VLMClient,
    model: str = PRIMARY_VLM_MODEL,
    prompt_version: str = "v1",
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    user_id = str(row["user_id"])
    claim_object = str(row["claim_object"]).lower().strip()
    user_claim = str(row["user_claim"])
    image_paths_str = str(row["image_paths"])

    user_history = user_history_map.get(user_id, {})
    reqs = get_applicable_requirements(claim_object, evidence_requirements)
    abs_image_paths = image_paths_to_abs(image_paths_str)

    prompt = build_inspection_prompt(
        claim_object=claim_object,
        user_claim=user_claim,
        user_history=user_history,
        evidence_requirements=reqs,
        image_count=len(abs_image_paths),
        prompt_version=prompt_version,
    )

    vlm_result = vlm_client.call(
        model=model,
        prompt=prompt,
        image_paths=abs_image_paths,
        prompt_version=prompt_version,
    )

    vlm_output = parse_json_from_text(vlm_result.get("content", ""))
    metadata = {
        "raw_content": vlm_result.get("content", ""),
        "model": vlm_result.get("model", model),
        "usage": vlm_result.get("usage", {}),
        "latency": vlm_result.get("latency", 0.0),
        "cache_hit": vlm_result.get("cache_hit", False),
    }

    if vlm_output is None:
        # Fallback: structured default
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

    deterministic_quality = analyze_images(abs_image_paths)
    deterministic_quality_flags = [k for k, v in deterministic_quality.items() if v]
    metadata["deterministic_quality_flags"] = deterministic_quality_flags

    final = apply_rules(
        claim_object,
        vlm_output,
        user_history,
        deterministic_quality_flags=deterministic_quality_flags,
    )

    output_row = {
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
    }

    return output_row, metadata


def run_pipeline(
    input_csv: Path,
    output_csv: Path,
    user_history_csv: Path = DATASET_DIR / "user_history.csv",
    evidence_requirements_csv: Path = DATASET_DIR / "evidence_requirements.csv",
    model: Optional[str] = None,
    prompt_version: str = "v2",
) -> Dict[str, Any]:
    input_csv = Path(input_csv).resolve()
    output_csv = Path(output_csv).resolve()
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    model = model or PRIMARY_VLM_MODEL

    user_history_map = load_user_history(user_history_csv)
    evidence_requirements = load_evidence_requirements(evidence_requirements_csv)

    df = pd.read_csv(input_csv, dtype=str)
    vlm_client = VLMClient()

    rows: List[Dict[str, str]] = []
    metadata: List[Dict[str, Any]] = []

    start = time.time()
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Processing claims"):
        out_row, meta = process_claim(
            row=row.to_dict(),
            user_history_map=user_history_map,
            evidence_requirements=evidence_requirements,
            vlm_client=vlm_client,
            model=model,
            prompt_version=prompt_version,
        )
        rows.append(out_row)
        metadata.append(meta)

    total_time = time.time() - start

    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    total_images = sum(len(image_paths_to_abs(r["image_paths"])) for r in rows)
    total_tokens = sum(
        m.get("usage", {}).get("total_tokens", 0) or m.get("usage", {}).get("completion_tokens", 0)
        for m in metadata
    )
    cache_hits = sum(1 for m in metadata if m.get("cache_hit"))

    return {
        "rows_processed": len(rows),
        "images_processed": total_images,
        "output_csv": str(output_csv),
        "total_time_seconds": total_time,
        "total_tokens": total_tokens,
        "cache_hits": cache_hits,
        "metadata": metadata,
    }
