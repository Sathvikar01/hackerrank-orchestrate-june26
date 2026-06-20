"""
Counterfactual robustness harness for the v8 (post-remediation) rules engine.

Purpose
-------
After the overfitting remediation pass, every rule in `code/rules_v2.py`
must depend ONLY on causal evidence (image, claim text, conversation,
evidence requirements). user_history_risk is allowed to add *advisory
risk flags* (e.g. manual_review_required) but must NEVER change
claim_status / issue_type / severity / object_part.

This harness verifies that contract empirically:

  * Test A — for every sample row, replay the pipeline using the cached
    VLM output for that row. If current prompt/cache keys have changed,
    fall back to the saved raw VLM outputs in `evaluation/v6_report.json`.
    Build three variants of user_history for
    the SAME VLM output:
        A0. original user_history (baseline)
        A1. user_history_risk flipped ON for every row (history-noise)
        A2. user_history_risk flipped OFF for every row (history-noise)
        A3. user_ids permuted (history-shuffle)
    Diff A0 against A1/A2/A3. The diffs must be confined to risk_flags
    (additive / subtractive). claim_status, issue_type, severity,
    object_part, valid_image, supporting_image_ids must be invariant.

The VLM output is held FIXED across A0..A3. Claim-parser signals are
recomputed from the same claim text that the live pipeline uses.

Run:
    python analysis/counterfactual_harness.py
"""

from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = ROOT / "code"
CACHE_DIR = ROOT / ".cache" / "vlm_calls"
DATASET_DIR = ROOT / "dataset"
SAMPLE_CSV = DATASET_DIR / "sample_claims.csv"
USER_HISTORY_CSV = DATASET_DIR / "user_history.csv"
EVIDENCE_CSV = DATASET_DIR / "evidence_requirements.csv"
SAVED_RAW_REPORT = ROOT / "evaluation" / "v6_report.json"

for p in (str(CODE_DIR),):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")

# Use replay_pipeline's exact functions so the cache key matches.
import pandas as pd  # noqa: E402
import replay_pipeline  # noqa: E402
from config import DATASET_DIR as CFG_DATASET_DIR, PRIMARY_VLM_MODEL  # noqa: E402
from claim_parser import extract_claim_signals  # noqa: E402
from rules_v2 import apply_rules_v2  # noqa: E402


# --- helpers ---------------------------------------------------------------

DECISIONAL_FIELDS = [
    "claim_status",
    "issue_type",
    "severity",
    "object_part",
    "valid_image",
    "supporting_image_ids",
]


def _read_user_history(path: Path) -> Dict[str, Dict[str, str]]:
    df = pd.read_csv(path, dtype=str).fillna("")
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def _read_evidence_requirements(path: Path) -> Dict[str, List[str]]:
    df = pd.read_csv(path, dtype=str)
    res: Dict[str, List[str]] = {}
    for _, r in df.iterrows():
        res.setdefault(r["claim_object"], []).append(r["minimum_image_evidence"])
    return res


def _normalise_history_flags(uh: Dict[str, str]) -> Dict[str, str]:
    uh = dict(uh)
    uh["history_flags"] = str(uh.get("history_flags", "") or "")
    return uh


def _flip_history_risk_on(uh: Dict[str, str]) -> Dict[str, str]:
    uh = _normalise_history_flags(uh)
    flags = [f.strip() for f in uh["history_flags"].split(";") if f.strip()]
    if "user_history_risk" not in flags:
        flags.append("user_history_risk")
    uh["history_flags"] = ";".join(flags)
    return uh


def _flip_history_risk_off(uh: Dict[str, str]) -> Dict[str, str]:
    uh = _normalise_history_flags(uh)
    flags = [f.strip() for f in uh["history_flags"].split(";") if f.strip() and f.strip() != "user_history_risk"]
    uh["history_flags"] = ";".join(flags) if flags else "none"
    return uh


def _run_row(row: Dict[str, str], uh: Dict[str, str], vlm_output: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(vlm_output)
    enriched.update(extract_claim_signals(row.get("user_claim", ""), row["claim_object"]))
    final = apply_rules_v2(
        claim_object=row["claim_object"],
        vlm_output=enriched,
        user_history=uh,
        deterministic_quality_flags=None,
    )
    return {
        "evidence_standard_met": final["evidence_standard_met"],
        "risk_flags": final["risk_flags"],
        "issue_type": final["issue_type"],
        "object_part": final["object_part"],
        "claim_status": final["claim_status"],
        "supporting_image_ids": final["supporting_image_ids"],
        "valid_image": final["valid_image"],
        "severity": final["severity"],
    }


def _diff_dictionaries(a: Dict[str, Any], b: Dict[str, Any], keys: List[str]) -> Dict[str, Tuple[Any, Any]]:
    diff: Dict[str, Tuple[Any, Any]] = {}
    for k in keys:
        if str(a.get(k)) != str(b.get(k)):
            diff[k] = (a.get(k), b.get(k))
    return diff


def _flag_diff(a: str, b: str) -> Tuple[set, set]:
    A = set(x.strip() for x in str(a).split(";") if x.strip() and x.strip().lower() != "none")
    B = set(x.strip() for x in str(b).split(";") if x.strip() and x.strip().lower() != "none")
    return (B - A, A - B)  # added, removed


# --- the tests -------------------------------------------------------------

def _build_cached_vlm_map(
    rows: List[Dict[str, str]],
    user_history_map: Dict[str, Dict[str, str]],
    evidence_requirements: Dict[str, List[str]],
    model: str = "mimo-v2.5",
    prompt_version: str = "v1",
) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """For every sample row, build the same prompt + cache key that the
    live pipeline built, and read the cached VLM response. Uses
    replay_pipeline.cache_key_for to guarantee key parity."""
    cached_outputs: Dict[str, Dict[str, Any]] = {}
    misses: List[str] = []
    for row in rows:
        uid = row["user_id"]
        user_claim = row["user_claim"]
        claim_object = row["claim_object"]
        uh = user_history_map.get(uid, {})
        reqs = list(evidence_requirements.get("all", []))
        reqs.extend(evidence_requirements.get(claim_object, []))
        abs_paths = replay_pipeline.image_paths_to_abs(row["image_paths"], CFG_DATASET_DIR)
        readable_paths = []
        try:
            from PIL import Image, UnidentifiedImageError
            for p in abs_paths:
                try:
                    with Image.open(p) as img:
                        img.verify()
                    readable_paths.append(p)
                except (UnidentifiedImageError, OSError, ValueError):
                    continue
            if readable_paths:
                abs_paths = readable_paths
        except ImportError:
            pass
        # Build prompt exactly as replay_pipeline does.
        from prompts import build_inspection_prompt
        prompt = build_inspection_prompt(
            claim_object=claim_object,
            user_claim=user_claim,
            user_history=uh,
            evidence_requirements=reqs,
            image_count=len(abs_paths),
            prompt_version=prompt_version,
        )
        key = replay_pipeline.cache_key_for(model, prompt, abs_paths, prompt_version)
        cached = replay_pipeline.find_cached(CACHE_DIR, key)
        if cached is None:
            misses.append(uid)
            continue
        parsed = replay_pipeline.parse_json_from_text(cached.get("content", ""))
        if parsed is None:
            misses.append(uid)
            continue
        cached_outputs[uid] = parsed
    return cached_outputs, misses


def _load_saved_raw_vlm_map(rows: List[Dict[str, str]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Fallback source for robustness tests when prompt/cache keys changed.

    The report metadata is indexed in the same order as sample_claims.csv and
    contains raw VLM JSON strings from a prior live run. This does not validate
    prompt-cache reproducibility, but it does validate that the deterministic
    rules are not depending on user IDs or history for decisions.
    """
    if not SAVED_RAW_REPORT.exists():
        return {}, [r["user_id"] for r in rows]
    with open(SAVED_RAW_REPORT, "r", encoding="utf-8") as f:
        report = json.load(f)
    metadata = report.get("metadata", [])
    outputs: Dict[str, Dict[str, Any]] = {}
    misses: List[str] = []
    for idx, row in enumerate(rows):
        uid = row["user_id"]
        raw = ""
        if idx < len(metadata):
            raw = str(metadata[idx].get("raw_content", "") or "")
        parsed = replay_pipeline.parse_json_from_text(raw)
        if parsed is None:
            misses.append(uid)
            continue
        outputs[uid] = parsed
    return outputs, misses


def test_history_risk_randomised(
    rows: List[Dict[str, str]],
    user_history: Dict[str, Dict[str, str]],
    cached_outputs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    summary = {
        "rows_tested": 0,
        "decisional_violations": [],
        "supporting_image_violations": [],
        "risk_flag_legitimate_changes": [],
        "risk_flag_illegitimate_changes": [],
    }
    rng = random.Random(20260620)
    for row in rows:
        uid = row["user_id"]
        vlm_output = cached_outputs.get(uid)
        if vlm_output is None:
            continue
        original_uh = _normalise_history_flags(user_history[uid])
        baseline = _run_row(row, original_uh, vlm_output)

        # Randomise: flip ON for half the rows, OFF for the other half.
        if rng.random() < 0.5:
            cf_uh = _flip_history_risk_on(original_uh)
            tag = "history_risk_on"
        else:
            cf_uh = _flip_history_risk_off(original_uh)
            tag = "history_risk_off"
        cf = _run_row(row, cf_uh, vlm_output)

        decisional_diff = _diff_dictionaries(baseline, cf, DECISIONAL_FIELDS)
        if decisional_diff:
            summary["decisional_violations"].append({
                "user_id": uid, "tag": tag, "diff": decisional_diff,
            })

        if baseline["supporting_image_ids"] != cf["supporting_image_ids"]:
            summary["supporting_image_violations"].append({
                "user_id": uid, "tag": tag,
                "baseline": baseline["supporting_image_ids"],
                "cf": cf["supporting_image_ids"],
            })

        added, removed = _flag_diff(baseline["risk_flags"], cf["risk_flags"])
        allowed = {"manual_review_required", "user_history_risk"}
        illegitimate_added = added - allowed
        illegitimate_removed = removed - allowed
        if added or removed:
            entry = {"user_id": uid, "tag": tag,
                     "added": sorted(added), "removed": sorted(removed)}
            if illegitimate_added or illegitimate_removed:
                entry["illegitimate_added"] = sorted(illegitimate_added)
                entry["illegitimate_removed"] = sorted(illegitimate_removed)
                summary["risk_flag_illegitimate_changes"].append(entry)
            else:
                summary["risk_flag_legitimate_changes"].append(entry)

        summary["rows_tested"] += 1
    return summary


def test_user_id_shuffled(
    rows: List[Dict[str, str]],
    user_history: Dict[str, Dict[str, str]],
    cached_outputs: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    summary = {
        "rows_tested": 0,
        "decisional_violations": [],
        "supporting_image_violations": [],
        "risk_flag_legitimate_changes": [],
        "risk_flag_illegitimate_changes": [],
    }
    rng = random.Random(42)
    user_ids = list(user_history.keys())
    perm = user_ids[:]
    rng.shuffle(perm)
    for row, surrogate_uid in zip(rows, perm):
        uid = row["user_id"]
        vlm_output = cached_outputs.get(uid)
        if vlm_output is None:
            continue
        original_uh = _normalise_history_flags(user_history[uid])
        surrogate_uh = _normalise_history_flags(user_history[surrogate_uid])
        baseline = _run_row(row, original_uh, vlm_output)
        cf = _run_row(row, surrogate_uh, vlm_output)

        decisional_diff = _diff_dictionaries(baseline, cf, DECISIONAL_FIELDS)
        if decisional_diff:
            summary["decisional_violations"].append({
                "user_id": uid, "surrogate_uid": surrogate_uid, "diff": decisional_diff,
            })

        if baseline["supporting_image_ids"] != cf["supporting_image_ids"]:
            summary["supporting_image_violations"].append({
                "user_id": uid, "surrogate_uid": surrogate_uid,
                "baseline": baseline["supporting_image_ids"],
                "cf": cf["supporting_image_ids"],
            })

        added, removed = _flag_diff(baseline["risk_flags"], cf["risk_flags"])
        allowed = {"manual_review_required", "user_history_risk"}
        illegitimate_added = added - allowed
        illegitimate_removed = removed - allowed
        if added or removed:
            entry = {"user_id": uid, "surrogate_uid": surrogate_uid,
                     "added": sorted(added), "removed": sorted(removed)}
            if illegitimate_added or illegitimate_removed:
                entry["illegitimate_added"] = sorted(illegitimate_added)
                entry["illegitimate_removed"] = sorted(illegitimate_removed)
                summary["risk_flag_illegitimate_changes"].append(entry)
            else:
                summary["risk_flag_legitimate_changes"].append(entry)

        summary["rows_tested"] += 1
    return summary


# --- main ------------------------------------------------------------------

def main() -> int:
    print("=" * 78)
    print("Counterfactual robustness harness - v8 (post-remediation)")
    print("=" * 78)

    df = pd.read_csv(SAMPLE_CSV, dtype=str).fillna("")
    rows = df.to_dict("records")
    user_history = _read_user_history(USER_HISTORY_CSV)
    evidence_requirements = _read_evidence_requirements(EVIDENCE_CSV)
    print(f"Sample rows: {len(rows)}; user_history entries: {len(user_history)}")

    print("Loading cached VLM outputs via replay_pipeline cache keys...")
    cached_outputs, misses = _build_cached_vlm_map(
        rows, user_history, evidence_requirements,
        model=PRIMARY_VLM_MODEL, prompt_version="v1",
    )
    source = "current_cache"
    fallback_misses: List[str] = []
    if len(cached_outputs) == 0:
        print("No current cache hits; falling back to saved raw VLM outputs.")
        cached_outputs, fallback_misses = _load_saved_raw_vlm_map(rows)
        source = "saved_raw_report"
    print(f"VLM outputs ready: {len(cached_outputs)}/{len(rows)} via {source}")
    print(f"Current-cache misses: {misses}")
    if fallback_misses:
        print(f"Saved-raw misses: {fallback_misses}")
    print()

    print("-" * 78)
    print("Test A: randomise user_history_risk for every row (VLM output fixed)")
    print("-" * 78)
    a = test_history_risk_randomised(rows, user_history, cached_outputs)
    print(f"Rows tested:           {a['rows_tested']}")
    print(f"Decisional violations: {len(a['decisional_violations'])}")
    for v in a["decisional_violations"][:10]:
        print(f"  - {v}")
    print(f"supporting_image_ids violations: {len(a['supporting_image_violations'])}")
    for v in a["supporting_image_violations"][:10]:
        print(f"  - {v}")
    print(f"Legitimate risk-flag changes:   {len(a['risk_flag_legitimate_changes'])}")
    print(f"Illegitimate risk-flag changes: {len(a['risk_flag_illegitimate_changes'])}")
    for v in a["risk_flag_illegitimate_changes"][:10]:
        print(f"  - {v}")
    print()

    print("-" * 78)
    print("Test B: permute user_ids (VLM output fixed, history swapped)")
    print("-" * 78)
    b = test_user_id_shuffled(rows, user_history, cached_outputs)
    print(f"Rows tested:           {b['rows_tested']}")
    print(f"Decisional violations: {len(b['decisional_violations'])}")
    for v in b["decisional_violations"][:10]:
        print(f"  - {v}")
    print(f"supporting_image_ids violations: {len(b['supporting_image_violations'])}")
    for v in b["supporting_image_violations"][:10]:
        print(f"  - {v}")
    print(f"Legitimate risk-flag changes:   {len(b['risk_flag_legitimate_changes'])}")
    print(f"Illegitimate risk-flag changes: {len(b['risk_flag_illegitimate_changes'])}")
    for v in b["risk_flag_illegitimate_changes"][:10]:
        print(f"  - {v}")
    print()

    overall_pass = (
        len(a["decisional_violations"]) == 0
        and len(a["supporting_image_violations"]) == 0
        and len(a["risk_flag_illegitimate_changes"]) == 0
        and len(b["decisional_violations"]) == 0
        and len(b["supporting_image_violations"]) == 0
        and len(b["risk_flag_illegitimate_changes"]) == 0
        and (a["rows_tested"] + b["rows_tested"]) > 0
    )

    print("=" * 78)
    print(f"OVERALL: {'PASS' if overall_pass else 'FAIL'}")
    print("=" * 78)

    out = {
        "test_a_history_risk_randomised": a,
        "test_b_user_id_shuffled": b,
        "vlm_output_source": source,
        "cache_misses": misses,
        "saved_raw_misses": fallback_misses,
        "overall_pass": overall_pass,
    }
    report_path = ROOT / "analysis" / "counterfactual_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"Report written to {report_path}")
    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
