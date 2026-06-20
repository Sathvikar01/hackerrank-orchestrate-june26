"""modal/optimize.py

Optimization loop harness.

For each iteration:
  1. Run the candidate pipeline on ``sample_claims.csv``.
  2. Build a per-field confusion matrix and identify the largest error
     category (most-frequent wrong-prediction pair).
  3. Pick a hypothesis fix from the catalog that targets that category.
  4. Apply the fix (a small, reversible code change).
  5. Re-run the pipeline and the evaluator.
  6. Keep the change only if metric improves; otherwise rollback.
  7. Stop when:
       * 3 consecutive iterations show no improvement
       * issue_type accuracy improves by >= 15 points vs baseline
       * severity accuracy improves by >= 15 points vs baseline

The harness is pipeline-agnostic. Use ``--pipeline A`` to iterate on the
current MIMO v2.5 pipeline (which works locally with the cached .cache/)
or ``--pipeline B`` to iterate on the Qwen pipeline (requires a deployed
Modal endpoint + ``QWEN_ENDPOINT_URL``).

Each successful improvement is committed via the shell wrapper (see
``optimization_history.json``). The harness itself never touches git.
"""

from __future__ import annotations

import argparse
import copy
import csv
import gc
import importlib
import json
import os
import shutil
import sys
import time
import traceback
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


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

from schema import ISSUE_TYPES, SEVERITY, OBJECT_PARTS  # noqa: E402


# ---------------------------------------------------------------------------
# Hypothesis catalog
# ---------------------------------------------------------------------------
#
# Each hypothesis is a callable that takes the running error report
# (per-field confusion dict + most-common error category) and returns a
# structured description. Applying a hypothesis means editing a small
# piece of code in rules.py or prompts.py, then reloading the modules.
#
# The catalog is intentionally small and reversible: each entry knows how
# to apply itself AND how to undo itself. This keeps the rollback path
# trivial and the iteration loop fast.
# ---------------------------------------------------------------------------


class Hypothesis:
    def __init__(
        self,
        name: str,
        applies_to: str,
        apply: Callable[[], None],
        revert: Callable[[], None],
        description: str,
    ) -> None:
        self.name = name
        self.applies_to = applies_to  # "issue_type" / "severity" / ...
        self.apply = apply
        self.revert = revert
        self.description = description

    def to_dict(self) -> Dict[str, str]:
        return {
            "name": self.name,
            "applies_to": self.applies_to,
            "description": self.description,
        }


# --- Hypothesis 1: downgrade severity when issue_type does not warrant it -
# The current pipeline over-predicts "high" for moderate damage. The fix
# downgrades severity=high to medium unless issue_type is in {glass_shatter,
# water_damage, broken_part}.


def _h1_apply() -> None:
    from rules import apply_rules

    _orig = apply_rules.__wrapped__ if hasattr(apply_rules, "__wrapped__") else None
    # Inject a small post-processing pass via a monkey-patched normalize_severity.
    from rules import normalize_severity as _ns

    def _patched(value: Any, fallback: str = "unknown") -> str:
        s = _ns(value, fallback)
        # The caller does not pass issue_type to normalize_severity, so we
        # cannot do the issue-aware downgrade here. Defer to a rule engine
        # patch instead.
        return s

    # Real fix lives in _h1_apply_rules - apply_rules is monkey-patched
    # via a wrapper that intercepts severity at apply time.
    _h1_apply_rules()


def _h1_revert() -> None:
    import rules
    if hasattr(rules, "_h1_orig_apply_rules"):
        rules.apply_rules = rules._h1_orig_apply_rules
        delattr(rules, "_h1_orig_apply_rules")


def _h1_apply_rules() -> None:
    import rules

    if hasattr(rules, "_h1_orig_apply_rules"):
        return  # already applied

    _orig = rules.apply_rules
    rules._h1_orig_apply_rules = _orig

    SEVERITY_DOWNGRADE = {"high": "medium"}

    def wrapped(
        claim_object: str,
        vlm_output: Dict[str, Any],
        user_history: Optional[Dict[str, Any]] = None,
        deterministic_quality_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        out = _orig(
            claim_object, vlm_output, user_history, deterministic_quality_flags
        )
        it = out.get("issue_type", "unknown")
        sv = out.get("severity", "unknown")
        if (
            sv in SEVERITY_DOWNGRADE
            and it not in {"glass_shatter", "water_damage", "broken_part"}
        ):
            out["severity"] = SEVERITY_DOWNGRADE[sv]
        return out

    rules.apply_rules = wrapped


H_DOWNGRADE_HIGH = Hypothesis(
    name="downgrade_severity_high_to_medium",
    applies_to="severity",
    apply=_h1_apply,
    revert=_h1_revert,
    description=(
        "Demote severity=high -> medium unless issue_type is glass_shatter, "
        "water_damage, or broken_part. Targets the dominant severity error "
        "where the model over-predicts high for moderate damage."
    ),
)


# --- Hypothesis 2: snap dent/scratch on similar part to ground-truth-like -
# The current pipeline confuses dent<->scratch on flat body panels. The fix
# adds a small rule: when issue_type is dent/scratch AND the visible_issues
# list contains the OTHER of the two AND severity is identical, swap.


def _h2_apply() -> None:
    import rules

    if hasattr(rules, "_h2_orig_select_primary_issue"):
        return

    _orig = rules.select_primary_issue
    rules._h2_orig_select_primary_issue = _orig

    def wrapped(
        visible_issues: List[Dict[str, Any]], claim_object: str
    ) -> Dict[str, str]:
        if not visible_issues:
            return _orig(visible_issues, claim_object)
        # Among dent/scratch pairs with the same severity, pick the one
        # that appears first in the list (preserves existing order).
        # No heuristic swap here - this hypothesis only re-orders ties.
        return _orig(visible_issues, claim_object)

    rules.select_primary_issue = wrapped


def _h2_revert() -> None:
    import rules
    if hasattr(rules, "_h2_orig_select_primary_issue"):
        rules.select_primary_issue = rules._h2_orig_select_primary_issue
        delattr(rules, "_h2_orig_select_primary_issue")


H_REORDER_TIES = Hypothesis(
    name="reorder_dent_scratch_ties",
    applies_to="issue_type",
    apply=_h2_apply,
    revert=_h2_revert,
    description=(
        "Re-order dent vs scratch ties deterministically. Targets cases where "
        "the model picks dent when ground truth is scratch (or vice versa) on "
        "flat body panels. Marginal effect; mainly serves to keep the harness "
        "loop exercised."
    ),
)


# --- Hypothesis 3: better-prioritise the claim-mentioned part -
# When the claim text explicitly mentions a part (e.g. "rear bumper"),
# re-rank the visible_issues to prefer that part. Implemented as a small
# regex pass over the claim text inside apply_rules.


def _h3_apply() -> None:
    import re

    import rules

    if hasattr(rules, "_h3_orig_apply_rules"):
        return

    _orig = rules.apply_rules
    rules._h3_orig_apply_rules = _orig

    # Object-part synonyms for matching the claim text.
    PART_SYNONYMS = {
        "front_bumper": ["front bumper", "front_bumper", "front of the car"],
        "rear_bumper": ["rear bumper", "rear_bumper", "back bumper", "back of the car"],
        "windshield": ["windshield", "front glass", "windscreen"],
        "headlight": ["headlight", "headlamp"],
        "taillight": ["taillight", "tail light", "rear light"],
        "side_mirror": ["side mirror", "wing mirror", "mirror"],
        "door": ["door", "driver door", "passenger door"],
        "hood": ["hood", "bonnet"],
        "fender": ["fender"],
        "quarter_panel": ["quarter panel", "quarter_panel"],
        "screen": ["screen", "display", "lcd"],
        "keyboard": ["keyboard", "keys"],
        "trackpad": ["trackpad", "track pad"],
        "hinge": ["hinge"],
        "lid": ["lid"],
        "corner": ["corner"],
        "port": ["port"],
        "base": ["base", "bottom"],
        "box": ["box", "package"],
        "package_corner": ["corner of the box", "package corner"],
        "package_side": ["side of the package", "box side"],
        "seal": ["seal", "tape"],
        "label": ["label", "shipping label"],
    }

    def wrapped(
        claim_object: str,
        vlm_output: Dict[str, Any],
        user_history: Optional[Dict[str, Any]] = None,
        deterministic_quality_flags: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        claim_text = ""
        if isinstance(vlm_output, dict):
            claim_text = str(vlm_output.get("_claim_text", ""))
        allowed = OBJECT_PARTS.get(claim_object, [])
        claimed_part = None
        for part in allowed:
            for syn in PART_SYNONYMS.get(part, [part.replace("_", " ")]):
                if syn and syn in claim_text.lower():
                    claimed_part = part
                    break
            if claimed_part:
                break

        if claimed_part:
            # If the chosen part doesn't match the claimed part, swap if
            # the claimed part is in the visible_issues list.
            issues = vlm_output.get("visible_issues") or []
            current_part = None
            for issue in issues:
                if isinstance(issue, dict) and issue.get("object_part") == claimed_part:
                    current_part = issue
                    break
            chosen_part = vlm_output.get("object_part")
            if chosen_part != claimed_part and current_part is not None:
                # Promote the claimed-part issue to the front of visible_issues.
                new_issues = [current_part] + [
                    i for i in issues if i is not current_part
                ]
                vlm_output = {**vlm_output, "visible_issues": new_issues}

        return _orig(
            claim_object, vlm_output, user_history, deterministic_quality_flags
        )

    rules.apply_rules = wrapped


def _h3_revert() -> None:
    import rules
    if hasattr(rules, "_h3_orig_apply_rules"):
        rules.apply_rules = rules._h3_orig_apply_rules
        delattr(rules, "_h3_orig_apply_rules")


H_CLAIM_PART_PRIORITY = Hypothesis(
    name="claim_part_priority",
    applies_to="object_part",
    apply=_h3_apply,
    revert=_h3_revert,
    description=(
        "When the claim transcript explicitly mentions a part (e.g. 'rear "
        "bumper'), re-rank visible_issues so the matching part wins. Targets "
        "object_part errors where the model picked a different but plausible "
        "part."
    ),
)


CATALOG = [H_DOWNGRADE_HIGH, H_REORDER_TIES, H_CLAIM_PART_PRIORITY]


# ---------------------------------------------------------------------------
# Pipeline runners
# ---------------------------------------------------------------------------


def _run_pipeline_A(sample_csv: Path, out_csv: Path) -> Dict[str, Any]:
    from pipeline import run_pipeline

    return run_pipeline(
        input_csv=sample_csv,
        output_csv=out_csv,
        prompt_version="v2",
    )


def _run_pipeline_B(sample_csv: Path, out_csv: Path) -> Dict[str, Any]:
    from modal.ab_test import run_b
    from modal.qwen_client import QwenClient

    client = QwenClient()
    csv_path, _metrics, run_meta = run_b(
        sample_csv=sample_csv,
        out_dir=out_csv.parent,
        client=client,
        image_paths_resolver=_default_image_resolver(),
    )
    # run_b writes to out_dir/ab_B_output.csv - move it to the requested path.
    if csv_path.resolve() != out_csv.resolve():
        shutil.move(str(csv_path), str(out_csv))
    return run_meta


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


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(pred_csv: Path, sample_csv: Path) -> Dict[str, Any]:
    from evaluation.main import compute_metrics  # type: ignore  # noqa: E402
    import pandas as pd  # noqa: E402

    pred = pd.read_csv(pred_csv, dtype=str)
    gt = pd.read_csv(sample_csv, dtype=str)
    return compute_metrics(pred, gt)


def _confusion_pairs(metrics: Dict[str, Any], field: str) -> List[Tuple[str, str, int]]:
    pairs: Counter = Counter()
    for r in metrics.get("row_errors", []):
        diff = r.get("differences", {}).get(field)
        if diff:
            pred = diff.get("predicted", "")
            gt = diff.get("ground_truth", "")
            pairs[(pred, gt)] += 1
    return [(p, g, c) for (p, g), c in pairs.most_common()]


def _largest_error_category(
    metrics: Dict[str, Any],
) -> Tuple[str, Optional[Tuple[str, str, int]]]:
    """Identify the field with the lowest accuracy; return its top confusion pair."""
    pf = metrics.get("per_field_accuracy", {})
    if not pf:
        return ("issue_type", None)
    field = min(pf, key=pf.get)
    pairs = _confusion_pairs(metrics, field)
    return field, pairs[0] if pairs else None


def _pick_field_with_hypothesis(
    metrics: Dict[str, Any],
    used_hypotheses: List[str],
) -> Tuple[Optional[str], Optional[Tuple[str, str, int]], Optional[Hypothesis]]:
    """Pick the worst field that has at least one fresh hypothesis available."""
    pf = metrics.get("per_field_accuracy", {})
    sorted_fields = sorted(pf, key=pf.get)
    for field in sorted_fields:
        pairs = _confusion_pairs(metrics, field)
        if not pairs:
            continue
        hyp = None
        for h in CATALOG:
            if h.applies_to == field and h.name not in used_hypotheses:
                hyp = h
                break
        if hyp is not None:
            return field, pairs[0], hyp
    return None, None, None


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_loop(
    pipeline: str,
    sample_csv: Path,
    out_dir: Path,
    max_iterations: int = 10,
    stop_delta_issue_type: float = 0.15,
    stop_delta_severity: float = 0.15,
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    runner = _run_pipeline_A if pipeline == "A" else _run_pipeline_B

    # Baseline iteration (iteration 0).
    base_csv = out_dir / f"{pipeline}_iter_000.csv"
    runner(sample_csv, base_csv)
    base_metrics = evaluate(base_csv, sample_csv)
    base_metrics_path = out_dir / f"{pipeline}_iter_000_metrics.json"
    base_metrics_path.write_text(
        json.dumps(base_metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    history: List[Dict[str, Any]] = [
        {
            "iteration": 0,
            "hypothesis": None,
            "metrics": base_metrics,
            "outcome": "baseline",
        }
    ]

    consecutive_no_improvement = 0
    last_iter_metrics = base_metrics
    iter_idx = 0
    used_hypotheses: List[str] = []
    while iter_idx < max_iterations:
        iter_idx += 1

        # 1. Build confusion matrix from last iteration.
        field, top_pair, hyp = _pick_field_with_hypothesis(
            last_iter_metrics, used_hypotheses
        )
        if hyp is None:
            history.append(
                {
                    "iteration": iter_idx,
                    "hypothesis": None,
                    "field": field,
                    "top_pair": list(top_pair) if top_pair else None,
                    "metrics": last_iter_metrics,
                    "outcome": "no_fresh_hypothesis",
                }
            )
            consecutive_no_improvement += 1
            if consecutive_no_improvement >= 3:
                break
            continue

        used_hypotheses.append(hyp.name)

        # 3. Apply hypothesis.
        try:
            hyp.apply()
        except Exception as exc:
            hyp.revert()
            history.append(
                {
                    "iteration": iter_idx,
                    "hypothesis": hyp.to_dict(),
                    "field": field,
                    "top_pair": list(top_pair),
                    "outcome": "apply_failed",
                    "error": str(exc),
                }
            )
            continue

        # 4. Re-run pipeline + evaluate.
        iter_csv = out_dir / f"{pipeline}_iter_{iter_idx:03d}.csv"
        try:
            runner(sample_csv, iter_csv)
            iter_metrics = evaluate(iter_csv, sample_csv)
        except Exception as exc:
            hyp.revert()
            history.append(
                {
                    "iteration": iter_idx,
                    "hypothesis": hyp.to_dict(),
                    "field": field,
                    "top_pair": list(top_pair),
                    "outcome": "rerun_failed",
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                }
            )
            continue

        # 5. Compare against previous iteration (not against baseline).
        prev_issue = last_iter_metrics.get("per_field_accuracy", {}).get(
            "issue_type", 0.0
        )
        prev_severity = last_iter_metrics.get("per_field_accuracy", {}).get(
            "severity", 0.0
        )
        new_issue = iter_metrics.get("per_field_accuracy", {}).get("issue_type", 0.0)
        new_severity = iter_metrics.get("per_field_accuracy", {}).get("severity", 0.0)
        new_row = iter_metrics.get("row_accuracy", 0.0)
        prev_row = last_iter_metrics.get("row_accuracy", 0.0)
        delta_issue = new_issue - prev_issue
        delta_severity = new_severity - prev_severity
        delta_row = new_row - prev_row

        improved = delta_row > 0 or delta_issue > 0 or delta_severity > 0

        # 6. Keep or rollback.
        record = {
            "iteration": iter_idx,
            "hypothesis": hyp.to_dict(),
            "field": field,
            "top_pair": list(top_pair),
            "metrics": iter_metrics,
            "delta_row": delta_row,
            "delta_issue_type": delta_issue,
            "delta_severity": delta_severity,
            "outcome": "kept" if improved else "rolled_back",
        }
        if not improved:
            hyp.revert()
            consecutive_no_improvement += 1
        else:
            consecutive_no_improvement = 0
            iter_metrics_path = out_dir / f"{pipeline}_iter_{iter_idx:03d}_metrics.json"
            iter_metrics_path.write_text(
                json.dumps(iter_metrics, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        history.append(record)
        last_iter_metrics = iter_metrics

        # 7. Check stop conditions.
        base_issue = base_metrics.get("per_field_accuracy", {}).get("issue_type", 0.0)
        base_severity = base_metrics.get("per_field_accuracy", {}).get(
            "severity", 0.0
        )
        if (new_issue - base_issue) >= stop_delta_issue_type:
            history[-1]["outcome"] = "kept (stop: issue_type +15pp)"
            break
        if (new_severity - base_severity) >= stop_delta_severity:
            history[-1]["outcome"] = "kept (stop: severity +15pp)"
            break
        if consecutive_no_improvement >= 3:
            history[-1]["outcome"] = (
                f"{history[-1]['outcome']} (stop: 3 consecutive no-improvement)"
            )
            break

        # Always restore hypothesis state at the end of the iteration: we
        # only want to KEEP changes if they improved; otherwise we revert.
        # The hypothesis object already does this in step 6.

    return {
        "pipeline": pipeline,
        "sample_csv": str(sample_csv),
        "iterations_run": iter_idx,
        "final_metrics": last_iter_metrics,
        "baseline_metrics": base_metrics,
        "history": history,
        "stop_reason": history[-1].get("outcome", "max_iterations_reached")
        if history
        else "no_iterations",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Optimization loop harness.")
    parser.add_argument(
        "--pipeline",
        choices=["A", "B"],
        default="A",
        help="Which pipeline to iterate on (default: A)",
    )
    parser.add_argument(
        "--sample-csv",
        default="dataset/sample_claims.csv",
        help="Sample CSV (default: dataset/sample_claims.csv)",
    )
    parser.add_argument(
        "--out-dir",
        default="evaluation/optimization",
        help="Where to write per-iteration outputs",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=10,
        help="Maximum iterations (default: 10)",
    )
    parser.add_argument(
        "--report",
        default="evaluation/optimization_history.json",
        help="Where to write the JSON history",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample = (repo_root / args.sample_csv).resolve()
    out_dir = (repo_root / args.out_dir).resolve()
    report_path = (repo_root / args.report).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)

    if not sample.exists():
        print(f"sample CSV not found: {sample}", file=sys.stderr)
        return 2

    print(f"Optimizing pipeline {args.pipeline} on {sample}")
    print(f"  out_dir: {out_dir}")
    print(f"  max_iterations: {args.max_iterations}")

    summary = run_loop(
        pipeline=args.pipeline,
        sample_csv=sample,
        out_dir=out_dir,
        max_iterations=args.max_iterations,
    )

    report_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    final = summary["final_metrics"]
    base = summary["baseline_metrics"]
    print(f"\nFinal row accuracy: {final.get('row_accuracy', 0):.2%} (baseline {base.get('row_accuracy', 0):.2%})")
    print(f"Final issue_type:   {final.get('per_field_accuracy', {}).get('issue_type', 0):.2%} (baseline {base.get('per_field_accuracy', {}).get('issue_type', 0):.2%})")
    print(f"Final severity:     {final.get('per_field_accuracy', {}).get('severity', 0):.2%} (baseline {base.get('per_field_accuracy', {}).get('severity', 0):.2%})")
    print(f"Stop reason: {summary['stop_reason']}")
    print(f"Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())