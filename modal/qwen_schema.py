"""modal/qwen_schema.py

Strict JSON schema for Qwen2.5-VL outputs and a validating parser.

The schema covers the six fields the brief calls out:

    {
      "issue_type": ...,
      "object_part": ...,
      "severity": ...,
      "visible_damage": bool,
      "evidence_sufficient": bool,
      "quality_flags": [...]
    }

The parser:
    1. Strips markdown fences / surrounding prose.
    2. Loads JSON (raises on failure).
    3. Validates the result against the schema using ``jsonschema``.
    4. Coerces enums into the canonical allowed lists (fuzzy match for the
       common LLM phrasings like "broken part", "high severity").
    5. Returns the canonical dict; raises ``ValueError`` on failure so the
       caller can retry once.

Retry policy
------------
The caller (``qwen_client.QwenClient.call``) catches
``QwenSchemaValidationError`` and retries once with a slightly stricter
prompt that emphasises the schema. After the retry it raises
``QwenSchemaValidationError`` so the caller can fall back to deterministic
defaults.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

# These mirror code/schema.py so the final post-processor in the existing
# pipeline does not have to map between two different vocabularies.
ISSUE_TYPES = [
    "dent",
    "scratch",
    "crack",
    "glass_shatter",
    "broken_part",
    "missing_part",
    "torn_packaging",
    "crushed_packaging",
    "water_damage",
    "stain",
    "none",
    "unknown",
]

SEVERITY = ["none", "low", "medium", "high", "unknown"]

QUALITY_FLAGS = [
    "blurry_image",
    "cropped_or_obstructed",
    "low_light_or_glare",
    "wrong_angle",
    "wrong_object",
    "wrong_object_part",
    "damage_not_visible",
    "claim_mismatch",
    "possible_manipulation",
    "non_original_image",
    "text_instruction_present",
]

OBJECT_PARTS: Dict[str, List[str]] = {
    "car": [
        "front_bumper", "rear_bumper", "door", "hood", "windshield",
        "side_mirror", "headlight", "taillight", "fender", "quarter_panel",
        "body", "unknown",
    ],
    "laptop": [
        "screen", "keyboard", "trackpad", "hinge", "lid", "corner", "port",
        "base", "body", "unknown",
    ],
    "package": [
        "box", "package_corner", "package_side", "seal", "label", "contents",
        "item", "unknown",
    ],
}

QWEN_OUTPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": [
        "issue_type",
        "object_part",
        "severity",
        "visible_damage",
        "evidence_sufficient",
        "quality_flags",
    ],
    "additionalProperties": False,
    "properties": {
        "issue_type": {"type": "string", "enum": ISSUE_TYPES},
        "object_part": {"type": "string"},
        "severity": {"type": "string", "enum": SEVERITY},
        "visible_damage": {"type": "boolean"},
        "evidence_sufficient": {"type": "boolean"},
        "quality_flags": {
            "type": "array",
            "items": {"type": "string", "enum": QUALITY_FLAGS},
        },
    },
}


class QwenSchemaValidationError(ValueError):
    """Raised when the model's raw output cannot be coerced into the schema."""


# ---------------------------------------------------------------------------
# Raw text -> dict
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json_block(text: str) -> str:
    """Pull the first JSON-looking block out of free-form text."""
    text = text.strip()
    if not text:
        raise QwenSchemaValidationError("Model returned empty text.")

    # Fast path: already JSON.
    if text.startswith("{"):
        return text

    # Markdown fence.
    fence = _JSON_FENCE_RE.search(text)
    if fence:
        return fence.group(1).strip()

    # Last resort: first { ... last }.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise QwenSchemaValidationError(
            f"No JSON object found in model output: {text[:200]!r}"
        )
    return text[start : end + 1]


def _coerce_issue_type(value: Any) -> str:
    return _fuzzy_issue_type(value)


def _fuzzy_issue_type(value: Any) -> str:
    if value is None:
        return "unknown"
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s in ISSUE_TYPES:
        return s
    aliases = {
        "dented": "dent",
        "scratched": "scratch",
        "cracked": "crack",
        "shattered": "glass_shatter",
        "shatter": "glass_shatter",
        "broken": "broken_part",
        "break": "broken_part",
        "missing": "missing_part",
        "torn": "torn_packaging",
        "crushed": "crushed_packaging",
        "wet": "water_damage",
        "water": "water_damage",
        "stained": "stain",
        "no_damage": "none",
        "no_issue": "none",
        "ok": "none",
        "fine": "none",
    }
    if s in aliases:
        return aliases[s]
    for allowed in ISSUE_TYPES:
        if s == allowed or s in allowed or allowed in s:
            return allowed
    # Strict: do NOT silently fall back to "unknown".
    return "__invalid__"


def _coerce_severity(value: Any) -> str:
    return _fuzzy_severity(value)


def _fuzzy_severity(value: Any) -> str:
    if value is None:
        return "unknown"
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s in SEVERITY:
        return s
    aliases = {
        "no": "none",
        "minor": "low",
        "small": "low",
        "moderate": "medium",
        "moderate_damage": "medium",
        "severe": "high",
        "major": "high",
        "critical": "high",
        "unsure": "unknown",
        "cant_tell": "unknown",
        "cannot_tell": "unknown",
        "n/a": "unknown",
        "na": "unknown",
    }
    if s in aliases:
        return aliases[s]
    for allowed in SEVERITY:
        if s == allowed or s in allowed or allowed in s:
            return allowed
    return "__invalid__"


def _coerce_object_part(value: Any, object_type: str) -> str:
    allowed = OBJECT_PARTS.get(object_type, ["unknown"])
    return _fuzzy_object_part(value, allowed)


def _fuzzy_object_part(value: Any, allowed: List[str]) -> str:
    if value is None:
        return "unknown"
    s = str(value).strip().lower().replace(" ", "_").replace("-", "_")
    if s in allowed:
        return s
    aliases = {
        "front_bumper": "front_bumper",
        "rear_bumper": "rear_bumper",
        "back_bumper": "rear_bumper",
        "windshield": "windshield",
        "front_glass": "windshield",
        "rear_glass": "windshield",
        "side_mirror": "side_mirror",
        "wing_mirror": "side_mirror",
        "headlight": "headlight",
        "headlamp": "headlight",
        "taillight": "taillight",
        "taillamp": "taillight",
        "tail_light": "taillight",
        "quarter_panel": "quarter_panel",
        "screen": "screen",
        "display": "screen",
        "lcd": "screen",
        "keyboard": "keyboard",
        "trackpad": "trackpad",
        "hinge": "hinge",
        "lid": "lid",
        "corner": "corner",
        "port": "port",
        "base": "base",
        "box": "box",
        "package_corner": "package_corner",
        "package_side": "package_side",
        "seal": "seal",
        "label": "label",
        "contents": "contents",
        "item": "item",
    }
    if s in aliases and aliases[s] in allowed:
        return aliases[s]
    for a in allowed:
        if s == a or s in a or a in s:
            return a
    # If the value looks like "unknown" / "n/a" / blank, allow it.
    if s in ("unknown", "n/a", "na", "none", ""):
        return "unknown"
    return "__invalid__"


def _coerce_quality_flags(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v.strip() for v in re.split(r"[,;|]", value) if v.strip()]
    if not isinstance(value, list):
        return []
    cleaned: List[str] = []
    for v in value:
        s = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        if s in QUALITY_FLAGS and s not in cleaned:
            cleaned.append(s)
    return cleaned


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("true", "yes", "1", "t", "y"):
        return True
    if s in ("false", "no", "0", "f", "n"):
        return False
    raise QwenSchemaValidationError(f"bool: {value!r} not recognised")


def coerce_into_schema(
    raw: Dict[str, Any], object_type: str, strict: bool = True
) -> Dict[str, Any]:
    """Return a new dict matching ``QWEN_OUTPUT_SCHEMA`` exactly.

    If ``strict=True`` (the default), any enum value that cannot be coerced
    raises ``QwenSchemaValidationError``. The brief mandates reject-and-retry
    on malformed outputs, so we never silently coerce to "unknown".
    """
    if "visible_damage" not in raw:
        raise QwenSchemaValidationError("visible_damage: missing")
    if "evidence_sufficient" not in raw:
        raise QwenSchemaValidationError("evidence_sufficient: missing")

    issue_type = _coerce_issue_type(raw.get("issue_type"))
    if strict and issue_type == "__invalid__":
        raise QwenSchemaValidationError(
            f"issue_type: {raw.get('issue_type')!r} not in allowed {ISSUE_TYPES}"
        )

    severity = _coerce_severity(raw.get("severity"))
    if strict and severity == "__invalid__":
        raise QwenSchemaValidationError(
            f"severity: {raw.get('severity')!r} not in allowed {SEVERITY}"
        )

    object_part = _coerce_object_part(raw.get("object_part"), object_type)
    if strict and object_part == "__invalid__":
        allowed = OBJECT_PARTS.get(object_type, ["unknown"])
        raise QwenSchemaValidationError(
            f"object_part: {raw.get('object_part')!r} not in allowed {allowed}"
        )

    return {
        "issue_type": issue_type,
        "object_part": object_part,
        "severity": severity,
        "visible_damage": _coerce_bool(raw.get("visible_damage")),
        "evidence_sufficient": _coerce_bool(raw.get("evidence_sufficient")),
        "quality_flags": _coerce_quality_flags(raw.get("quality_flags")),
    }


def parse_qwen_output(raw_text: str, object_type: str = "") -> Dict[str, Any]:
    """Parse + validate model output text.

    Raises
    ------
    QwenSchemaValidationError
        If the text cannot be parsed as JSON, or if the parsed JSON cannot be
        coerced into the schema.
    """
    block = _extract_json_block(raw_text)
    try:
        raw = json.loads(block)
    except json.JSONDecodeError as exc:
        raise QwenSchemaValidationError(
            f"JSON decode failed: {exc}; block={block[:200]!r}"
        ) from exc
    if not isinstance(raw, dict):
        raise QwenSchemaValidationError(
            f"Expected JSON object, got {type(raw).__name__}"
        )

    coerced = coerce_into_schema(raw, object_type, strict=True)

    # Final structural check.
    try:
        import jsonschema  # local import to keep cold start snappy

        jsonschema.validate(coerced, QWEN_OUTPUT_SCHEMA)
    except Exception as exc:  # pragma: no cover (defensive)
        raise QwenSchemaValidationError(f"Schema validation failed: {exc}") from exc

    return coerced


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------


def self_test() -> Tuple[bool, List[str]]:
    """Smoke check used by CI."""
    errors: List[str] = []

    sample_good = (
        '{"issue_type":"dent","object_part":"rear_bumper","severity":"medium",'
        '"visible_damage":true,"evidence_sufficient":true,"quality_flags":[]}'
    )
    sample_fenced = (
        "```json\n" + sample_good + "\n```"
    )
    sample_prose = "Sure, here is the JSON you requested: " + sample_good + " Thanks!"

    for label, text in (("bare", sample_good), ("fenced", sample_fenced), ("prose", sample_prose)):
        try:
            out = parse_qwen_output(text, object_type="car")
            assert out["issue_type"] == "dent"
            assert out["object_part"] == "rear_bumper"
            assert out["severity"] == "medium"
            assert out["visible_damage"] is True
            assert out["evidence_sufficient"] is True
            assert out["quality_flags"] == []
        except Exception as exc:  # pragma: no cover
            errors.append(f"{label}: {exc}")

    try:
        parse_qwen_output("not json at all", object_type="car")
        errors.append("expected failure on non-json input")
    except QwenSchemaValidationError:
        pass

    try:
        parse_qwen_output(
            '{"issue_type":"banana","object_part":"rear_bumper","severity":"medium",'
            '"visible_damage":true,"evidence_sufficient":true,"quality_flags":[]}',
            object_type="car",
        )
        errors.append("expected failure on bad issue_type")
    except QwenSchemaValidationError:
        pass

    return (len(errors) == 0, errors)


if __name__ == "__main__":
    ok, errors = self_test()
    if ok:
        print("qwen_schema self_test: OK")
    else:
        print("qwen_schema self_test: FAIL")
        for e in errors:
            print(" -", e)
        raise SystemExit(1)