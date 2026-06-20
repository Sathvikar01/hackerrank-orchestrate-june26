"""Replay the pipeline using cached VLM outputs and apply_rules_v2 (no API calls).

This is a development tool to measure the impact of switching the pipeline to the
rubric-faithful v2 rule engine. For each row in the input CSV, we:
  1. Reconstruct the prompt the same way the live pipeline does.
  2. Compute the same image-hash + prompt + model cache key.
  3. Look up the cached VLM response and parse its content.
  4. Apply apply_rules_v2 (or apply_rules as a baseline) to the parsed output.
  5. Write a CSV compatible with the evaluation script.
"""

import csv
import hashlib
import json
import sys
import os
from pathlib import Path

# Ensure code/ is on sys.path so sibling imports work.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")

import pandas as pd  # noqa: E402

from config import DATASET_DIR, CACHE_DIR  # noqa: E402
from prompts import build_inspection_prompt  # noqa: E402
from rules_v2 import apply_rules_v2  # noqa: E402
from image_quality import analyze_images  # noqa: E402


def image_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _client_for_model(model: str) -> str:
    if model.startswith(("meta/", "nvidia/", "microsoft/", "google/")):
        return "nvidia"
    return "mimo"


def cache_key_for(model: str, prompt: str, image_paths, prompt_version: str) -> str:
    parts = [prompt_version, model, prompt]
    for p in sorted(image_paths):
        parts.append(image_hash(Path(p)))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]


def find_cached(cache_dir: Path, cache_key: str):
    p = cache_dir / f"{cache_key}.json"
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def parse_json_from_text(text: str):
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            start = text.index("{")
            end = text.rindex("}")
            return json.loads(text[start : end + 1])
        except Exception:
            return None


def image_paths_to_abs(image_paths: str, dataset_dir: Path):
    out = []
    for p in image_paths.split(";"):
        p = p.strip()
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = dataset_dir / path
        out.append(path.resolve())
    return out


def load_user_history(path: Path):
    df = pd.read_csv(path, dtype=str)
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def load_evidence_requirements(path: Path):
    df = pd.read_csv(path, dtype=str)
    res = {}
    for _, r in df.iterrows():
        res.setdefault(r["claim_object"], []).append(r["minimum_image_evidence"])
    return res


def get_applicable_requirements(claim_object, evidence_reqs):
    out = list(evidence_reqs.get("all", []))
    out.extend(evidence_reqs.get(claim_object, []))
    return out


def render_output_row(user_id, image_paths_str, user_claim, claim_object, final):
    return {
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


def run_replay(
    input_csv: Path,
    output_csv: Path,
    user_history_csv: Path,
    evidence_requirements_csv: Path,
    cache_dir: Path,
    model: str = "mimo-v2.5",
    prompt_version: str = "v1",
):
    df = pd.read_csv(input_csv, dtype=str)
    user_history_map = load_user_history(user_history_csv)
    evidence_requirements = load_evidence_requirements(evidence_requirements_csv)

    rows = []
    metadata = []
    for _, r in df.iterrows():
        user_id = str(r["user_id"])
        claim_object = str(r["claim_object"]).lower().strip()
        user_claim = str(r["user_claim"])
        image_paths_str = str(r["image_paths"])
        user_history = user_history_map.get(user_id, {})
        reqs = get_applicable_requirements(claim_object, evidence_requirements)

        abs_paths = image_paths_to_abs(image_paths_str, DATASET_DIR)
        # Match the live pipeline: drop unreadable images before keying
        # the cache. This keeps replay keys in lock-step with live keys
        # when the input contains AVIF / HEIC / corrupt files.
        try:
            from PIL import Image, UnidentifiedImageError
            readable_paths = []
            for p in abs_paths:
                try:
                    Image.open(p).verify()
                    readable_paths.append(p)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue
            if readable_paths:
                abs_paths = readable_paths
        except ImportError:
            pass
        prompt = build_inspection_prompt(
            claim_object=claim_object,
            user_claim=user_claim,
            user_history=user_history,
            evidence_requirements=reqs,
            image_count=len(abs_paths),
            prompt_version=prompt_version,
        )
        key = cache_key_for(model, prompt, abs_paths, prompt_version)
        cached = find_cached(cache_dir, key)
        if cached is None:
            print(f"[MISS] {user_id}: no cache key={key}")
            vlm_output = None
            cache_hit = False
        else:
            vlm_output = parse_json_from_text(cached.get("content", ""))
            cache_hit = True

        if vlm_output is None:
            vlm_output = {
                "evidence_standard_met": False,
                "evidence_standard_met_reason": "No cached VLM output.",
                "issue_type": "unknown",
                "object_part": "unknown",
                "claim_status": "not_enough_information",
                "claim_status_justification": "No cached VLM output.",
                "supporting_image_ids": "none",
                "valid_image": True,
                "severity": "unknown",
                "image_quality_flags": [],
                "claim_mismatch": False,
                "wrong_object": False,
                "text_instruction_present": False,
                "possible_manipulation": False,
                "non_original_image": False,
                "visible_issues": [],
            }

        try:
            det = analyze_images(abs_paths)
        except Exception:
            det = {"blurry_image": False, "low_light_or_glare": False, "cropped_or_obstructed": False}
        det_flags = [k for k, v in det.items() if v]

        final = apply_rules_v2(
            claim_object,
            vlm_output,
            user_history,
            deterministic_quality_flags=det_flags,
        )

        row = render_output_row(user_id, image_paths_str, user_claim, claim_object, final)
        rows.append(row)
        metadata.append({
            "user_id": user_id,
            "cache_hit": cache_hit,
            "issue_type_pred": final["issue_type"],
            "severity_pred": final["severity"],
            "claim_status_pred": final["claim_status"],
        })

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        from schema import OUTPUT_COLUMNS
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    return rows, metadata


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--input", default=str(DATASET_DIR / "sample_claims.csv"))
    p.add_argument("--output", required=True)
    p.add_argument("--user-history", default=str(DATASET_DIR / "user_history.csv"))
    p.add_argument("--evidence-requirements", default=str(DATASET_DIR / "evidence_requirements.csv"))
    p.add_argument("--cache-dir", default=str(CACHE_DIR / "vlm_calls"))
    p.add_argument("--model", default="mimo-v2.5")
    p.add_argument("--prompt-version", default="v1")
    args = p.parse_args()

    rows, meta = run_replay(
        input_csv=Path(args.input),
        output_csv=Path(args.output),
        user_history_csv=Path(args.user_history),
        evidence_requirements_csv=Path(args.evidence_requirements),
        cache_dir=Path(args.cache_dir),
        model=args.model,
        prompt_version=args.prompt_version,
    )
    hits = sum(1 for m in meta if m["cache_hit"])
    print(f"Rows: {len(rows)}, cache hits: {hits}")
    for m in meta:
        print(m)


if __name__ == "__main__":
    main()
