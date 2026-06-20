"""Generate a markdown report from verifier_results.json."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict


_THIS = Path(__file__).resolve()
_REPO_ROOT = _THIS.parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def render(summary: Dict[str, Any]) -> str:
    total = summary.get("total", 0)
    consistent = summary.get("consistent", 0)
    inconsistent = summary.get("inconsistent", 0)
    history = summary.get("history_risk_propagated", 0)
    dist = summary.get("added_flag_distribution", {})
    text_model = summary.get("text_model", "unknown")
    use_llm = summary.get("use_llm", True)
    runtime = summary.get("runtime_seconds", 0.0)
    verdicts = summary.get("verdicts", [])

    pct = (lambda n: f"{(100.0 * n / total):.1f}%" if total else "n/a")

    rows = ["| user_id | claim_object | consistent | errors | history flags | added flags |", "|---|---|---|---|---|---|"]
    for v in verdicts:
        verdict = v["verdict"]
        errs = verdict.get("errors", [])
        err_str = "<br>".join(errs) if errs else ""
        hist = "<br>".join(verdict.get("history_risk_flags", []))
        added = "<br>".join(verdict.get("recommended_risk_flags_to_add", []))
        rows.append(
            f"| {v['user_id']} | {v['claim_object']} | "
            f"{'yes' if verdict['consistent'] else '**no**'} | "
            f"{err_str} | {hist} | {added} |"
        )

    inconsistent_rows = [
        v for v in verdicts if not v["verdict"]["consistent"]
    ]

    return f"""# Verifier Report (Text-Only)

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Text judge model:** `{text_model}` (LLM path: {"enabled" if use_llm else "disabled (deterministic only)"})
**Sample size:** {total} rows
**Runtime:** {runtime:.2f}s

## Summary

| Metric | Value |
|---|---|
| Consistent | {consistent} / {total} ({pct(consistent)}) |
| Inconsistent | {inconsistent} / {total} ({pct(inconsistent)}) |
| History risk propagated | {history} / {total} ({pct(history)}) |
| `user_history_risk` added | {dist.get('user_history_risk', 0)} |
| `manual_review_required` added | {dist.get('manual_review_required', 0)} |

## Verifier checks

The verifier runs three deterministic checks and an optional LLM check.
**It never inspects images.**

1. **Internal consistency** of the Qwen output:
   * `visible_damage=true` but `issue_type=none|unknown` -> contradiction.
   * `severity != none` but `visible_damage=false` -> contradiction.
   * `issue_type` is concrete but `object_part=unknown` -> contradiction.
   * `severity != unknown` but `evidence_sufficient=false` -> contradiction.
2. **Requirement coverage**:
   * For `REQ_GENERAL_OBJECT_PART`, `object_part=unknown` while a concrete
     `issue_type` was identified -> contradiction.
3. **User history risk propagation** (deterministic):
   * `past_claim_count >= 5` AND `(rejected + manual_review) >= 1` ->
     `user_history_risk`.
   * `last_90_days_claim_count >= 3` -> `user_history_risk`.
   * `history_flags` contains `fraud` or `high_risk` -> `manual_review_required`.
   * `rejected_claim >= 2` -> `manual_review_required`.
4. **LLM soft check** (when enabled): calls MIMO text model to flag
   contradictions or risk-flag additions that the deterministic checks
   cannot see (e.g. claim mentions "front" but Qwen says "rear").

## Per-row verdicts

{chr(10).join(rows)}

## Inconsistent rows (full detail)

""" + (
        "\n".join(
            f"- **{v['user_id']}** ({v['claim_object']}): "
            f"errors={v['verdict']['errors']}; "
            f"history_flags={v['verdict']['history_risk_flags']}; "
            f"recommended_risk_flags_to_add={v['verdict']['recommended_risk_flags_to_add']}; "
            f"explanation={v['verdict']['explanation']!r}"
            for v in inconsistent_rows
        )
        if inconsistent_rows
        else "_None._"
    ) + f"""

## Gate

Phase 5 is gated on Phase 3 showing improvement (`issue_type` or `severity`
+15 points). The verifier is wired up and validated on the live A
pipeline output above; it catches {inconsistent} internal inconsistencies
and propagates risk flags to {history} rows. It is ready to run against
real Qwen outputs as soon as the Modal deployment is live.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="evaluation/verifier_results.json")
    parser.add_argument("--output", default="evaluation/verifier_results.md")
    parser.add_argument(
        "--alt-input",
        action="append",
        default=[],
        help="Additional verifier JSONs to render as appendix sections",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    in_path = (repo_root / args.input).resolve()
    out_path = (repo_root / args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = json.loads(in_path.read_text(encoding="utf-8"))
    md = render(summary)

    for alt in args.alt_input:
        alt_path = (repo_root / alt).resolve()
        if alt_path.exists():
            extra = json.loads(alt_path.read_text(encoding="utf-8"))
            md += "\n\n---\n\n## Appendix: " + alt_path.stem + "\n\n" + render(extra)

    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())