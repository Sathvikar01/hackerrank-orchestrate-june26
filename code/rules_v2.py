"""
Rubric-faithful rule engine (Phase 1).

This module exposes :func:`apply_rules_v2`, a deterministic, layer-by-layer
post-processor that replaces the loose ``code.rules.apply_rules`` engine.

It is designed to be importable without any external API keys and without
triggering ``code.config`` (which reads ``.env`` at import time). To make the
``code.*`` package safe to import under a no-key sandbox, dummy API key values
are inserted into ``os.environ`` before any ``code.*`` import.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Set

# --- Env-var guard: prevent code.config from raising on import. -------------
os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")

# --- Make the ``code/`` directory importable without requiring an __init__.py
# file in the repo (the existing code/ layout is not a package). This lets
# ``rules_v2`` be loaded either as ``code.rules_v2`` (when the repo root is on
# sys.path AND code/ has been made a package by another mechanism) or as
# ``rules_v2`` directly (the pattern used by ``code/main.py``).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# --- Imports from sibling code/ modules (no config import required). --------
from rules import (  # noqa: E402  (env-var guard above is intentional)
    compose_risk_flags,
    normalize_bool,
    normalize_enum,
    normalize_object_part,
    normalize_severity,
    normalize_supporting_ids,
    select_primary_issue,
)
from schema import (  # noqa: E402
    CLAIM_STATUS,
    EVIDENCE_REQUIREMENTS_BY_OBJECT,
    ISSUE_TYPES,
    OBJECT_PARTS,
    RISK_FLAGS,
    SEVERITY,
    SEVERITY_ORDER,
)


# ---------------------------------------------------------------------------
# Layer 1: deterministic severity mapping (issue_type [+ object_part] -> sev)
# ---------------------------------------------------------------------------

_BASE_SEVERITY_MAP: Dict[str, str] = {
    "scratch": "low",
    "crack": "medium",          # bounded; never high
    "glass_shatter": "high",    # the only path to high on glass
    "broken_part": "medium",
    "missing_part": "high",
    "torn_packaging": "medium",
    "water_damage": "medium",
    "stain": "medium",
    "none": "none",
    "unknown": "unknown",
    # "dent" and "crushed_packaging" are handled specially (see below)
}

_LOW_DENT_PARTS = {"corner", "quarter_panel", "trackpad"}


def _layer1_severity(issue_type: str, object_part: str, vlm_severity: str,
                     vlm_blurb: str) -> str:
    """Layer 1 deterministic severity derivation."""
    if issue_type == "dent":
        return "low" if object_part in _LOW_DENT_PARTS else "medium"
    if issue_type == "crushed_packaging":
        # Simple heuristic: if the VLM blurb contains "minor", downgrade.
        if vlm_blurb and "minor" in vlm_blurb.lower():
            return "low"
        return "medium"
    if issue_type in _BASE_SEVERITY_MAP:
        return _BASE_SEVERITY_MAP[issue_type]
    # Fall back to the VLM's severity (already normalized elsewhere).
    return vlm_severity


# ---------------------------------------------------------------------------
# Layer 2: visibility / no-damage heuristics
# ---------------------------------------------------------------------------

_NOT_VISIBLE_PHRASES = (
    "not visible", "not in the image", "not in view", "not in frame",
    "out of frame", "out of view", "out of shot", "out of the frame",
    "cannot see", "can't see", "can not see",
    "no clear view", "no view of", "not shown", "cropped", "cropped out",
    "wrong angle", "angle does not show", "no image of",
    "relevant part is not visible",
)

_NO_DAMAGE_PHRASES = (
    "no damage", "no visible damage", "no visible sign", "no sign of damage",
    "no scratch", "no dent", "no crack", "no visible scratch",
    "no visible dent", "no visible crack", "no broken",
    "intact", "clean", "undamaged", "no issues", "no defects",
)


def _text_lower(*candidates: Any) -> str:
    """Concatenate and lower-case candidate strings (skipping Nones)."""
    parts: List[str] = []
    for c in candidates:
        if c is None:
            continue
        if isinstance(c, str):
            parts.append(c)
        else:
            parts.append(str(c))
    return "\n".join(parts).lower()


def _contains_any(haystack: str, needles) -> bool:
    if not haystack:
        return False
    return any(n in haystack for n in needles)


def _visible_issues_concrete(visible_issues: List[Dict[str, Any]]) -> bool:
    """Return True iff visible_issues contains at least one concrete (non-none) entry."""
    if not isinstance(visible_issues, list):
        return False
    for entry in visible_issues:
        if not isinstance(entry, dict):
            continue
        it = str(entry.get("issue_type", "")).strip().lower()
        if it and it not in ("none", "unknown", ""):
            return True
    return False


# ---------------------------------------------------------------------------
# Layer 3: taxonomy correction keywords
# ---------------------------------------------------------------------------

_SHATTER_HINTS = ("shatter", "spider", "radiating", "missing glass", "shattered")
_NO_DEFORM_HINTS = ("surface mark", "no deformation", "no dent",
                    "scratch on", "scratch across", "line on", "line across")
_RESIDUE_HINTS = ("stain", "residue", "discoloration", "discolored")
_WET_HINTS = ("wet", "water pattern", "water stain", "tide line",
              "water mark", "water damage", "soaked", "moisture")


# ---------------------------------------------------------------------------
# Helper: parse a `supporting_image_ids` value into a clean list.
# ---------------------------------------------------------------------------

def _first_image_id(vlm_output: Dict[str, Any], visible_issues: List[Dict[str, Any]]) -> str:
    """Pick a single best supporting image id."""
    # Prefer the first visible_issues entry that has an image_id.
    if isinstance(visible_issues, list):
        for entry in visible_issues:
            if isinstance(entry, dict):
                img = entry.get("image_id") or entry.get("image")
                if img:
                    return str(img).strip()
    # Fallback: the first token from VLM's supporting_image_ids.
    raw = vlm_output.get("supporting_image_ids", "")
    if isinstance(raw, list):
        if raw:
            return str(raw[0]).strip()
    elif isinstance(raw, str) and raw.strip().lower() not in ("", "none"):
        first = raw.replace(",", ";").split(";")[0].strip()
        if first:
            return first
    return "img_1"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def apply_rules_v2(
    claim_object: str,
    vlm_output: dict,
    user_history: dict,
    deterministic_quality_flags: Optional[List[str]] = None,
    *,
    layer_severity: bool = True,
    layer_visibility: bool = True,
    layer_taxonomy: bool = True,
    layer_evidence: bool = True,
    layer_risk_flags: bool = True,
    layer_supporting: bool = True,
) -> dict:
    """Rubric-faithful rule engine.

    Parameters
    ----------
    claim_object:
        One of ``"car"``, ``"laptop"``, ``"package"``.
    vlm_output:
        Raw VLM output dict. Expected keys: ``issue_type``, ``object_part``,
        ``severity``, ``claim_status``, ``claim_status_justification``,
        ``evidence_standard_met``, ``evidence_standard_met_reason``,
        ``supporting_image_ids``, ``valid_image``, ``visible_issues`` (list),
        ``image_quality_flags`` (list), plus the boolean structural flags
        ``claim_mismatch``, ``wrong_object``, ``wrong_object_part``,
        ``text_instruction_present``, ``possible_manipulation``,
        ``non_original_image``.
    user_history:
        User history dict; ``history_flags`` is consumed if present.
    deterministic_quality_flags:
        Optional list of deterministically (OpenCV) detected quality flags.
    layer_*, keyword-only:
        Independently toggle each of the 6 layers (default True).

    Returns
    -------
    dict
        Output dict with the 10 fields ``evidence_standard_met``,
        ``evidence_standard_met_reason``, ``risk_flags``, ``issue_type``,
        ``object_part``, ``claim_status``, ``claim_status_justification``,
        ``supporting_image_ids``, ``valid_image``, ``severity``. Enum values
        are lower-cased strings; booleans are the literal strings
        ``"true"``/``"false"``.
    """
    # ------------------------------------------------------------------
    # Read raw VLM signals up front. We never mutate ``vlm_output``.
    # ------------------------------------------------------------------
    visible_issues = vlm_output.get("visible_issues", []) or []
    if not isinstance(visible_issues, list):
        visible_issues = []
    visible_issues = [e for e in visible_issues if isinstance(e, dict)]

    image_quality_flags = vlm_output.get("image_quality_flags", []) or []
    if not isinstance(image_quality_flags, list):
        image_quality_flags = []

    vlm_blurb = _text_lower(
        vlm_output.get("claim_status_justification"),
        vlm_output.get("evidence_standard_met_reason"),
    )

    # Pull the top-level VLM fields.
    vlm_issue_type = normalize_enum(
        vlm_output.get("issue_type", "unknown"), ISSUE_TYPES, "unknown"
    )
    vlm_object_part = normalize_object_part(
        vlm_output.get("object_part", "unknown"), claim_object
    )
    vlm_severity = normalize_severity(vlm_output.get("severity", "unknown"))
    vlm_claim_status = normalize_enum(
        vlm_output.get("claim_status", "not_enough_information"),
        CLAIM_STATUS,
        "not_enough_information",
    )

    # ------------------------------------------------------------------
    # Track the mutable state through the layers.
    # ------------------------------------------------------------------
    issue_type = vlm_issue_type
    object_part = vlm_object_part
    severity = vlm_severity
    claim_status = vlm_claim_status
    justification = str(vlm_output.get("claim_status_justification", "") or "").strip()
    reason = str(vlm_output.get("evidence_standard_met_reason", "") or "").strip()

    # Booleans from the VLM output.
    vlm_claim_mismatch = normalize_bool(vlm_output.get("claim_mismatch"))
    vlm_wrong_object = normalize_bool(vlm_output.get("wrong_object"))
    vlm_wrong_object_part = normalize_bool(vlm_output.get("wrong_object_part"))
    vlm_possible_manipulation = normalize_bool(vlm_output.get("possible_manipulation"))
    vlm_non_original_image = normalize_bool(vlm_output.get("non_original_image"))
    vlm_text_instruction_present = normalize_bool(vlm_output.get("text_instruction_present"))

    has_concrete_visible_issue = _visible_issues_concrete(visible_issues)

    # ------------------------------------------------------------------
    # Layer 2 — Visibility / no-damage gate (issue_type -> claim_status,
    # evidence, supporting). Run before Layer 1 / 3 because it can collapse
    # issue_type to "none" / "unknown", which makes later layers no-ops.
    # ------------------------------------------------------------------
    if layer_visibility:
        # 1. claim_mismatch + no visible damage -> contradicted (none/none)
        if vlm_claim_mismatch and not has_concrete_visible_issue:
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"
            if not justification:
                justification = (
                    "The user-reported issue is not corroborated by the "
                    "submitted images."
                )

        # 2. explicit none/none -> contradicted
        elif vlm_issue_type == "none" and vlm_severity == "none":
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"

        # 3. unknown/unknown/unknown -> NEI
        elif (vlm_issue_type == "unknown"
              and vlm_severity == "unknown"
              and vlm_object_part == "unknown"):
            claim_status = "not_enough_information"
            issue_type = "unknown"
            severity = "unknown"

        # 4. strong "not visible" language + no concrete visible_issues -> NEI
        elif (_contains_any(vlm_blurb, _NOT_VISIBLE_PHRASES)
              and not has_concrete_visible_issue):
            claim_status = "not_enough_information"
            issue_type = "unknown"
            severity = "unknown"

        # 5. "no damage" language + no concrete visible_issues -> contradicted
        elif (_contains_any(vlm_blurb, _NO_DAMAGE_PHRASES)
              and not has_concrete_visible_issue):
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"

        # 6. Otherwise trust the VLM's claim_status (already normalized).

    # ------------------------------------------------------------------
    # Layer 3 — issue_type taxonomy corrections. Only fire when the VLM
    # actually emitted one of the trigger issue_types.
    # ------------------------------------------------------------------
    if layer_taxonomy:
        # Mirror damage -> broken_part (even when shatter is reported)
        if issue_type == "glass_shatter" and object_part in {"side_mirror", "mirror"}:
            issue_type = "broken_part"

        # Intact-glass single fracture -> crack (NOT shatter)
        if issue_type == "glass_shatter" and not has_concrete_visible_issue:
            # No concrete shatter entry; check VLM blurb for shatter language.
            if not _contains_any(vlm_blurb, _SHATTER_HINTS):
                issue_type = "crack"

        # Surface mark no deformation -> scratch (NOT dent)
        if issue_type == "dent" and _contains_any(vlm_blurb, _NO_DEFORM_HINTS):
            issue_type = "scratch"

        # Residue / stain (no real water pattern) -> stain (NOT water_damage)
        if issue_type == "water_damage" and _contains_any(vlm_blurb, _RESIDUE_HINTS):
            if not _contains_any(vlm_blurb, _WET_HINTS):
                issue_type = "stain"

    # ------------------------------------------------------------------
    # Normalize the (possibly updated) issue_type / object_part again.
    # ------------------------------------------------------------------
    issue_type = normalize_enum(issue_type, ISSUE_TYPES, "unknown")
    object_part = normalize_object_part(object_part, claim_object)

    # ------------------------------------------------------------------
    # Layer 1 — Severity derivation (applied last so the final severity
    # reflects any Layer 2/3 corrections to issue_type).
    # ------------------------------------------------------------------
    if layer_severity:
        severity = _layer1_severity(
            issue_type=issue_type,
            object_part=object_part,
            vlm_severity=vlm_severity,
            vlm_blurb=vlm_blurb,
        )
    severity = normalize_severity(severity)

    # ------------------------------------------------------------------
    # Enforce NEI invariants on the other fields.
    # ------------------------------------------------------------------
    valid_image = normalize_bool(vlm_output.get("valid_image", True))
    if vlm_wrong_object or vlm_non_original_image or vlm_possible_manipulation:
        valid_image = False

    if claim_status == "not_enough_information":
        issue_type = "unknown"
        severity = "unknown"
        if object_part == "unknown":
            # Keep the claimed part from user_history / user_claim if we can
            # find it; otherwise leave as "unknown".
            claimed = (user_history or {}).get("claimed_part") if isinstance(user_history, dict) else None
            if claimed:
                object_part = normalize_object_part(claimed, claim_object)
        if not justification:
            justification = "The relevant part is not visible in the submitted images."

    # If we are contradicted AND have no concrete visible_issue AND the VLM
    # didn't report a concrete issue at the top level, collapse to none/none.
    if (claim_status == "contradicted"
            and not has_concrete_visible_issue
            and vlm_issue_type in ("none", "unknown")):
        issue_type = "none"
        severity = "none"

    # ------------------------------------------------------------------
    # Layer 4 — evidence_standard_met invariant (decoupled from valid_image).
    # ------------------------------------------------------------------
    if layer_evidence:
        if claim_status in ("supported", "contradicted"):
            evidence_standard_met = True
        elif claim_status == "not_enough_information":
            evidence_standard_met = False
        else:
            evidence_standard_met = False
    else:
        # Fall back to the VLM's value (normalized to bool).
        evidence_standard_met = normalize_bool(
            vlm_output.get("evidence_standard_met", False)
        )

    # Per the rubric: if valid_image is False due to non_original /
    # possible_manipulation, claim_status flips to contradicted and evidence
    # remains True (risk_flags warn, they don't flip status off
    # supported/contradicted).
    if valid_image is False:
        if claim_status not in ("contradicted",):
            claim_status = "contradicted"
        evidence_standard_met = True

    # ------------------------------------------------------------------
    # Layer 6 — supporting_image_ids policy (do this before Layer 5 so we
    # have the final supporting_image_ids available for the risk-flag
    # composition if any downstream code needs it).
    # ------------------------------------------------------------------
    if layer_supporting:
        if claim_status == "not_enough_information" or valid_image is False:
            supporting_image_ids = "none"
        else:
            supporting_image_ids = _first_image_id(vlm_output, visible_issues)
    else:
        supporting_image_ids = normalize_supporting_ids(
            vlm_output.get("supporting_image_ids")
        )

    # ------------------------------------------------------------------
    # Layer 5 — risk_flags.
    # ------------------------------------------------------------------
    if layer_risk_flags:
        # Start from the existing helper so we keep its image-quality-flag,
        # structural-flag, and history-flag logic.
        risk_flags = compose_risk_flags(
            vlm_output,
            user_history if isinstance(user_history, dict) else {},
            image_quality_flags,
            evidence_standard_met,
            valid_image,
            deterministic_quality_flags=deterministic_quality_flags,
        )
        flag_set: Set[str] = set()
        if risk_flags and risk_flags.strip().lower() != "none":
            for f in str(risk_flags).split(";"):
                f = f.strip()
                if f and f.lower() != "none":
                    flag_set.add(f)

        # manual_review_required: when any high-risk signal co-occurs.
        high_risk_signals = {
            "claim_mismatch",
            "non_original_image",
            "possible_manipulation",
            "damage_not_visible",
            "user_history_risk",
            "wrong_object",
        }
        if flag_set & high_risk_signals and "manual_review_required" not in flag_set:
            flag_set.add("manual_review_required")

        # damage_not_visible: NEI without an existing flag.
        if claim_status == "not_enough_information" and "damage_not_visible" not in flag_set:
            flag_set.add("damage_not_visible")

        # damage_not_visible: visible part but no concrete damage and not NEI.
        if (issue_type in ("none", "unknown")
                and claim_status != "not_enough_information"
                and "damage_not_visible" not in flag_set):
            flag_set.add("damage_not_visible")

        # Gate wrong_object_part: keep only if the VLM explicitly set it AND
        # the predicted object_part is unknown or clearly mismatches a
        # visible_issues entry.
        if "wrong_object_part" in flag_set:
            keep = False
            if vlm_wrong_object_part and object_part == "unknown":
                keep = True
            else:
                # Check for a real part mismatch against visible_issues.
                for entry in visible_issues:
                    if not isinstance(entry, dict):
                        continue
                    entry_part = normalize_object_part(
                        entry.get("object_part", "unknown"), claim_object
                    )
                    if (entry_part not in ("unknown", "")
                            and entry_part != object_part):
                        keep = True
                        break
            if not keep:
                flag_set.discard("wrong_object_part")

        # Text-instruction co-occurrence: if the VLM flagged
        # text_instruction_present, ensure manual_review_required is present.
        if vlm_text_instruction_present and "text_instruction_present" not in flag_set:
            flag_set.add("text_instruction_present")
        if (vlm_text_instruction_present
                and flag_set
                and "manual_review_required" not in flag_set):
            flag_set.add("manual_review_required")

        risk_flags = ";".join(sorted(flag_set)) if flag_set else "none"
    else:
        risk_flags = compose_risk_flags(
            vlm_output,
            user_history if isinstance(user_history, dict) else {},
            image_quality_flags,
            evidence_standard_met,
            valid_image,
            deterministic_quality_flags=deterministic_quality_flags,
        )

    # ------------------------------------------------------------------
    # Final normalization + default reasons.
    # ------------------------------------------------------------------
    issue_type = normalize_enum(issue_type, ISSUE_TYPES, "unknown")
    object_part = normalize_object_part(object_part, claim_object)
    severity = normalize_severity(severity)
    claim_status = normalize_enum(claim_status, CLAIM_STATUS, "not_enough_information")
    supporting_image_ids = normalize_supporting_ids(supporting_image_ids)

    if not reason:
        if evidence_standard_met:
            reason = "The image set is sufficient to evaluate the claim."
        else:
            reason = "The submitted images do not clearly show the claimed object or damage."

    if not justification:
        if claim_status == "supported":
            justification = "The visible evidence matches the claim."
        elif claim_status == "contradicted":
            justification = "The visible evidence does not match the claim."
        else:
            justification = "The relevant part is not visible in the submitted images."

    return {
        "evidence_standard_met": str(bool(evidence_standard_met)).lower(),
        "evidence_standard_met_reason": reason,
        "risk_flags": risk_flags,
        "issue_type": issue_type,
        "object_part": object_part,
        "claim_status": claim_status,
        "claim_status_justification": justification,
        "supporting_image_ids": supporting_image_ids,
        "valid_image": str(bool(valid_image)).lower(),
        "severity": severity,
    }
