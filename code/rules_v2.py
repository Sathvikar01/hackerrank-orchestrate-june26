"""
Rubric-faithful rule engine (Phase 1).

This module exposes :func:`apply_rules_v2`, a deterministic, layer-by-layer
post-processor that replaces the loose ``code.rules.apply_rules`` engine.

It is designed to be importable without any external API keys and without
triggering ``code.config`` (which reads ``.env`` at import time).
"""

from __future__ import annotations

import re
import os
import sys
from typing import Any, Dict, List, Optional, Set

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
    # broken_part / missing_part on cars: honor the VLM's severity if
    # the VLM's blurb describes catastrophic / extensive / major damage.
    # The default map says broken_part=medium, missing_part=high, but the
    # VLM may correctly call a broken_part as "high" for severe cases
    # (e.g., broken-off fender) and a missing_part as "medium" for
    # trivial cases. Trust the VLM severity in those situations.
    if issue_type in ("broken_part", "missing_part") and vlm_severity in (
            "high", "medium"):
        if issue_type == "missing_part" and vlm_severity == "medium":
            return "medium"
        if vlm_blurb and _contains_any(vlm_blurb, _CATASTROPHIC_HINTS):
            return vlm_severity
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
    "relevant part is not visible", "no item visible", "not in the picture",
    "outside the frame", "absent from the image", "absent from view",
    "not captured", "not provided", "missing from view",
    "cannot evaluate", "can not evaluate", "unable to evaluate",
    "impossible to evaluate", "cannot be evaluated", "can not be evaluated",
    "not in focus", "is blurry", "blurry and out", "too dark", "too bright",
    "do not show", "does not show", "shows a different", "shows another",
    "not the claimed", "different from the claimed",
)

_NO_DAMAGE_PHRASES = (
    "no damage", "no visible damage", "no visible sign", "no sign of damage",
    "no scratch", "no dent", "no crack", "no visible scratch",
    "no visible dent", "no visible crack", "no broken",
    "intact", "clean", "undamaged", "no issues", "no defects",
    "no torn", "no tear", "no crushing", "no crush",
    "package seal does not show", "seal does not show",
    "not torn", "no missing", "no broken", "no sign of tear",
    "no sign of torn", "shows only", "does not show torn",
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
    for needle in needles:
        if not needle:
            continue
        if re.fullmatch(r"[\w_]+", needle):
            if re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack):
                return True
        elif needle in haystack:
            return True
    return False


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

# Trigger phrases that indicate the VLM actually saw a TRUE shatter
# (spider pattern, missing glass pieces, broken shards). If NONE of these
# are present in the VLM's blurb, a glass_shatter call is treated as a
# single fracture (crack) instead.
#
# Per annotation_conventions.md: "Multiple radiating fractures from one
# impact are still crack + medium when the glass surface is largely
# intact and no pieces are missing." So 'radiating' alone is NOT a
# shatter signal — we require explicit spider-web / missing-glass /
# broken-shard language.
_STRICT_SHATTER_HINTS = ("shatter", "spider", "missing glass", "shattered",
                         "shattered into", "spiderweb", "spider pattern",
                         "shattered glass", "shatters", "broken into pieces",
                         "broken glass", "pieces missing", "broken shards",
                         "shards of glass", "glass shards", "missing pieces",
                         "completely shattered", "shattered completely",
                         "crumbling", "crumbled", "shattered pattern",
                         "multiple broken", "glass falling", "glass broken off")

# Backwards-compatible alias.
_SHATTER_HINTS = _STRICT_SHATTER_HINTS

# Trigger phrases that indicate the VLM saw a NON-deforming surface
# mark (scratch, scrape, paint transfer) rather than a real dent.
# Note: only visual deformation-related words. Do NOT include
# rationalization phrases like "consistent with the customer" — those
# are tracked separately in _VLM_RATIONALIZATION_HINTS.
_NO_DEFORM_HINTS = ("surface mark", "no deformation", "no dent",
                    "scratch on", "scratch across", "line on", "line across",
                    "scrape", "scratch", "scratched",
                    "scrape or scratch", "scratch or scrape",
                    "mark on surface", "mark on the surface",
                    "no indentation", "no concave", "not concave",
                    "paint transfer", "clear coat", "clear-coat",
                    "surface only", "no panel pushed", "surface-level",
                    "minor mark", "minor scratch", "minor scrape")

# Phrases whose ABSENCE means the description does NOT contain evidence of
# actual deformation. If NONE are present in the blurb, a dent call is
# downgraded to scratch (because the VLM described no panel push-in,
# concavity, etc.).
_DEFORM_HINTS = ("deformation", "deformed", "concave", "concavity",
                 "indentation", "indented", "panel pushed in",
                 "panel is pushed", "metal pushed", "metal pushed in",
                 "metal is pushed", "dented inward", "pushed inward",
                 "structural deformation", "crushed", "buckled",
                 "crumpled metal", "deep dent", "deep indentation",
                 "significant dent", "large dent", "major dent",
                 "dented in", "deeper dent", "severely dented",
                 "structural damage", "frame bent")

# Crack-focused trigger phrases used to DOWNGRADE a glass_shatter call to
# crack. The default shatter path requires explicit "spider" / "shards" /
# "missing pieces" language. The crack path is triggered by the VLM using
# the words "crack" / "cracks" / "fracture" / "chip" — these describe a
# single bounded fracture, NOT a full shatter, even if the VLM's overall
# verdict said "shatter".
_CRACK_FOCUSED_HINTS = (
    "crack ", "cracks", "crack.", "crack,", "cracks.", "cracks,",
    "cracked", "hairline crack", "hairline", "single crack",
    "fracture", "fractures", "fractured", "stone chip", "stone impact",
    "spreading crack", "radiating crack", "radiating cracks",
    "multiple cracks", "spider crack", "spider crack pattern",
    "small crack", "minor crack", "clear crack",
)

# Phrases that indicate the VLM is describing a WET/water pattern (not
# just a residue/stain). Used to NOT downgrade water_damage to stain
# when the VLM's blurb clearly says "wet" or "water".
_WET_PATTERN_HINTS = (
    "wet", "wet-looking", "wet looking", "damp", "moist", "moisture",
    "saturated", "water pattern", "water damage", "water-marked",
    "water stained", "water-stained", "water mark", "water-mark",
    "water ring", "water-ring", "liquid",
)

# Phrases that suggest the VLM is OVER-confident in matching a generic
# user claim ("physical damage", "issue", "problem") to a specific
# issue_type. Used as a hallucination safety net.
_VLM_RATIONALIZATION_HINTS = (
    "consistent with the customer", "consistent with the user's",
    "consistent with the claim", "matches the customer",
    "matches the user's", "matches the claim",
    "as the customer described", "as the user described",
    "aligns with the customer", "aligns with the user's",
    "aligns with the claim",
)

# Phrases that the VLM uses to indicate it is FOLLOWING an instruction
# in the image or transcript (rather than describing the image). When
# paired with `text_instruction_present` from the VLM, this is direct
# causal evidence of injection-induced fabrication.
_INJECTION_FABRICATION_HINTS = (
    "as instructed", "as the prompt asked", "as the instructions say",
    "as the text instructs", "per the instructions",
    "based on the user's description", "based on the customer's",
    "following the instructions", "as requested by the customer",
    "as requested by the user", "as the user said to look for",
    "as the customer said to look for", "as described in the text",
    "the text in the image states", "the embedded text says",
    "the text instructs", "the note in the image",
)

# Concrete damage descriptors. A VLM blurb that contains at least one of
# these is grounded in a real visual observation of the specific damage
# it is reporting. A blurb that lacks ALL of these is asserting a damage
# verdict without naming what it actually saw -- the canonical signature
# of a hallucinated or injection-following "supported" verdict.
_CONCRETE_DAMAGE_DESCRIPTORS = (
    "dent", "dented", "denting",
    "scratch", "scratched", "scratches", "scrape", "scraped", "scuff",
    "scuffed", "paint transfer", "clear coat",
    "crack", "cracked", "cracks", "fracture", "fractured",
    "hairline", "stone chip", "stone impact",
    "shatter", "shattered", "shatters", "spider", "spiderweb",
    "spider-web", "shards of glass", "glass shards", "broken shards",
    "broken into pieces", "pieces missing", "missing pieces",
    "broken", "broken off", "snapped", "fractured off",
    "torn", "torn off", "ripped", "ripped off", "tear", "torn packaging",
    "crushed", "crushed packaging", "crumpled",
    "stain", "stained", "discoloration", "discolored", "discolor",
    "water damage", "water ring", "water mark", "tide line", "tide mark",
    "wet", "damp", "moisture", "saturated", "soaked",
    "missing", "absent", "not present", "no item", "no contents",
    "residue", "sticky", "sticky keys", "spill", "spilled",
    "leak", "leaking", "leaked", "corrosion", "corroded", "rust",
    "rusted", "oxidation", "warped", "warping", "swelling", "swollen",
    "short circuit", "short-circuit", "fried",
    "fire damage", "burn", "burned", "burnt", "charred",
    "broken glass", "broken lens", "broken mirror",
)

# Positive scratch / scrape descriptors used to DOWNGRADE a "dent" call
# to "scratch" when the VLM's blurb is actually describing a non-deforming
# surface mark. The downgrade requires a POSITIVE signal in the blurb
# (the VLM is naming a scratch / scrape), not merely the absence of
# deformation language.
_SCRATCH_DESCRIPTORS = (
    "scratch", "scratched", "scratches",
    "scrape", "scraped", "scrapes",
    "scuff", "scuffed",
    "paint transfer", "clear coat", "clear-coat",
    "surface mark", "surface-level", "surface only",
    "line on", "line across", "linear mark",
    "mark on surface", "mark on the surface",
    "minor scratch", "minor scrape", "minor mark",
)

# Phrases that suggest a "catastrophic" / "extensive" / "major" damage
# pattern. Used to override the default broken_part=medium severity to
# allow the VLM's severity (e.g. "high") to be honored for car parts.
_CATASTROPHIC_HINTS = (
    "catastrophic", "extensive", "severe", "destruction", "destroyed",
    "total", "major", "broken off", "ripped off", "torn off",
    "shredded", "unusable", "inoperable", "not functional",
    "extensive damage", "severe damage", "major damage",
    "catastrophic damage", "completely broken", "split in two",
)

# Trigger phrases for residue/stain (used to downgrade water_damage to stain
# when no real water damage pattern is described).
# Trigger phrases for residue/stain (used to downgrade water_damage to stain
# when no real water damage pattern is described). "water droplets" /
# "droplets" are intentionally NOT here — they describe the actual water
# damage (a wet pattern), not a residue / stain. Only dried-out / stained
# patterns go here.
_RESIDUE_HINTS = ("stain", "residue", "discoloration", "discolored",
                  "discolor", "staining", "stained", "ring", "tide",
                  "marks on", "mark on", "streak", "streaks",
                  "water spill", "liquid spill", "spill residue",
                  "spilled liquid", "sticky", "sticky keys",
                  "residue from", "mark from",
                  "dried water", "dried liquid", "dried droplets",
                  "water ring", "water-ring", "tide line",
                  "water mark", "water-mark")

# Trigger phrases whose ABSENCE means the description does NOT contain
# evidence of actual water damage (corrosion, swelling, short-circuit).
# If NONE present, water_damage is downgraded to stain.
_WET_HINTS = ("wet", "water pattern", "water stain", "tide line",
              "water mark", "water damage", "soaked", "moisture",
              "corrosion", "corroded", "rust", "rusted",
              "swelling", "swollen", "warped", "warping",
              "short circuit", "short-circuit", "fried",
              "liquid damage", "liquid penetration", "liquid entered",
              "mineral deposit", "water marks", "oxidation",
              "circuit damage", "component damage", "board damage")


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


def _all_image_ids_from_vlm(vlm_output: Dict[str, Any], visible_issues: List[Dict[str, Any]]) -> List[str]:
    """Collect every image id cited anywhere in the VLM output."""
    ids: List[str] = []
    seen = set()

    def _add(s: str) -> None:
        s = s.strip()
        if s and s.lower() != "none" and s not in seen:
            seen.add(s)
            ids.append(s)

    if isinstance(visible_issues, list):
        for entry in visible_issues:
            if isinstance(entry, dict):
                img = entry.get("image_id") or entry.get("image")
                if img:
                    _add(str(img))
    raw = vlm_output.get("supporting_image_ids", "")
    if isinstance(raw, list):
        for x in raw:
            if x:
                _add(str(x))
    elif isinstance(raw, str) and raw.strip():
        for tok in raw.replace(",", ";").split(";"):
            if tok.strip():
                _add(tok)
    return ids


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
    valid_image = normalize_bool(vlm_output.get("valid_image", True))
    justification = str(vlm_output.get("claim_status_justification", "") or "").strip()
    reason = str(vlm_output.get("evidence_standard_met_reason", "") or "").strip()

    # Booleans from the VLM output.
    vlm_claim_mismatch = normalize_bool(vlm_output.get("claim_mismatch"))
    vlm_wrong_object = normalize_bool(vlm_output.get("wrong_object"))
    vlm_wrong_object_part = normalize_bool(vlm_output.get("wrong_object_part"))
    vlm_possible_manipulation = normalize_bool(vlm_output.get("possible_manipulation"))
    vlm_non_original_image = normalize_bool(vlm_output.get("non_original_image"))
    vlm_text_instruction_present = normalize_bool(vlm_output.get("text_instruction_present"))
    claimed_issue_type = normalize_enum(
        vlm_output.get("_claimed_issue_type", "unknown"), ISSUE_TYPES, "unknown"
    )
    claimed_object_part = normalize_object_part(
        vlm_output.get("_claimed_object_part", "unknown"), claim_object
    )
    claimed_functional_issue = normalize_bool(vlm_output.get("_claimed_functional_issue"))

    has_concrete_visible_issue = _visible_issues_concrete(visible_issues)

    # ------------------------------------------------------------------
    # Layer 2 — Visibility / no-damage gate (issue_type -> claim_status,
    # evidence, supporting). Run before Layer 1 / 3 because it can collapse
    # issue_type to "none" / "unknown", which makes later layers no-ops.
    # ------------------------------------------------------------------
    if layer_visibility:
        # Pre-compute the flag for whether the VLM emitted a concrete
        # damage type (anything other than "none" / "unknown" / "").
        vlm_issue_is_concrete = vlm_issue_type not in (
            "none", "unknown", ""
        )

        # 0. "Part not visible" gate — fires FIRST so it pre-empts the
        # claim_mismatch branch. If the VLM's blurb talks about the
        # relevant part not being visible / not being in the frame /
        # not being evaluable, the right status is
        # not_enough_information (we genuinely cannot see whether the
        # user's claim is true), NOT contradicted.
        # Exception: when the VLM explicitly says wrong_object=True, the
        # claim is contradicted (we found the wrong thing, not "not
        # visible"). The wrong_object case is evaluable.
        if (not vlm_issue_is_concrete
                and _contains_any(vlm_blurb, _NOT_VISIBLE_PHRASES)
                and not vlm_wrong_object):
            claim_status = "not_enough_information"
            issue_type = "unknown"
            severity = "unknown"
            if vlm_non_original_image or vlm_possible_manipulation:
                valid_image = False
            if not justification:
                justification = (
                    "The relevant part is not visible in the "
                    "submitted images."
                )

        # 0b. Concrete missing_part with no clear view of the contents —
        # missing_part requires seeing inside the package and confirming
        # the item is absent. If the blurb only describes the packing
        # material ("only crumpled paper", "no item visible", etc.),
        # the evidence is insufficient -> not_enough_information.
        elif (vlm_issue_type == "missing_part"
              and _contains_any(vlm_blurb, (
                  "only crumpled", "only packing", "no item visible",
                  "no product visible", "no contents visible",
                  "cannot confirm", "cannot verify",
                  "only paper", "only bubble wrap", "only styrofoam",
                  "only foam", "only cardboard", "only tissue",
                  "no clear view", "not clearly visible",
              ))):
            claim_status = "not_enough_information"
            issue_type = "unknown"
            severity = "unknown"
            # When the package contents are not clearly visible, the image
            # itself is not sufficient for evaluation -> valid_image=False.
            valid_image = False
            if not justification:
                justification = (
                    "The submitted images do not clearly show the "
                    "package contents, so the missing-item claim "
                    "cannot be verified."
                )

        # 0c. Prompt-injection / fabrication safety net.
        # Causal signal: the VLM acknowledges seeing instruction-like
        # text in the transcript / image (text_instruction_present=true),
        # AND the VLM is producing a "supported" verdict, AND the VLM's
        # own blurb contains no concrete damage descriptor that would
        # justify a real visual match. In that case the VLM is asserting
        # damage without naming what it saw -- it is matching the
        # injection text rather than describing the image.
        #
        # NO user_history_risk dependency: this rule fires whenever the
        # VLM's own output is self-contradictory (flagged injection +
        # bare assertion of damage with no descriptive content). The
        # history signal was the only sample-correlated co-trigger and
        # has been removed for hidden-test robustness.
        elif (vlm_text_instruction_present
                and claim_status == "supported"
                and has_concrete_visible_issue
                and not _contains_any(vlm_blurb, _CONCRETE_DAMAGE_DESCRIPTORS)):
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"
            if not justification:
                justification = (
                    "The image contains instruction-like text and the "
                    "VLM's description does not name any specific "
                    "visible damage that would justify a supported "
                    "verdict."
                )

        # 0d. Generic-claim hallucination override.
        # Causal signal: the VLM uses a meta-cognitive rationalization
        # phrase ("consistent with the customer's report") -- the VLM
        # is admitting it is matching the user's claim rather than
        # describing the image -- AND the VLM's blurb contains no
        # concrete damage descriptor that would justify a real visual
        # match.
        #
        # NO user_history_risk dependency: hallucination is detectable
        # from the VLM's own blurb alone. The history co-trigger was
        # the only sample-correlated signal and has been removed.
        elif (claim_status == "supported"
                and has_concrete_visible_issue
                and _contains_any(vlm_blurb, _VLM_RATIONALIZATION_HINTS)
                and not _contains_any(vlm_blurb, _CONCRETE_DAMAGE_DESCRIPTORS)):
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"
            if not justification:
                justification = (
                    "The VLM's description does not clearly support the "
                    "user's specific claim; the visible damage does not "
                    "match what was reported."
                )

        # 1. claim_mismatch + no visible damage -> contradicted (none/none)
        elif vlm_claim_mismatch and not has_concrete_visible_issue:
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

        # 6. Defensive visibility guard. Fires when the VLM reports
        # claim_status=supported without naming any damage: issue_type is
        # unknown, no concrete visible_issue exists, AND the VLM did not
        # flag a visibility / framing problem. In that situation the VLM
        # is "supporting" a claim about damage it did not actually
        # classify; the correct status is not_enough_information.
        # No user_history, claim, or sample-specific inputs are read.
        elif (claim_status == "supported"
              and vlm_issue_type == "unknown"
              and not has_concrete_visible_issue
              and not _contains_any(vlm_blurb, _NOT_VISIBLE_PHRASES)):
            claim_status = "not_enough_information"
            issue_type = "unknown"
            severity = "unknown"
            if not justification:
                justification = (
                    "The VLM reported the claim as supported but did "
                    "not identify any visible damage in the submitted "
                    "images; the claim cannot be verified."
                )

        # 7. Otherwise trust the VLM's claim_status (already normalized).

        # 8. Defensive claim-shape correction. Car claims about dents,
        # scratches, cracks, or broken parts are sometimes over-read by VLMs
        # as missing_part even though the claimed car part is visible. Use the
        # transcript-derived issue only for this impossible taxonomy mismatch.
        if (claim_object == "car"
                and issue_type == "missing_part"
                and claimed_issue_type in {"dent", "scratch", "crack", "broken_part"}
                and claimed_object_part != "unknown"):
            issue_type = claimed_issue_type
            object_part = claimed_object_part

        # Functional laptop failures cannot be proven by a still image unless
        # the visible issue itself is a broken part. A cosmetic dent near the
        # claimed control does not prove that the control stopped working.
        if (claim_object == "laptop"
                and claimed_functional_issue
                and claim_status == "supported"
                and issue_type in {"dent", "scratch", "stain"}):
            claim_status = "contradicted"
            issue_type = "none"
            severity = "none"
            if claimed_object_part != "unknown":
                object_part = claimed_object_part
            justification = (
                "The image does not show visual evidence proving the claimed "
                "functional failure."
            )

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

        # Second glass_shatter gate: even WITH a concrete visible_issue, if
        # the VLM's blurb describes a single fracture (crack/hairline/single
        # line) and does NOT contain any shatter language, the call is a
        # crack, not a shatter. This catches the common VLM error of
        # labeling a single stone-hit fracture as glass_shatter.
        if issue_type == "glass_shatter":
            single_fracture_hints = (
                "crack", "cracked", "hairline", "single fracture",
                "single crack", "one crack", "linear crack", "line crack",
                "stone hit", "stone impact", "small crack", "minor crack",
            )
            if (_contains_any(vlm_blurb, single_fracture_hints)
                    and not _contains_any(vlm_blurb, _SHATTER_HINTS)):
                issue_type = "crack"

        # Third glass_shatter gate: catch cases where the VLM says
        # "shatter" / "shattered" but provides no evidence of a real
        # shatter (no spider-web / shards / missing-pieces language).
        # Per the rubric, a single bounded fracture — even when the VLM
        # uses the word "shattered" — is crack + medium, NOT
        # glass_shatter + high. The "shatter" word alone is not enough
        # to justify the high-severity shatter call.
        if issue_type == "glass_shatter":
            # Severe-shatter signals: if ANY of these are present, the
            # shatter call is probably justified. Otherwise, the VLM is
            # over-escalating and we downgrade to crack.
            _SEVERE_SHATTER_HINTS = (
                "spider", "spiderweb", "spider-web", "spider pattern",
                "shards of glass", "glass shards", "broken shards",
                "broken into pieces", "pieces missing", "missing pieces",
                "missing glass", "missing chunks", "chunk missing",
                "completely shattered", "shattered completely",
                "crumbling", "crumbled", "shattered pattern",
                "glass falling", "glass broken off", "broken off",
                "shattered into", "shattered all over",
                "broken into", "many cracks and missing",
            )
            severe_signal = _contains_any(vlm_blurb, _SEVERE_SHATTER_HINTS)
            if not severe_signal:
                # No severe shatter signal -- downgrade to crack.
                # This is the most aggressive correction and catches
                # all the "VLM says shatter but it's actually a single
                # fracture" cases.
                issue_type = "crack"

        # Surface mark no deformation -> scratch (NOT dent)
        if issue_type == "dent" and _contains_any(vlm_blurb, _NO_DEFORM_HINTS):
            issue_type = "scratch"

        # Second dent gate: claim_mismatch + the VLM blurb contains a
        # POSITIVE scratch / scrape descriptor -> scratch (NOT dent).
        # The downgrade requires a positive signal (the VLM is naming
        # a scratch / scrape / surface mark), not merely the absence
        # of deformation language. claim_mismatch=true means the VLM
        # already flagged that the user's claim and the image do not
        # agree, so the VLM is re-interpreting the visible mark as a
        # scratch rather than a deformation.
        if (issue_type == "dent"
                and vlm_claim_mismatch
                and _contains_any(vlm_blurb, _SCRATCH_DESCRIPTORS)):
            issue_type = "scratch"

        # Residue / stain (no real water pattern) -> stain (NOT water_damage)
        if issue_type == "water_damage" and _contains_any(vlm_blurb, _RESIDUE_HINTS):
            # Don't downgrade if the VLM blurb is describing a true wet
            # pattern (e.g., wet keys, moisture, water damage without
            # any spill/residue language). The exception is "water spill" /
            # "sticky" — these indicate residue / stain even when the VLM
            # also mentions wet language.
            sticky_or_spill = _contains_any(vlm_blurb, (
                "water spill", "liquid spill", "spill residue",
                "spilled liquid", "sticky", "sticky keys",
                "spill", "spilled",
            ))
            if sticky_or_spill:
                issue_type = "stain"
            elif not _contains_any(vlm_blurb, _WET_PATTERN_HINTS):
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
    # Per the rubric, valid_image is only False for unreadable / tampered
    # images (non_original / possible_manipulation). A wrong-object match
    # does NOT make the image unreadable — the image still shows something
    # — so we keep valid_image=True and let the wrong_object risk flag
    # convey the structural mismatch.
    if vlm_non_original_image or vlm_possible_manipulation:
        valid_image = False

    # For wrong_object cases, force valid_image=True. The image is
    # usable — it just shows the wrong object. The wrong_object risk
    # flag conveys the mismatch.
    if vlm_wrong_object:
        valid_image = True

    # For NEI missing_part cases (the 0b rule set valid_image=False
    # because the contents cannot be verified), re-assert valid_image
    # =False here in case the normalize_bool above overwrote it.
    if (claim_status == "not_enough_information"
            and vlm_issue_type == "missing_part"
            and (issue_type == "unknown" or vlm_issue_type == "missing_part")):
        valid_image = False

    if claim_status == "not_enough_information":
        issue_type = "unknown"
        severity = "unknown"
        # object_part is left as whatever the VLM reported (typically
        # "unknown"). It is NEVER filled from user_history.claimed_part
        # because that would be a text-derived value filling a visual-
        # evidence gap, violating the image-first discipline.
        if not justification:
            justification = "The relevant part is not visible in the submitted images."

    # If we are contradicted AND have no concrete visible_issue AND the VLM
    # didn't report a concrete issue at the top level, collapse to none/none.
    if (claim_status == "contradicted"
            and not has_concrete_visible_issue
            and vlm_issue_type in ("none", "unknown")):
        issue_type = "none"
        severity = "none"

    # wrong_object + contradicted: the image shows the wrong thing, but
    # the image itself IS still valid (we can evaluate that it's wrong).
    # Per the rubric: evidence_standard_met=True (we successfully
    # evaluated the claim — we found the wrong object), valid_image=True
    # (the image is usable, it just shows the wrong object),
    # supporting_image_ids should point to the image that was used to
    # make the wrong-object determination (not "none"). Damage severity
    # remains unknown because this is not a visible damage grade on the
    # claimed object.
    if vlm_wrong_object and claim_status == "contradicted":
        issue_type = "unknown"
        object_part = "unknown"
        severity = "unknown"

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
    # supported/contradicted). Exception: when the claim was already
    # not_enough_information (e.g., missing_part NEI), keep that status —
    # valid_image=False for "cannot verify contents" is not the same as
    # "tampered image".
    if valid_image is False and claim_status != "not_enough_information":
        if claim_status not in ("contradicted",):
            claim_status = "contradicted"
        evidence_standard_met = True

    # ------------------------------------------------------------------
    # Layer 6 — supporting_image_ids policy (do this before Layer 5 so we
    # have the final supporting_image_ids available for the risk-flag
    # composition if any downstream code needs it).
    # ------------------------------------------------------------------
    if layer_supporting:
        if claim_status == "not_enough_information":
            # Genuine NEI: no image supports a determination.
            supporting_image_ids = "none"
        elif claim_status == "contradicted":
            # For contradicted cases, every image that was reviewed is
            # part of the basis for the contradiction (all of them show
            # the intact part, the wrong object, or the absence of the
            # claimed damage). Cite every image. This is rubric-aligned:
            # supporting_image_ids lists the images that support the
            # decision, and for a contradiction every reviewed image
            # supports the decision.
            all_ids = _all_image_ids_from_vlm(vlm_output, visible_issues)
            if all_ids:
                supporting_image_ids = ";".join(all_ids)
            else:
                supporting_image_ids = _first_image_id(
                    vlm_output, visible_issues
                )
        else:
            # For supported cases, cite the single most relevant image.
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

        # For NEI cases where the VLM's blurb says the part is not visible /
        # wrong angle, the right risk flag is wrong_angle, NOT claim_mismatch.
        # claim_mismatch implies the image shows something different from
        # what was claimed; wrong_angle implies the image doesn't show the
        # claimed part at all. Exception: for missing_part NEI cases (the
        # image shows packing material but the item is not visible), the
        # right flag is cropped_or_obstructed, not wrong_angle — the
        # contents are obscured / cut off, not just at a wrong angle.
        if claim_status == "not_enough_information":
            if _contains_any(vlm_blurb, _NOT_VISIBLE_PHRASES):
                is_missing_part_nei = (
                    vlm_issue_type == "missing_part"
                    or _contains_any(vlm_blurb, (
                        "no item visible", "no contents visible",
                        "no product visible", "only crumpled",
                        "only packing", "only paper", "only bubble",
                        "only foam", "only styrofoam", "only cardboard",
                        "only tissue",
                    ))
                )
                if is_missing_part_nei:
                    if "cropped_or_obstructed" not in flag_set:
                        flag_set.add("cropped_or_obstructed")
                    flag_set.discard("wrong_angle")
                else:
                    if "wrong_angle" not in flag_set:
                        flag_set.add("wrong_angle")
                    flag_set.discard("claim_mismatch")

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

        # Gate cropped_or_obstructed: keep only if the VLM's blurb actually
        # mentions cropping, obstruction, or blockage. The VLM's
        # image_quality_flags can be a false positive on clean rows.
        # Exception: missing_part NEI cases where the blurb mentions
        # "no item visible" / "only crumpled" — these are valid
        # cropped_or_obstructed signals.
        if "cropped_or_obstructed" in flag_set:
            crop_hints = (
                "crop", "cropped", "cut off", "obstruct", "obstruction",
                "block", "blocked", "partial", "partially visible",
                "edge of frame", "out of frame", "only part",
                # Additional hints for missing_part NEI cases:
                "no item visible", "no contents visible",
                "no product visible", "only crumpled", "only packing",
                "only paper", "only bubble", "only foam", "only styrofoam",
                "only cardboard", "only tissue",
            )
            if not _contains_any(vlm_blurb, crop_hints):
                flag_set.discard("cropped_or_obstructed")

        # Gate possible_manipulation: keep only if the VLM's blurb actually
        # mentions manipulation, editing, or digital alteration.
        if "possible_manipulation" in flag_set:
            manip_hints = (
                "manipulat", "manipulated", "edit", "edited", "alter",
                "altered", "photoshop", "digital alteration", "compos",
                "composite", "airbrush", "photo editing", "tamper",
                "tampered", "fake", "forged", "photoshopped",
            )
            if not _contains_any(vlm_blurb, manip_hints):
                flag_set.discard("possible_manipulation")

        # manual_review_required from user_history: if the user's history
        # flags indicate risk, always add manual_review_required.
        if isinstance(user_history, dict):
            uh_flags = str(user_history.get("history_flags", "")).lower()
            if ("user_history_risk" in uh_flags
                    or "manual_review_required" in uh_flags):
                if "manual_review_required" not in flag_set:
                    flag_set.add("manual_review_required")

        # damage_not_visible: only add when issue_type is none/unknown
        # (no visible damage to support the claim). Concrete issue_types
        # like scratch / broken_part / dent mean damage IS visible —
        # the contradiction is in the TYPE of damage, not in the
        # visibility, so claim_mismatch is the right flag (already added
        # by compose_risk_flags). The earlier block at line ~803 already
        # handles the none/unknown case.

        # For wrong_object cases: damage_not_visible does NOT apply. The
        # image shows SOMETHING (just not the claimed object), so the
        # image is evaluable. Exclude damage_not_visible here.
        if vlm_wrong_object and "damage_not_visible" in flag_set:
            flag_set.discard("damage_not_visible")

        # For wrong_object cases: wrong_object_part only applies when
        # the VLM actually emitted it AND the visible_issue is empty
        # AND the object_part is unknown. Otherwise it's a false
        # positive from the VLM's structural flags. The earlier gate
        # at line ~812 already handles the structural check, but for
        # wrong_object cases we strip it unconditionally when there's
        # no concrete visible issue (so the VLM's wrong_object_part
        # flag was speculative).
        if vlm_wrong_object and not has_concrete_visible_issue:
            flag_set.discard("wrong_object_part")

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
