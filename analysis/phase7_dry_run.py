"""
Synthetic adversarial scenarios for the packaged submission.

Builds a small set of edge-case claims that exercise:
  * missing_part NEI gate
  * wrong_object contradicted
  * clean support
  * prompt-injection attempt (text_instruction_present=true)
  * hallucinated generic-claim (rationalization phrase in blurb)
  * invisible-part NEI
  * contradicted citation policy (multi-image)
  * dent -> scratch downgrade (positive scratch descriptor)
  * defensive visibility guard (supported + unknown issue_type)

For each scenario, drive apply_rules_v2 directly with a hand-crafted
vlm_output dict and assert the resulting claim_status / issue_type /
severity / supporting_image_ids / valid_image / evidence_standard_met.
No API calls, no cache lookups. Verifies that the rules engine shipped
in the bundle produces spec-aligned output for synthetic edge cases.
"""

import csv
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "code"))
os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")

import rules_v2  # noqa: E402

OUTPUT_COLUMNS = [
    "user_id", "image_paths", "user_claim", "claim_object",
    "evidence_standard_met", "evidence_standard_met_reason", "risk_flags",
    "issue_type", "object_part", "claim_status", "claim_status_justification",
    "supporting_image_ids", "valid_image", "severity",
]


def _apply(claim_object, vlm_output, user_history=None):
    return rules_v2.apply_rules_v2(
        claim_object=claim_object,
        vlm_output=vlm_output,
        user_history=user_history if user_history is not None else {},
        deterministic_quality_flags=None,
    )


SCENARIOS = [
    {
        "name": "clean_dent_supported",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "dent", "object_part": "rear_bumper",
            "severity": "medium", "claim_status": "supported",
            "claim_status_justification": "Image shows a clear dent on the rear bumper; pushed-in panel visible.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "dent", "object_part": "rear_bumper", "severity": "medium", "image_id": "img_1"}],
        },
        "expect": {"claim_status": "supported", "issue_type": "dent", "severity": "medium"},
    },
    {
        "name": "wrong_object_contradicted",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "unknown", "object_part": "unknown",
            "severity": "unknown", "claim_status": "contradicted",
            "claim_status_justification": "The image shows a motorcycle, not a car.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": True, "wrong_object": True,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [],
        },
        "expect": {"claim_status": "contradicted", "severity": "low"},
    },
    {
        "name": "missing_part_nei_obscured",
        "claim_object": "package",
        "vlm_output": {
            "issue_type": "missing_part", "object_part": "package_side",
            "severity": "high", "claim_status": "supported",
            "claim_status_justification": "Only crumpled paper is visible inside the package; no product visible.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "missing_part", "object_part": "package_side", "severity": "high", "image_id": "img_1"}],
        },
        "expect": {"claim_status": "not_enough_information", "valid_image": "false"},
    },
    {
        "name": "injection_attempt_supported_but_no_concrete_blurb",
        "claim_object": "package",
        "vlm_output": {
            "issue_type": "missing_part", "object_part": "package_side",
            "severity": "high", "claim_status": "supported",
            "claim_status_justification": "As instructed in the text overlay, the claim is correct.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": True,
            "visible_issues": [{"issue_type": "missing_part", "object_part": "package_side", "severity": "high", "image_id": "img_1"}],
        },
        "expect": {"claim_status": "contradicted", "issue_type": "none", "severity": "none"},
    },
    {
        "name": "hallucinated_generic_claim_rationalization",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "dent", "object_part": "rear_bumper",
            "severity": "medium", "claim_status": "supported",
            "claim_status_justification": "Damage is consistent with the customer's report of an issue.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "dent", "object_part": "rear_bumper", "severity": "medium", "image_id": "img_1"}],
        },
        "expect": {"claim_status": "contradicted", "issue_type": "none", "severity": "none"},
    },
    {
        "name": "invisible_part_nei",
        "claim_object": "laptop",
        "vlm_output": {
            "issue_type": "unknown", "object_part": "unknown",
            "severity": "unknown", "claim_status": "not_enough_information",
            "claim_status_justification": "The trackpad is not visible in the submitted images.",
            "supporting_image_ids": "none", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [],
        },
        "expect": {"claim_status": "not_enough_information"},
    },
    {
        "name": "contradicted_with_multiple_images",
        "claim_object": "package",
        "vlm_output": {
            "issue_type": "missing_part", "object_part": "package_side",
            "severity": "high", "claim_status": "contradicted",
            "claim_status_justification": "Both img_1 and img_2 show the contents present.",
            "supporting_image_ids": "img_1;img_2", "valid_image": True,
            "claim_mismatch": True, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "missing_part", "object_part": "package_side", "severity": "high", "image_id": "img_1"}],
        },
        "expect": {"claim_status": "contradicted", "supporting_image_ids": "img_1;img_2"},
    },
    {
        "name": "dent_downgraded_to_scratch_positive_signal",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "dent", "object_part": "quarter_panel",
            "severity": "medium", "claim_status": "contradicted",
            "claim_status_justification": "The visible mark is a scratch on the surface, not a deformation.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": True, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "dent", "object_part": "quarter_panel", "severity": "medium", "image_id": "img_1"}],
        },
        "expect": {"issue_type": "scratch"},
    },
    {
        "name": "defensive_visibility_guard_supported_unknown",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "unknown", "object_part": "unknown",
            "severity": "unknown", "claim_status": "supported",
            "claim_status_justification": "Damage matches the user's claim.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [],
        },
        "expect": {"claim_status": "not_enough_information"},
    },
    {
        "name": "history_risk_adds_only_manual_review_flag",
        "claim_object": "car",
        "vlm_output": {
            "issue_type": "dent", "object_part": "rear_bumper",
            "severity": "medium", "claim_status": "supported",
            "claim_status_justification": "Image shows a dent on the rear bumper; pushed-in panel visible.",
            "supporting_image_ids": "img_1", "valid_image": True,
            "claim_mismatch": False, "wrong_object": False,
            "wrong_object_part": False, "possible_manipulation": False,
            "non_original_image": False, "text_instruction_present": False,
            "visible_issues": [{"issue_type": "dent", "object_part": "rear_bumper", "severity": "medium", "image_id": "img_1"}],
        },
        "user_history": {"history_flags": "user_history_risk", "history_summary": "x"},
        "expect": {"claim_status": "supported", "issue_type": "dent", "severity": "medium"},
    },
]


def main():
    print("=" * 78)
    print("Phase 7 dry-run: synthetic adversarial scenarios against packaged rules")
    print("=" * 78)
    passed = 0
    failed = 0
    failures = []
    out_dir = Path(os.path.dirname(__file__)).resolve()
    rows_for_csv = []

    for sc in SCENARIOS:
        name = sc["name"]
        uh = sc.get("user_history", {"history_flags": "none", "history_summary": "low-risk"})
        try:
            res = _apply(sc["claim_object"], sc["vlm_output"], uh)
        except Exception as e:
            failed += 1
            failures.append({"name": name, "error": f"exception {type(e).__name__}: {e}", "trace": traceback.format_exc()})
            continue
        ok = True
        details = []
        for k, v in sc["expect"].items():
            actual = res.get(k)
            if str(actual) != str(v):
                ok = False
                details.append(f"{k}: expected={v!r} actual={actual!r}")
        # Schema validity
        for col in ["evidence_standard_met", "valid_image"]:
            if res[col] not in ("true", "false"):
                ok = False
                details.append(f"{col}: not a boolean string ({res[col]!r})")
        if ok:
            passed += 1
            print(f"PASS  {name}")
        else:
            failed += 1
            failures.append({"name": name, "details": details, "actual": res})
            print(f"FAIL  {name}: {'; '.join(details)}")

        # Build output-shaped row
        rows_for_csv.append({
            "user_id": f"synthetic_{name}",
            "image_paths": sc["vlm_output"].get("supporting_image_ids", "img_1"),
            "user_claim": "",
            "claim_object": sc["claim_object"],
            "evidence_standard_met": res["evidence_standard_met"],
            "evidence_standard_met_reason": res["evidence_standard_met_reason"],
            "risk_flags": res["risk_flags"],
            "issue_type": res["issue_type"],
            "object_part": res["object_part"],
            "claim_status": res["claim_status"],
            "claim_status_justification": res["claim_status_justification"],
            "supporting_image_ids": res["supporting_image_ids"],
            "valid_image": res["valid_image"],
            "severity": res["severity"],
        })

    # Write a CSV with the synthetic outputs.
    csv_path = out_dir / "phase7_synthetic_output.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        w.writeheader()
        w.writerows(rows_for_csv)

    print()
    print("=" * 78)
    print(f"Synthetic dry-run: {passed} passed, {failed} failed")
    print(f"CSV: {csv_path}")
    if failures:
        print()
        print("Failures:")
        for f in failures:
            print(json.dumps(f, indent=2, ensure_ascii=False))
    print("=" * 78)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())