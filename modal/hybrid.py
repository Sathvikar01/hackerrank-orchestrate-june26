"""modal/hybrid.py

Hybrid pipeline: Qwen (vision) + MIMO (reasoning) + rule engine (merge).

Architecture
------------

::

    images + claim + object_type
            |
            v
    +---------------+
    |  Qwen Observer| <-- Modal A10G (Qwen2.5-VL-3B)
    +---------------+
            |
            | {issue_type, object_part, severity, visible_damage}
            v
    +---------------+
    |  MIMO Judge   | <-- mimo-v2.5-pro via MIMO API (text-only)
    +---------------+
            |
            | {claim_status, evidence_standard_met, risk_flags,
            |  supporting_image_ids}
            v
    +---------------+
    |  Rule Engine  | <-- deterministic merge + consistency
    +---------------+
            |
            v
        output row

Why hybrid
----------
Live A/B testing showed:
* A (mimo-v2.5): row=20%, issue_type=40%, severity=45%, claim_status=85%
* B (Qwen-3B on Modal): row=10%, issue_type=55%, severity=40%, claim_status=65%

Qwen is +15pp on issue_type (the brief's primary gate) but -20pp on
claim_status and -15pp on supporting_image_ids. The hybrid splits the
work: Qwen handles what it does well (visual perception), MIMO handles
what it does well (text reasoning over claim + history + evidence reqs).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Local imports MUST come before any `import modal` so our local `modal/`
# folder wins the namespace race against the installed Modal Labs SDK.
_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
_CODE_DIR = _REPO_ROOT / "code"
_LOCAL_MODAL = _THIS.parent
for p in (str(_REPO_ROOT), str(_CODE_DIR), str(_LOCAL_MODAL)):
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)

from schema import (  # noqa: E402
    CLAIM_STATUS,
    ISSUE_TYPES,
    OBJECT_PARTS,
    OUTPUT_COLUMNS,
    RISK_FLAGS,
    SEVERITY,
    SEVERITY_ORDER,
)


# ---------------------------------------------------------------------------
# Step 1: Qwen Observer
# ---------------------------------------------------------------------------


def qwen_observe(
    image_paths: List[str],
    claim_text: str,
    object_type: str,
    qwen_client,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Call the deployed Qwen pipeline and return the visual-observation dict.

    Returns the schema-conformant dict from
    ``modal.qwen_client.QwenClient.predict_damage`` plus the four fields
    the brief calls out: ``issue_type``, ``object_part``, ``severity``,
    ``visible_damage``.
    """
    raw = qwen_client.predict_damage(
        image_paths=image_paths,
        claim_text=claim_text,
        object_type=object_type,
        use_cache=use_cache,
    )
    return {
        "issue_type": raw.get("issue_type", "unknown"),
        "object_part": raw.get("object_part", "unknown"),
        "severity": raw.get("severity", "unknown"),
        "visible_damage": bool(raw.get("visible_damage", False)),
        "_meta": raw.get("_meta", {}),
    }


# ---------------------------------------------------------------------------
# Step 2: MIMO Judge (text-only)
# ---------------------------------------------------------------------------


JUDGE_SYSTEM_PROMPT = """You decide an insurance claim from text only.

Inputs you receive:
- The user's claim transcript (chat with support)
- A vision model's structured observation of the submitted images
- The list of submitted image IDs
- The minimum evidence requirements for this object type
- The user's claim history

Return a single JSON object with these exact keys:

{
  "claim_status": "supported" | "contradicted" | "not_enough_information",
  "evidence_standard_met": true | false,
  "evidence_standard_met_reason": "one short sentence",
  "risk_flags": []
}

Rules:
- claim_status = "supported" when Qwen reports visible_damage=true AND
  issue_type is concrete AND object_part is consistent with the claim.
- claim_status = "contradicted" when the claim names a specific issue
  (e.g. "scratch") but Qwen reports a different issue (e.g. "dent"),
  OR when Qwen reports visible_damage=true but the claim denies damage.
- claim_status = "not_enough_information" when Qwen reports
  visible_damage=false, OR issue_type is "none"/"unknown", OR the
  claimed part is not visible.
- Add "claim_mismatch" to risk_flags when the claim and Qwen disagree
  on issue type.
- Add "user_history_risk" if past_claim_count >= 5 with rejections.
- Add "manual_review_required" if history_flags mentions "fraud" or
  rejected_claim >= 2.

Output ONLY the JSON object."""


def mimo_judge(
    claim_text: str,
    object_type: str,
    qwen_obs: Dict[str, Any],
    evidence_requirements: List[str],
    user_history: Dict[str, Any],
    image_ids: List[str],
    text_model: str,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """Call MIMO text model and return the judge decision.

    Returns a dict with ``claim_status``, ``evidence_standard_met``,
    ``evidence_standard_met_reason``, ``risk_flags``,
    ``supporting_image_ids``. Deterministic fallbacks are applied if the
    LLM call fails or returns malformed JSON.
    """
    cache_key = json.dumps(
        {
            "claim": claim_text,
            "object_type": object_type,
            "qwen": qwen_obs,
            "evidence": evidence_requirements,
            "history": user_history,
            "image_ids": image_ids,
            "model": text_model,
        },
        sort_keys=True,
    )
    cache_path = (
        Path(os.environ.get("HYBRID_JUDGE_CACHE_DIR", ".cache/hybrid_judge"))
        / f"{hashlib_md5(cache_key)}.json"
    )
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if use_cache and cache_path.exists():
        with open(cache_path, "r", encoding="utf-8") as f:
            return json.load(f)

    try:
        from openai import OpenAI
        from config import MIMO_BASE_URL, MIMO_API_KEY

        client = OpenAI(base_url=MIMO_BASE_URL, api_key=MIMO_API_KEY)
    except Exception as exc:  # pragma: no cover (missing config)
        return _judge_fallback(qwen_obs, user_history, image_ids, reason=f"client: {exc}")

    user_payload = (
        f"Claim transcript:\n```\n{claim_text}\n```\n\n"
        f"Object type: {object_type}\n\n"
        f"Vision model observations:\n```json\n{json.dumps(qwen_obs, indent=2)}\n```\n\n"
        f"Submitted image IDs: {image_ids}\n\n"
        f"Evidence requirements that apply:\n"
        + "\n".join(f"- {r}" for r in evidence_requirements)
        + "\n\n"
        f"User history:\n```json\n{json.dumps(user_history, indent=2)}\n```\n"
    )

    try:
        resp = client.chat.completions.create(
            model=text_model,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_payload},
            ],
            temperature=0.0,
            max_tokens=4096,
        )
        choice = resp.choices[0]
        raw = choice.message.content or ""
        if os.environ.get("HYBRID_DEBUG_PARSE", "0") == "1":
            finish = choice.finish_reason
            usage = getattr(resp, "usage", None)
            print(
                f"[hybrid] judge raw: len={len(raw)} finish={finish} usage={usage.model_dump() if usage else None}"
            )
            print(f"[hybrid] judge raw text[:600]: {raw[:600]!r}")
    except Exception as exc:  # pragma: no cover (network)
        return _judge_fallback(
            qwen_obs, user_history, image_ids, reason=f"transport: {exc}"
        )

    parsed = _safe_parse_judge(raw)
    # Ensure the result has all required keys with sensible defaults.
    parsed.setdefault("claim_status", "not_enough_information")
    parsed.setdefault("evidence_standard_met", False)
    parsed.setdefault(
        "evidence_standard_met_reason",
        "MIMO judge did not return a reason; defaulting to not_enough_information.",
    )
    parsed.setdefault("risk_flags", [])
    parsed.setdefault("supporting_image_ids", image_ids if image_ids else ["none"])

    # Normalise enums.
    if parsed["claim_status"] not in CLAIM_STATUS:
        parsed["claim_status"] = "not_enough_information"
    parsed["evidence_standard_met"] = bool(parsed["evidence_standard_met"])
    parsed["risk_flags"] = [
        f for f in (parsed["risk_flags"] or []) if isinstance(f, str) and f in RISK_FLAGS
    ]
    parsed["supporting_image_ids"] = _normalize_supporting(
        parsed["supporting_image_ids"], image_ids
    )

    # Deterministic history risk propagation (belt + braces).
    for f in _history_risk_flags(user_history):
        if f not in parsed["risk_flags"]:
            parsed["risk_flags"].append(f)
    if "manual_review_required" in parsed["risk_flags"]:
        # Soft escalation: do not flip claim_status, but flag it.
        pass

    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(parsed, f, ensure_ascii=False, indent=2)
    return parsed


def _safe_parse_judge(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    try:
        return json.loads(text)
    except Exception as exc:
        if os.environ.get("HYBRID_DEBUG_PARSE", "0") == "1":
            print(f"[hybrid] judge parse failed: {exc}; text={text[:500]!r}")
        return {}


# ---------------------------------------------------------------------------
# Deterministic claim-vs-Qwen cross-check
# ---------------------------------------------------------------------------


_CLAIM_ISSUE_KEYWORDS = {
    "dent": ["dent", "dented", "ding"],
    "scratch": ["scratch", "scratched", "scrape", "scraped", "mark"],
    "crack": ["crack", "cracked", "hairline"],
    "glass_shatter": ["shatter", "shattered", "broken glass"],
    "broken_part": ["broken", "broke", "snapped", "fracture"],
    "missing_part": ["missing", "absent", "no longer there", "lost the"],
    "torn_packaging": ["torn", "tear", "ripped", "open"],
    "crushed_packaging": ["crushed", "dented box", "smashed", "flattened"],
    "water_damage": ["water", "wet", "leak", "soaked", "moisture"],
    "stain": ["stain", "stained", "discolor", "spot"],
}


# Synonym groups: claims that name one issue in a group and Qwen sees
# another in the same group are NOT contradictions (e.g. the user says
# "shattered" but Qwen correctly identifies "crack" on the same panel).
_ISSUE_SYNONYM_GROUPS = [
    {"crack", "glass_shatter"},
    {"dent", "scratch"},  # both surface damage
    {"torn_packaging", "crushed_packaging"},  # both packaging damage
    {"broken_part", "torn_packaging", "crushed_packaging", "missing_part"},  # all "thing is damaged or gone"
    {"water_damage", "stain"},  # both liquid-related marks
]


def _claim_mentions_issue(user_claim: str) -> Optional[str]:
    """Detect which issue_type the claim transcript explicitly names.

    Returns the most specific issue_type mentioned, or None. The claim
    text is matched case-insensitively against a small keyword table.
    Uses word-boundary matching (not raw substring) to avoid false
    positives like "spreading" matching "ding".
    """
    if not user_claim:
        return None
    text = user_claim.lower()
    for issue, keywords in _CLAIM_ISSUE_KEYWORDS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", text):
                return issue
    return None


def _are_issues_synonymous(a: str, b: str) -> bool:
    """True when ``a`` and ``b`` are in the same synonym group."""
    if a == b:
        return True
    for group in _ISSUE_SYNONYM_GROUPS:
        if a in group and b in group:
            return True
    return False


def _reconcile_judge_with_qwen(
    judge_decision: Dict[str, Any],
    qwen_obs: Dict[str, Any],
    user_claim: str,
) -> Dict[str, Any]:
    """Deterministically reconcile judge decision with Qwen observations.

    Fixes two known judge failure modes:
    * judge says "supported" when the claim names one issue but Qwen sees
      a different one -> override to "contradicted" + claim_mismatch.
      Synonymous issues (e.g. "shattered" vs "crack") are NOT flagged.
    * judge says "supported" but issue_type is none/unknown -> override
      to "not_enough_information".
    """
    qwen_issue = qwen_obs.get("issue_type", "unknown")
    qwen_part = qwen_obs.get("object_part", "unknown")
    visible_damage = bool(qwen_obs.get("visible_damage", False))
    claim_status = judge_decision.get("claim_status", "not_enough_information")
    risk_flags = list(judge_decision.get("risk_flags", []) or [])
    supporting_ids = judge_decision.get("supporting_image_ids", ["none"]) or ["none"]

    # 1. Issue type mismatch (claim names one issue, Qwen sees another).
    claim_issue = _claim_mentions_issue(user_claim)
    if (
        claim_issue
        and claim_issue != qwen_issue
        and qwen_issue not in ("none", "unknown")
        and visible_damage
        and not _are_issues_synonymous(claim_issue, qwen_issue)
    ):
        claim_status = "contradicted"
        if "claim_mismatch" not in risk_flags:
            risk_flags.append("claim_mismatch")
        judge_decision["claim_status"] = claim_status
        judge_decision["risk_flags"] = risk_flags
        judge_decision["evidence_standard_met"] = False
        judge_decision["evidence_standard_met_reason"] = (
            f"Claim names {claim_issue!r} but Qwen sees {qwen_issue!r} on "
            f"{qwen_part!r}; contradicted."
        )

    # 2. supported but no visible damage -> downgrade.
    if claim_status == "supported" and not visible_damage:
        claim_status = "not_enough_information"
        judge_decision["claim_status"] = claim_status
        judge_decision["evidence_standard_met"] = False
        judge_decision["evidence_standard_met_reason"] = (
            "Downgraded: judge said supported but Qwen reports no visible damage."
        )

    # 3. supporting_ids=none forces not_enough_information.
    if supporting_ids == ["none"] and claim_status == "supported":
        claim_status = "not_enough_information"
        judge_decision["claim_status"] = claim_status
        judge_decision["evidence_standard_met"] = False
        judge_decision["evidence_standard_met_reason"] = (
            "Downgraded: judge said supported but no supporting image IDs."
        )

    return judge_decision


def _judge_fallback(
    qwen_obs: Dict[str, Any],
    user_history: Dict[str, Any],
    image_ids: List[str],
    reason: str,
) -> Dict[str, Any]:
    """Deterministic fallback used when the LLM is unavailable."""
    visible = bool(qwen_obs.get("visible_damage"))
    issue_type = qwen_obs.get("issue_type", "unknown")
    object_part = qwen_obs.get("object_part", "unknown")
    evidence_met = visible and issue_type not in ("none", "unknown")
    if visible:
        claim_status = "supported"
    elif issue_type in ("none", "unknown"):
        claim_status = "not_enough_information"
    else:
        claim_status = "contradicted"
    risk_flags = _history_risk_flags(user_history)
    return {
        "claim_status": claim_status,
        "evidence_standard_met": evidence_met,
        "evidence_standard_met_reason": (
            f"Deterministic fallback (LLM unavailable: {reason}). "
            f"Qwen visible_damage={visible}, issue_type={issue_type}."
        ),
        "risk_flags": risk_flags,
        "supporting_image_ids": (
            image_ids if evidence_met and image_ids else ["none"]
        ),
    }


def _normalize_supporting(value: Any, valid_ids: List[str]) -> List[str]:
    """Coerce ``supporting_image_ids`` to a list of valid image IDs or 'none'."""
    if value is None:
        return ["none"] if not valid_ids else valid_ids
    if isinstance(value, str):
        if value.lower() in ("none", "n/a", ""):
            return ["none"]
        value = [v.strip() for v in value.replace(",", ";").split(";") if v.strip()]
    if isinstance(value, list):
        cleaned: List[str] = []
        for v in value:
            s = str(v).strip()
            if not s:
                continue
            if s.lower() == "none":
                if "none" not in cleaned:
                    cleaned.append("none")
                continue
            if s in valid_ids and s not in cleaned:
                cleaned.append(s)
        return cleaned or (valid_ids if valid_ids else ["none"])
    return valid_ids or ["none"]


def _history_risk_flags(user_history: Dict[str, Any]) -> List[str]:
    if not user_history:
        return []
    flags: List[str] = []
    past = int(user_history.get("past_claim_count") or 0)
    rejected = int(user_history.get("rejected_claim") or 0)
    manual = int(user_history.get("manual_review_claim") or 0)
    last90 = int(user_history.get("last_90_days_claim_count") or 0)
    history_flags = str(user_history.get("history_flags", "")).lower()
    if past >= 5 and (rejected + manual) >= 1:
        flags.append("user_history_risk")
    if last90 >= 3:
        flags.append("user_history_risk")
    if "fraud" in history_flags or "high_risk" in history_flags:
        flags.append("manual_review_required")
    if rejected >= 2:
        flags.append("manual_review_required")
    seen: set = set()
    deduped: List[str] = []
    for f in flags:
        if f in RISK_FLAGS and f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


def hashlib_md5(s: str) -> str:
    import hashlib
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step 3: Merge (rule engine)
# ---------------------------------------------------------------------------


# Per-field routing weights (live measured on sample_claims.csv).
# Higher weight = prefer this source. Set to 0.5/0.5 for tie.
# These are the per-field accuracies of A and B observed in the A/B test.
_FIELD_ROUTING_WEIGHTS = {
    "evidence_standard_met": {"A": 0.85, "B": 0.90},   # B slightly better
    "risk_flags":             {"A": 0.55, "B": 0.50},   # A slightly better
    "issue_type":             {"A": 0.40, "B": 0.55},   # B clearly better
    "object_part":            {"A": 0.90, "B": 0.85},   # A slightly better
    "claim_status":           {"A": 0.85, "B": 0.65},   # A clearly better
    "supporting_image_ids":   {"A": 0.70, "B": 0.55},   # A clearly better
    "valid_image":            {"A": 0.90, "B": 0.90},   # tie
    "severity":               {"A": 0.45, "B": 0.40},   # A slightly better
}


def merge_into_row_router(
    user_id: str,
    image_paths_str: str,
    user_claim: str,
    claim_object: str,
    a_row: Dict[str, str],
    b_row: Dict[str, str],
    h_row: Dict[str, str],
) -> Dict[str, str]:
    """Best-of-both router: pick A or B per field based on routing weights.

    For each field, we pick the source with the higher routing weight.
    Fields where the router picks "B" get Qwen's value; fields where the
    router picks "A" get mimo-v2.5's value. The hybrid row H is used
    for the cross-field consistency check.

    The output is the same 14-column schema as A/B rows.
    """
    weights = _FIELD_ROUTING_WEIGHTS
    out = {
        "user_id": user_id,
        "image_paths": image_paths_str,
        "user_claim": user_claim,
        "claim_object": claim_object,
    }
    for f in [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]:
        w = weights.get(f, {"A": 0.5, "B": 0.5})
        if w["B"] > w["A"]:
            out[f] = b_row.get(f, "")
        else:
            out[f] = a_row.get(f, "")

    # Cross-field consistency: match the production rule engine's logic
    # rather than naively forcing not_enough_information when the image
    # is invalid. A wrong object / manipulated image contradicts the claim
    # rather than leaving it undecidable.
    valid_image = out.get("valid_image", "true").lower() in ("true", "1", "yes")
    evidence_met = out.get("evidence_standard_met", "true").lower() in (
        "true", "1", "yes"
    )
    cs = out.get("claim_status", "")
    if not valid_image:
        if "wrong_object" in out.get("risk_flags", "") or "possible_manipulation" in out.get(
            "risk_flags", ""
        ):
            out["claim_status"] = "contradicted"
        else:
            out["claim_status"] = "not_enough_information"
        out["evidence_standard_met"] = "false"
    elif not evidence_met:
        # No evidence -> not_enough_information (unless contradicted).
        if cs != "contradicted":
            out["claim_status"] = "not_enough_information"

    # Justification: cite both sources for transparency.
    out["claim_status_justification"] = (
        f"Router: issue_type from B (Qwen); claim_status from A (MIMO). "
        f"B issue={b_row.get('issue_type', '?')}, A cs={a_row.get('claim_status', '?')}, "
        f"H cs={h_row.get('claim_status', '?')}."
    )
    return out


def merge_into_row(
    user_id: str,
    image_paths_str: str,
    user_claim: str,
    claim_object: str,
    qwen_obs: Dict[str, Any],
    judge_decision: Dict[str, Any],
) -> Dict[str, str]:
    """Combine Qwen observations + MIMO judge decision into a 14-col row.

    The merge is intentionally minimal: trust the judge's claim_status
    unless there is a hard contradiction with the Qwen observations
    (e.g. judge says "supported" but Qwen says visible_damage=false).
    """
    claim_status = judge_decision.get("claim_status", "not_enough_information")
    evidence_met = bool(judge_decision.get("evidence_standard_met", False))
    reason = judge_decision.get(
        "evidence_standard_met_reason",
        "MIMO judge did not provide a reason.",
    )
    risk_flags = list(judge_decision.get("risk_flags", []) or [])
    supporting_ids = judge_decision.get("supporting_image_ids", ["none"]) or ["none"]
    if isinstance(supporting_ids, list):
        supporting_str = (
            ";".join(supporting_ids)
            if supporting_ids and supporting_ids != ["none"]
            else "none"
        )
    else:
        supporting_str = str(supporting_ids)

    issue_type = qwen_obs.get("issue_type", "unknown")
    object_part = qwen_obs.get("object_part", "unknown")
    severity = qwen_obs.get("severity", "unknown")
    visible_damage = bool(qwen_obs.get("visible_damage", False))

    # Hard safety rules (only override the judge when there is a
    # demonstrable contradiction between Qwen's visual output and the
    # judge's textual decision).
    if claim_status == "supported" and not visible_damage:
        # Judge says supported but Qwen saw no damage -> downgrade.
        claim_status = "not_enough_information"
        evidence_met = False
        reason = (
            "Downgraded to not_enough_information: judge reported supported "
            "but Qwen reports no visible damage on the claimed part."
        )
    if supporting_str == "none" and claim_status == "supported":
        # No supporting images -> cannot be supported.
        claim_status = "not_enough_information"
        evidence_met = False
        reason = (
            "Downgraded to not_enough_information: judge reported supported "
            "but no supporting image IDs were identified."
        )

    risk_flags_str = ";".join(risk_flags) if risk_flags else "none"

    return {
        "user_id": user_id,
        "image_paths": image_paths_str,
        "user_claim": user_claim,
        "claim_object": claim_object,
        "evidence_standard_met": "true" if evidence_met else "false",
        "evidence_standard_met_reason": reason,
        "risk_flags": risk_flags_str,
        "issue_type": issue_type,
        "object_part": object_part,
        "claim_status": claim_status,
        "claim_status_justification": (
            f"Hybrid: Qwen reports {issue_type} on {object_part} ({severity}, "
            f"visible={visible_damage}); MIMO judge decides {claim_status}."
        ),
        "supporting_image_ids": supporting_str,
        "valid_image": "true",
        "severity": severity,
    }


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _default_image_resolver():
    from config import DATASET_DIR

    def resolve(paths_str: str) -> List[str]:
        out = []
        for p in paths_str.split(";"):
            p = p.strip()
            if not p:
                continue
            path = Path(p)
            if not path.is_absolute():
                path = DATASET_DIR / path
            out.append(str(path.resolve()))
        return out

    return resolve


def _image_ids_from_paths(image_paths_str: str) -> List[str]:
    return [
        Path(p.strip()).stem
        for p in image_paths_str.split(";")
        if p.strip()
    ]


def run_hybrid_router(
    sample_csv: Path,
    a_csv: Path,
    b_csv: Path,
    out_dir: Path,
) -> Tuple[Path, Dict[str, Any]]:
    """Run the best-of-both router: per-field pick from A or B.

    Loads cached A and B outputs (must already exist), combines them
    via ``merge_into_row_router`` to produce a hybrid "R" output CSV.
    The router does not need the live hybrid (H) pipeline; it is a
    purely deterministic post-processor.

    Returns (output_csv, run_meta).
    """
    import pandas as pd

    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "router_R_output.csv"
    sample = pd.read_csv(sample_csv, dtype=str)
    a_df = pd.read_csv(a_csv, dtype=str)
    b_df = pd.read_csv(b_csv, dtype=str)

    rows: List[Dict[str, str]] = []
    start = time.time()
    for i in range(len(sample)):
        gt_row = sample.iloc[i].to_dict()
        a_row = a_df.iloc[i].to_dict()
        b_row = b_df.iloc[i].to_dict()
        # Empty hybrid row for the justification cross-check.
        h_row = {
            "issue_type": b_row.get("issue_type", "unknown"),
            "object_part": b_row.get("object_part", "unknown"),
            "claim_status": b_row.get("claim_status", "not_enough_information"),
            "visible_damage": True,
        }
        merged = merge_into_row_router(
            user_id=gt_row["user_id"],
            image_paths_str=gt_row["image_paths"],
            user_claim=gt_row["user_claim"],
            claim_object=gt_row["claim_object"],
            a_row=a_row,
            b_row=b_row,
            h_row=h_row,
        )
        rows.append(merged)

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    run_meta = {
        "runtime_seconds": time.time() - start,
        "rows": len(rows),
        "method": "router (per-field best-of-A-B)",
    }
    return out_csv, run_meta


def run_hybrid(
    sample_csv: Path,
    out_dir: Path,
    qwen_client,
    text_model: str,
    user_history_csv: Path,
    evidence_csv: Path,
    use_cache: bool = True,
) -> Tuple[Path, Dict[str, Any]]:
    """Run the hybrid pipeline on sample_claims.csv.

    Returns (output_csv, run_meta).
    """
    from config import DATASET_DIR  # noqa: F401
    from rules import apply_rules  # noqa: F401

    out_dir.mkdir(parents=True, exist_ok=True)
    out_csv = out_dir / "hybrid_H_output.csv"

    history_map = _load_user_history(user_history_csv)
    evidence_map = _load_evidence_requirements(evidence_csv)

    rows: List[Dict[str, str]] = []
    succeeded = 0
    failed = 0
    qwen_total_ms = 0
    judge_total_ms = 0
    start = time.time()

    resolver = _default_image_resolver()

    with open(sample_csv, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            user_id = row["user_id"]
            image_paths_str = row["image_paths"]
            user_claim = row["user_claim"]
            claim_object = row["claim_object"].strip().lower()
            abs_paths = resolver(image_paths_str)
            image_ids = _image_ids_from_paths(image_paths_str)
            requirements = (
                evidence_map.get("all", []) + evidence_map.get(claim_object, [])
            )
            user_history = history_map.get(user_id, {})

            # Step 1: Qwen Observer.
            try:
                qwen_obs = qwen_observe(
                    image_paths=abs_paths,
                    claim_text=user_claim,
                    object_type=claim_object,
                    qwen_client=qwen_client,
                    use_cache=use_cache,
                )
                qwen_ms = int(
                    qwen_obs.get("_meta", {}).get("latency_ms", 0) or 0
                )
                qwen_total_ms += qwen_ms
            except Exception as exc:
                qwen_obs = {
                    "issue_type": "unknown",
                    "object_part": "unknown",
                    "severity": "unknown",
                    "visible_damage": False,
                    "_meta": {"error": str(exc)},
                }
                qwen_ms = 0

            # Step 2: MIMO Judge.
            t0 = time.time()
            judge_decision = mimo_judge(
                claim_text=user_claim,
                object_type=claim_object,
                qwen_obs=qwen_obs,
                evidence_requirements=requirements,
                user_history=user_history,
                image_ids=image_ids,
                text_model=text_model,
                use_cache=use_cache,
            )
            judge_ms = int((time.time() - t0) * 1000)
            judge_total_ms += judge_ms

            # Step 2.5: Deterministic reconciliation. Catches the judge
            # failure modes that the LLM prompt alone does not catch
            # (issue_type mismatch, supported-without-damage, etc.).
            judge_decision = _reconcile_judge_with_qwen(
                judge_decision, qwen_obs, user_claim
            )

            # Step 3: Merge.
            row_out = merge_into_row(
                user_id=user_id,
                image_paths_str=image_paths_str,
                user_claim=user_claim,
                claim_object=claim_object,
                qwen_obs=qwen_obs,
                judge_decision=judge_decision,
            )
            rows.append(row_out)
            if qwen_obs.get("issue_type") in ("none", "unknown") and not qwen_obs.get(
                "visible_damage"
            ):
                # Not necessarily a failure, but flag for metrics.
                pass
            succeeded += 1

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    run_meta = {
        "succeeded": succeeded,
        "failed": failed,
        "qwen_total_ms": qwen_total_ms,
        "judge_total_ms": judge_total_ms,
        "qwen_mean_ms": int(qwen_total_ms / succeeded) if succeeded else 0,
        "judge_mean_ms": int(judge_total_ms / succeeded) if succeeded else 0,
        "runtime_seconds": time.time() - start,
        "text_model": text_model,
    }
    return out_csv, run_meta


def _load_user_history(path: Path) -> Dict[str, Dict[str, Any]]:
    import pandas as pd

    df = pd.read_csv(path, dtype=str)
    return {str(r["user_id"]): r.to_dict() for _, r in df.iterrows()}


def _load_evidence_requirements(path: Path) -> Dict[str, List[str]]:
    import pandas as pd

    df = pd.read_csv(path, dtype=str)
    out: Dict[str, List[str]] = {}
    for _, r in df.iterrows():
        obj = r["claim_object"]
        req = r["minimum_image_evidence"]
        out.setdefault(obj, []).append(req)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid pipeline runner.")
    parser.add_argument(
        "--sample-csv",
        default="dataset/sample_claims.csv",
    )
    parser.add_argument("--out-dir", default="evaluation")
    parser.add_argument(
        "--text-model",
        default=os.environ.get("TEXT_JUDGE_MODEL", "mimo-v2.5"),
        help="MIMO text model for the judge step",
    )
    parser.add_argument("--user-history", default="dataset/user_history.csv")
    parser.add_argument("--evidence", default="dataset/evidence_requirements.csv")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--no-judge-llm",
        action="store_true",
        help="Skip the LLM call and use deterministic fallback only",
    )
    parser.add_argument(
        "--router",
        action="store_true",
        help="Run the best-of-A-B router (no live Qwen/judge calls)",
    )
    parser.add_argument("--a-csv", default="evaluation/ab_A_output.csv")
    parser.add_argument("--b-csv", default="evaluation/ab_B_output.csv")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample = (repo_root / args.sample_csv).resolve()
    out_dir = (repo_root / args.out_dir).resolve()
    user_history = (repo_root / args.user_history).resolve()
    evidence = (repo_root / args.evidence).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not all(p.exists() for p in (sample, user_history, evidence)):
        missing = [str(p) for p in (sample, user_history, evidence) if not p.exists()]
        print(f"missing: {missing}", file=sys.stderr)
        return 2

    from modal.qwen_client import QwenClient

    if args.router:
        a_csv = (repo_root / args.a_csv).resolve()
        b_csv = (repo_root / args.b_csv).resolve()
        if not all(p.exists() for p in (sample, a_csv, b_csv)):
            missing = [str(p) for p in (sample, a_csv, b_csv) if not p.exists()]
            print(f"missing: {missing}", file=sys.stderr)
            return 2
        print(f"router mode: per-field best of A={a_csv} + B={b_csv}")
        out_csv, run_meta = run_hybrid_router(
            sample_csv=sample, a_csv=a_csv, b_csv=b_csv, out_dir=out_dir
        )
        print(f"\nWrote {out_csv}")
        print(f"  rows: {run_meta['rows']}")
        print(f"  runtime: {run_meta['runtime_seconds']:.2f} s")
        meta_path = out_dir / "router_run_meta.json"
        meta_path.write_text(
            json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return 0

    qwen_client = QwenClient()
    print(f"sample: {sample}")
    print(f"text_model: {args.text_model} (judge-llm={not args.no_judge_llm})")

    t0 = time.time()
    if args.no_judge_llm:
        # Patch mimo_judge to use deterministic fallback.
        global mimo_judge
        _real = mimo_judge

        def _deterministic_only(*a, **kw):  # type: ignore
            qwen_obs = kw.get("qwen_obs") or (a[2] if len(a) >= 3 else {})
            user_history = kw.get("user_history") or (a[4] if len(a) >= 5 else {})
            image_ids = kw.get("image_ids") or (a[5] if len(a) >= 6 else [])
            return _judge_fallback(
                qwen_obs, user_history, image_ids, reason="--no-judge-llm"
            )

        mimo_judge = _deterministic_only  # type: ignore

    out_csv, run_meta = run_hybrid(
        sample_csv=sample,
        out_dir=out_dir,
        qwen_client=qwen_client,
        text_model=args.text_model,
        user_history_csv=user_history,
        evidence_csv=evidence,
        use_cache=not args.no_cache,
    )
    print(f"\nWrote {out_csv}")
    print(f"  succeeded: {run_meta['succeeded']}")
    print(f"  qwen mean latency: {run_meta['qwen_mean_ms']} ms")
    print(f"  judge mean latency: {run_meta['judge_mean_ms']} ms")
    print(f"  total runtime: {time.time() - t0:.1f} s")

    meta_path = out_dir / "hybrid_run_meta.json"
    meta_path.write_text(
        json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())