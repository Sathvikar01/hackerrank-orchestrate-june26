import re
from typing import Any, Dict, List, Optional, Set

from schema import (
    CLAIM_STATUS,
    ISSUE_TYPES,
    OBJECT_PARTS,
    RISK_FLAGS,
    SEVERITY,
    SEVERITY_ORDER,
)


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1", "t")
    return bool(value)


def normalize_enum(value: Any, allowed: List[str], fallback: Optional[str] = None) -> str:
    if value is None:
        return fallback or allowed[-1]
    s = str(value).strip().lower().replace(" ", "_")
    if s in allowed:
        return s
    # fuzzy: exact match ignoring underscores/hyphens
    for a in allowed:
        if s == a.replace("-", "_").lower():
            return a
    # substring match
    for a in allowed:
        if s in a.lower() or a.lower() in s:
            return a
    return fallback or allowed[-1]


def normalize_severity(value: Any) -> str:
    return normalize_enum(value, SEVERITY, "unknown")


def normalize_supporting_ids(value: Any) -> str:
    if value is None or value == "" or str(value).lower() == "none":
        return "none"
    if isinstance(value, list):
        return ";".join(str(v).strip() for v in value if v)
    return ";".join(str(v).strip() for v in str(value).replace(",", ";").split(";") if v.strip())


def _object_part_fuzzy(part: str, allowed: List[str]) -> str:
    part = part.strip().lower().replace(" ", "_").replace("-", "_")
    if part in allowed:
        return part
    # common aliases
    aliases = {
        "front bumper": "front_bumper",
        "rear bumper": "rear_bumper",
        "back bumper": "rear_bumper",
        "side mirror": "side_mirror",
        "wing mirror": "side_mirror",
        "tail light": "taillight",
        "tail_lamp": "taillight",
        "head light": "headlight",
        "head_lamp": "headlight",
        "wind shield": "windshield",
        "front glass": "windshield",
        "rear glass": "windshield",
        "screen": "screen",
        "display": "screen",
        "track pad": "trackpad",
        "trackpad": "trackpad",
        "key board": "keyboard",
        "package corner": "package_corner",
        "box corner": "package_corner",
        "corner": "package_corner",
        "package side": "package_side",
        "box side": "package_side",
        "package seal": "seal",
        "box seal": "seal",
        "shipping label": "label",
        "package label": "label",
        "package contents": "contents",
        "item": "item",
        "inside item": "item",
    }
    if part in aliases and aliases[part] in allowed:
        return aliases[part]
    for a in allowed:
        if part == a or part in a or a in part:
            return a
    return "unknown"


def normalize_object_part(part: Any, claim_object: str) -> str:
    allowed = OBJECT_PARTS.get(claim_object, ["unknown"])
    if part is None:
        return "unknown"
    s = str(part).strip()
    return _object_part_fuzzy(s, allowed)


def select_primary_issue(visible_issues: List[Dict[str, Any]], claim_object: str) -> Dict[str, str]:
    """Pick the most severe visible issue."""
    if not visible_issues:
        return {"issue_type": "none", "object_part": "unknown", "severity": "none"}

    def score(issue: Dict[str, Any]) -> int:
        sev = normalize_severity(issue.get("severity", "unknown"))
        return SEVERITY_ORDER.get(sev, 1)

    sorted_issues = sorted(visible_issues, key=score, reverse=True)
    primary = sorted_issues[0]
    return {
        "issue_type": normalize_enum(primary.get("issue_type", "unknown"), ISSUE_TYPES, "unknown"),
        "object_part": normalize_object_part(primary.get("object_part", "unknown"), claim_object),
        "severity": normalize_severity(primary.get("severity", "unknown")),
    }


def _normalize_risk_flag(flag: str) -> Optional[str]:
    flag = flag.strip().lower().replace(" ", "_")
    for rf in RISK_FLAGS:
        if flag == rf:
            return rf
        if flag.replace("-", "_") == rf:
            return rf
    return None


def compose_risk_flags(
    vlm_output: Dict[str, Any],
    user_history: Dict[str, Any],
    image_quality_flags: List[str],
    evidence_standard_met: bool,
    valid_image: bool,
) -> str:
    flags: Set[str] = set()

    # Image quality flags from VLM
    for f in image_quality_flags:
        normalized = _normalize_risk_flag(f)
        if normalized and normalized != "none":
            flags.add(normalized)

    # Structural VLM detections
    if normalize_bool(vlm_output.get("claim_mismatch")):
        flags.add("claim_mismatch")
    if normalize_bool(vlm_output.get("wrong_object")):
        flags.add("wrong_object")
    if normalize_bool(vlm_output.get("wrong_object_part")):
        flags.add("wrong_object_part")
    if normalize_bool(vlm_output.get("text_instruction_present")):
        flags.add("text_instruction_present")
    if normalize_bool(vlm_output.get("possible_manipulation")):
        flags.add("possible_manipulation")
    if normalize_bool(vlm_output.get("non_original_image")):
        flags.add("non_original_image")

    # Evidence logic
    if not evidence_standard_met and not flags:
        flags.add("damage_not_visible")

    # User history (never overrides evidence, only flags)
    history_flags = str(user_history.get("history_flags", "none")).strip()
    if "user_history_risk" in history_flags:
        flags.add("user_history_risk")
    if "manual_review_required" in history_flags:
        flags.add("manual_review_required")

    if not flags:
        return "none"
    return ";".join(sorted(flags))


def apply_rules(claim_object: str, vlm_output: Dict[str, Any], user_history: Dict[str, Any]) -> Dict[str, Any]:
    """Take raw VLM output and enforce schema/consistency rules."""
    # Validity
    valid_image = normalize_bool(vlm_output.get("valid_image", True))
    if normalize_bool(vlm_output.get("wrong_object")) or normalize_bool(vlm_output.get("non_original_image")) or normalize_bool(vlm_output.get("possible_manipulation")):
        valid_image = False

    # Evidence sufficiency
    evidence_standard_met = normalize_bool(vlm_output.get("evidence_standard_met", False))
    if not valid_image:
        evidence_standard_met = False

    # Primary issue from VLM visible issues, with fallback to top-level fields
    visible_issues = vlm_output.get("visible_issues", [])
    if not isinstance(visible_issues, list):
        visible_issues = []

    primary = select_primary_issue(visible_issues, claim_object)

    issue_type = normalize_enum(vlm_output.get("issue_type", primary["issue_type"]), ISSUE_TYPES, "unknown")
    object_part = normalize_object_part(vlm_output.get("object_part", primary["object_part"]), claim_object)
    severity = normalize_severity(vlm_output.get("severity", primary["severity"]))

    # If the VLM reported a primary issue but top-level is still unknown, use primary
    if issue_type == "unknown" and primary["issue_type"] != "unknown":
        issue_type = primary["issue_type"]
    if object_part == "unknown" and primary["object_part"] != "unknown":
        object_part = primary["object_part"]
    if severity == "unknown" and primary["severity"] != "unknown":
        severity = primary["severity"]

    # Image quality flags (excluding user history flags)
    raw_quality_flags = vlm_output.get("image_quality_flags", [])
    if not isinstance(raw_quality_flags, list):
        raw_quality_flags = []

    # Claim status consistency
    claim_status = normalize_enum(vlm_output.get("claim_status"), CLAIM_STATUS, "not_enough_information")
    if not valid_image or normalize_bool(vlm_output.get("wrong_object")):
        claim_status = "contradicted"
    elif not evidence_standard_met:
        claim_status = "not_enough_information"
    elif normalize_bool(vlm_output.get("claim_mismatch")):
        claim_status = "contradicted"

    # Supporting image IDs
    supporting_image_ids = normalize_supporting_ids(vlm_output.get("supporting_image_ids"))
    if claim_status == "not_enough_information":
        supporting_image_ids = "none"

    risk_flags = compose_risk_flags(
        vlm_output,
        user_history,
        raw_quality_flags,
        evidence_standard_met,
        valid_image,
    )

    reason = str(vlm_output.get("evidence_standard_met_reason", "")).strip()
    if not reason:
        if evidence_standard_met:
            reason = "The image set is sufficient to evaluate the claim."
        else:
            reason = "The submitted images do not clearly show the claimed object or damage."

    justification = str(vlm_output.get("claim_status_justification", "")).strip()
    if not justification:
        justification = f"Decision: {claim_status}."

    return {
        "evidence_standard_met": str(evidence_standard_met).lower(),
        "evidence_standard_met_reason": reason,
        "risk_flags": risk_flags,
        "issue_type": issue_type,
        "object_part": object_part,
        "claim_status": claim_status,
        "claim_status_justification": justification,
        "supporting_image_ids": supporting_image_ids,
        "valid_image": str(valid_image).lower(),
        "severity": severity,
    }
