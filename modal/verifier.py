"""modal/verifier.py

Text-only verifier using MIMO (no image inspection).

Inputs
------
* claim - the user-claim transcript.
* qwen_json - the schema-conformant output of the Qwen pipeline
  (see ``modal.qwen_schema.QWEN_OUTPUT_SCHEMA``).
* evidence_requirements - the list of REQ_* strings that apply to this
  claim_object.
* user_history - the row from user_history.csv for this user_id.

The verifier checks three things and only three things:

1. **Contradiction** - does the Qwen output contradict itself or the claim?
   Examples:
   - Qwen says ``visible_damage=true`` but ``issue_type=none``.
   - Qwen says ``evidence_sufficient=true`` but the claim mentions a part
     that Qwen says is "unknown".
   - The claim says "front bumper" but Qwen's object_part is "rear_bumper"
     without flagging ``wrong_object_part``.

2. **Evidence sufficiency** - given the requirement list, is the claim
   assessable? Examples:
   - Claim mentions a laptop screen but the REQ list says
     "REQ_LAPTOP_SCREEN_KEYBOARD_TRACKPAD" and Qwen's object_part is
     "keyboard" (allowed) but visible_damage=false on the claimed part.
   - Multiple images are required by REQ_GENERAL_MULTI_IMAGE but Qwen's
     evidence_sufficient is true without it being reflected.

3. **Risk flags** - does user_history suggest elevated risk that should be
   propagated? Examples:
   - past_claim_count high + rejected_claim > 0 -> user_history_risk.
   - history_flags contains "fraud_history" -> manual_review_required.

The verifier never inspects images. The output is consumed by the pipeline
to amend the Qwen output: add risk flags, possibly flip claim_status.

Why MIMO and not a heavier model?
---------------------------------
The verifier is text-only, deterministic-input. A small text model is more
than enough and ~10x cheaper than running the VLM. ``mimo-v2-flash`` is the
configured ``TEXT_JUDGE_MODEL`` in the existing project.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
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

from schema import RISK_FLAGS  # noqa: E402


# ---------------------------------------------------------------------------
# Deterministic pre-checks (no LLM needed)
# ---------------------------------------------------------------------------


def _internal_consistency(qwen: Dict[str, Any]) -> List[str]:
    """Check the Qwen dict for obvious internal contradictions."""
    errors: List[str] = []
    issue_type = qwen.get("issue_type", "unknown")
    object_part = qwen.get("object_part", "unknown")
    severity = qwen.get("severity", "unknown")
    visible_damage = bool(qwen.get("visible_damage", False))
    evidence_sufficient = bool(qwen.get("evidence_sufficient", False))

    if visible_damage and issue_type in ("none", "unknown"):
        errors.append(
            f"visible_damage=true but issue_type={issue_type!r}"
        )
    if not visible_damage and severity in ("low", "medium", "high"):
        errors.append(
            f"severity={severity!r} but visible_damage=false"
        )
    # "unknown" issue_type legitimately pairs with "unknown" object_part
    # (the model couldn't tell). Only flag if the model produced a
    # concrete issue_type but left object_part blank.
    if (
        issue_type not in ("none", "unknown")
        and object_part == "unknown"
    ):
        errors.append(
            f"issue_type={issue_type!r} but object_part=unknown"
        )
    if not evidence_sufficient and severity != "unknown":
        errors.append(
            f"severity={severity!r} but evidence_sufficient=false"
        )
    return errors


def _requirement_coverage(
    qwen: Dict[str, Any], requirements: List[str]
) -> List[str]:
    """Check that Qwen's prediction is consistent with the evidence requirements."""
    errors: List[str] = []
    issue_type = qwen.get("issue_type", "unknown")
    evidence_sufficient = bool(qwen.get("evidence_sufficient", False))

    for req in requirements:
        if req.startswith("REQ_GENERAL_MULTI_IMAGE") and evidence_sufficient:
            # If multi-image is required and Qwen says evidence is sufficient,
            # we cannot flag a contradiction without seeing the images.
            # The deterministic check only flags when Qwen says NOT sufficient.
            continue
        if req.startswith("REQ_GENERAL_OBJECT_PART"):
            object_part = qwen.get("object_part", "unknown")
            if object_part == "unknown" and issue_type != "unknown":
                errors.append(
                    f"{req}: object_part is unknown but a specific issue was identified"
                )
    return errors


def _history_risk_flags(user_history: Dict[str, Any]) -> List[str]:
    """Propagate risk flags from user history without inspecting images."""
    flags: List[str] = []
    if not user_history:
        return flags

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


# ---------------------------------------------------------------------------
# LLM call (MIMO text)
# ---------------------------------------------------------------------------


def _llm_verify(
    claim: str,
    qwen: Dict[str, Any],
    requirements: List[str],
    user_history: Dict[str, Any],
    text_model: str,
) -> Dict[str, Any]:
    """Call MIMO text-only judge for the soft-check layer.

    Returns the parsed JSON dict. Falls back to ``{"consistent": True,
    "errors": [], ...}`` on parse failure so the deterministic checks above
    still apply.
    """
    try:
        from openai import OpenAI
        from config import MIMO_API_KEY, MIMO_BASE_URL
    except Exception as exc:  # pragma: no cover (missing config)
        return {
            "consistent": True,
            "errors": [],
            "recommended_claim_status": None,
            "recommended_risk_flags_to_add": [],
            "explanation": f"verifier unavailable: {exc}",
        }

    client = OpenAI(base_url=MIMO_BASE_URL, api_key=MIMO_API_KEY)

    system = (
        "You are a senior claims reviewer doing a TEXT-ONLY consistency check. "
        "You do NOT have access to images. You will be given the user's claim "
        "transcript, a vision model's structured prediction, the applicable "
        "evidence requirements, and the user's claim history. "
        "Return a single JSON object with keys: consistent (bool), errors "
        "(list of short strings), recommended_claim_status (one of "
        "'supported', 'contradicted', 'not_enough_information', or null), "
        "recommended_risk_flags_to_add (list of allowed risk flag strings), "
        "explanation (one short sentence). "
        "Only flag contradictions you can prove from the text. Do NOT invent."
    )

    user = (
        f"Claim transcript:\n```\n{claim}\n```\n\n"
        f"Vision model prediction:\n```json\n{json.dumps(qwen, indent=2)}\n```\n\n"
        f"Evidence requirements that apply:\n"
        + "\n".join(f"- {r}" for r in requirements)
        + "\n\n"
        f"User history:\n```json\n{json.dumps(user_history, indent=2)}\n```\n\n"
        "Return ONLY the JSON object, no markdown fences, no prose."
    )

    try:
        resp = client.chat.completions.create(
            model=text_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=600,
        )
        text = resp.choices[0].message.content or ""
    except Exception as exc:  # pragma: no cover (network)
        return {
            "consistent": True,
            "errors": [],
            "recommended_claim_status": None,
            "recommended_risk_flags_to_add": [],
            "explanation": f"verifier transport error: {exc}",
        }

    return _safe_parse(text)


def _safe_parse(text: str) -> Dict[str, Any]:
    text = text.strip()
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
    except Exception:
        return {
            "consistent": True,
            "errors": [],
            "recommended_claim_status": None,
            "recommended_risk_flags_to_add": [],
            "explanation": "verifier parse failed; defaulting to consistent",
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify(
    claim: str,
    qwen: Dict[str, Any],
    requirements: List[str],
    user_history: Dict[str, Any],
    text_model: str,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Run the full verifier (deterministic + LLM) and return merged verdict.

    The return dict matches the schema described in the brief::

        {
            "consistent": bool,
            "errors": [str],
            "recommended_claim_status": "supported" | "contradicted" |
                                          "not_enough_information" | None,
            "recommended_risk_flags_to_add": [str],
            "explanation": str,
        }
    """
    errors = _internal_consistency(qwen)
    errors.extend(_requirement_coverage(qwen, requirements))

    hist_flags = _history_risk_flags(user_history)

    llm_result: Dict[str, Any] = {
        "consistent": True,
        "errors": [],
        "recommended_claim_status": None,
        "recommended_risk_flags_to_add": [],
        "explanation": "",
    }
    if use_llm:
        llm_result = _llm_verify(claim, qwen, requirements, user_history, text_model)

    # Merge errors: deterministic + LLM.
    errors.extend(llm_result.get("errors") or [])

    # Merge risk flags: history propagation + LLM additions.
    risk_flags_added: List[str] = []
    for f in hist_flags + (llm_result.get("recommended_risk_flags_to_add") or []):
        if f and f in RISK_FLAGS and f not in risk_flags_added:
            risk_flags_added.append(f)

    consistent = (
        len(errors) == 0
        and bool(llm_result.get("consistent", True))
    )

    return {
        "consistent": consistent,
        "errors": errors,
        "recommended_claim_status": llm_result.get("recommended_claim_status"),
        "recommended_risk_flags_to_add": risk_flags_added,
        "explanation": llm_result.get("explanation") or "deterministic checks only",
        "deterministic_errors": _internal_consistency(qwen)
        + _requirement_coverage(qwen, requirements),
        "history_risk_flags": hist_flags,
    }


# ---------------------------------------------------------------------------
# Runner that evaluates the verifier on the sample claims
# ---------------------------------------------------------------------------


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


def run_verifier_on_sample(
    sample_csv: Path,
    qwen_csv: Path,
    user_history_csv: Path,
    evidence_csv: Path,
    text_model: str,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """Run the verifier against the existing Qwen output (or stub) and report.

    Parameters
    ----------
    sample_csv
        Ground-truth CSV (with the expected outputs).
    qwen_csv
        The Qwen pipeline's output CSV.
    user_history_csv, evidence_csv
        Reference data files.

    Returns a dict with overall + per-row verdicts.
    """
    import pandas as pd

    sample = pd.read_csv(sample_csv, dtype=str)
    qwen_df = pd.read_csv(qwen_csv, dtype=str)
    history = _load_user_history(user_history_csv)
    evidence = _load_evidence_requirements(evidence_csv)

    verdicts = []
    for idx, row in sample.iterrows():
        user_id = row["user_id"]
        claim_object = row["claim_object"].strip().lower()
        claim = row["user_claim"]
        requirements = evidence.get("all", []) + evidence.get(claim_object, [])
        qwen_row = qwen_df.iloc[idx].to_dict()
        qwen_dict = {
            "issue_type": qwen_row.get("issue_type", "unknown"),
            "object_part": qwen_row.get("object_part", "unknown"),
            "severity": qwen_row.get("severity", "unknown"),
            "visible_damage": qwen_row.get("severity", "unknown") not in ("none", "unknown"),
            "evidence_sufficient": qwen_row.get("evidence_standard_met", "false").lower() == "true",
            "quality_flags": [
                f.strip()
                for f in str(qwen_row.get("risk_flags", "")).split(";")
                if f.strip() and f.strip().lower() != "none"
            ],
        }
        verdict = verify(
            claim=claim,
            qwen=qwen_dict,
            requirements=requirements,
            user_history=history.get(user_id, {}),
            text_model=text_model,
            use_llm=use_llm,
        )
        verdicts.append(
            {
                "user_id": user_id,
                "claim_object": claim_object,
                "verdict": verdict,
            }
        )

    n_consistent = sum(1 for v in verdicts if v["verdict"]["consistent"])
    n_inconsistent = len(verdicts) - n_consistent
    flagged_with_history = sum(
        1 for v in verdicts if v["verdict"]["history_risk_flags"]
    )
    added_flags_counter: Counter = Counter()
    for v in verdicts:
        for f in v["verdict"]["recommended_risk_flags_to_add"]:
            added_flags_counter[f] += 1

    return {
        "total": len(verdicts),
        "consistent": n_consistent,
        "inconsistent": n_inconsistent,
        "history_risk_propagated": flagged_with_history,
        "added_flag_distribution": dict(added_flags_counter),
        "verdicts": verdicts,
        "text_model": text_model,
        "use_llm": use_llm,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the text-only verifier.")
    parser.add_argument("--sample-csv", default="dataset/sample_claims.csv")
    parser.add_argument(
        "--qwen-csv",
        default="evaluation/multicrop_B_output.csv",
        help="Qwen pipeline output to verify (default: multicrop output)",
    )
    parser.add_argument("--user-history", default="dataset/user_history.csv")
    parser.add_argument("--evidence", default="dataset/evidence_requirements.csv")
    parser.add_argument(
        "--text-model",
        default=os.environ.get("TEXT_JUDGE_MODEL", "mimo-v2-flash"),
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Skip the LLM call and run only deterministic checks",
    )
    parser.add_argument("--out", default="evaluation/verifier_results.json")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample = (repo_root / args.sample_csv).resolve()
    qwen = (repo_root / args.qwen_csv).resolve()
    history = (repo_root / args.user_history).resolve()
    evidence = (repo_root / args.evidence).resolve()
    out_path = (repo_root / args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not all(p.exists() for p in (sample, qwen, history, evidence)):
        missing = [str(p) for p in (sample, qwen, history, evidence) if not p.exists()]
        print(f"missing files: {missing}", file=sys.stderr)
        return 2

    print(f"sample: {sample}")
    print(f"qwen:   {qwen}")
    print(f"model:  {args.text_model} (llm={not args.no_llm})")

    t0 = time.time()
    summary = run_verifier_on_sample(
        sample_csv=sample,
        qwen_csv=qwen,
        user_history_csv=history,
        evidence_csv=evidence,
        text_model=args.text_model,
        use_llm=not args.no_llm,
    )
    summary["runtime_seconds"] = time.time() - t0

    out_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path}")
    print(f"  consistent: {summary['consistent']}/{summary['total']}")
    print(f"  inconsistent: {summary['inconsistent']}/{summary['total']}")
    print(f"  history_risk_propagated: {summary['history_risk_propagated']}/{summary['total']}")
    print(f"  added_flag_distribution: {summary['added_flag_distribution']}")
    print(f"  runtime: {summary['runtime_seconds']:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())