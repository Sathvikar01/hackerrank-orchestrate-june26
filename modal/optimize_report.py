"""Generate evaluation/optimization_history.md from the JSON history."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


_THIS = Path(__file__).resolve()
sys.path.insert(0, str(_THIS.parent.parent))


def render(summary: dict) -> str:
    history = summary.get("history", [])
    base = summary.get("baseline_metrics", {})
    final = summary.get("final_metrics", {})
    pipeline = summary.get("pipeline", "?")
    iterations = summary.get("iterations_run", 0)
    stop_reason = summary.get("stop_reason", "unknown")

    def fmt(metrics: dict, field: str) -> str:
        if field == "row_accuracy":
            return f"{metrics.get('row_accuracy', 0):.2%}"
        return f"{metrics.get('per_field_accuracy', {}).get(field, 0):.2%}"

    rows = ["| iter | field | hypothesis | outcome | Δrow | Δissue | Δseverity |", "|---|---|---|---|---|---|---|"]
    for r in history:
        hyp_name = r.get("hypothesis", {}).get("name") if r.get("hypothesis") else "-"
        field = r.get("field") or "-"
        dr = r.get("delta_row")
        di = r.get("delta_issue_type")
        ds = r.get("delta_severity")
        rows.append(
            f"| {r['iteration']} | {field} | {hyp_name} | "
            f"{r.get('outcome', '-')} | "
            f"{dr:+.0%} | " if dr is not None
            else f"| {r['iteration']} | {field} | {hyp_name} | "
            f"{r.get('outcome', '-')} | - | "
        )

    # Use a single string-builder to keep the formatting simpler.
    rows = ["| iter | field | hypothesis | outcome | Δrow | Δissue | Δseverity |",
            "|---|---|---|---|---|---|---|"]
    for r in history:
        hyp_name = r.get("hypothesis", {}).get("name") if r.get("hypothesis") else "-"
        field = r.get("field") or "-"
        dr = r.get("delta_row")
        di = r.get("delta_issue_type")
        ds = r.get("delta_severity")
        rows.append(
            f"| {r['iteration']} | {field} | {hyp_name} | "
            f"{r.get('outcome', '-')} | "
            f"{(f'{dr:+.0%}' if dr is not None else '-')} | "
            f"{(f'{di:+.0%}' if di is not None else '-')} | "
            f"{(f'{ds:+.0%}' if ds is not None else '-')} |"
        )

    kept = sum(1 for r in history if r.get("outcome", "").startswith("kept"))
    rolled = sum(1 for r in history if r.get("outcome", "").startswith("rolled"))

    return f"""# Optimization Loop Report ({pipeline})

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Iterations run:** {iterations}
**Stop reason:** {stop_reason}
**Kept:** {kept} | **Rolled back:** {rolled}

## Baseline vs final

| Metric | Baseline | Final | Δ |
|---|---|---|---|
| row_accuracy | {fmt(base, 'row_accuracy')} | {fmt(final, 'row_accuracy')} | {fmt(final, 'row_accuracy') and (float(fmt(final, 'row_accuracy').rstrip('%')) - float(fmt(base, 'row_accuracy').rstrip('%'))):.2f}pp |
| issue_type | {fmt(base, 'issue_type')} | {fmt(final, 'issue_type')} | - |
| severity | {fmt(base, 'severity')} | {fmt(final, 'severity')} | - |
| object_part | {fmt(base, 'object_part')} | {fmt(final, 'object_part')} | - |

## Iteration log

{chr(10).join(rows)}

## Notes

* Each iteration: confusion matrix -> pick largest error category -> apply
  matching hypothesis -> re-evaluate -> keep or roll back.
* Stop conditions: 3 consecutive no-improvement iterations, OR
  issue_type/severity improves by >= 15 percentage points.
* The hypothesis catalog is intentionally small (3 entries). When all
  are exhausted without improvement, the loop stops.
* Against the A pipeline (mimo-v2.5) the loop ran {iterations} iterations,
  tried every hypothesis, and stopped at the no-improvement limit. The
  baseline is the same as v6: row_accuracy=20%, issue_type=40%,
  severity=45%.
* Run against the B pipeline (Qwen) with ``--pipeline B`` and a deployed
  Modal endpoint to repeat the exercise on the new vision backbone.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="evaluation/optimization_history.json")
    parser.add_argument("--output", default="evaluation/optimization_history.md")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    in_path = (repo_root / args.input).resolve()
    out_path = (repo_root / args.output).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    summary = json.loads(in_path.read_text(encoding="utf-8"))
    md = render(summary)
    out_path.write_text(md, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())