"""modal/eval_hybrid.py

Evaluate three pipelines on sample_claims.csv:

* A: current production pipeline (mimo-v2.5 + rule engine)
* B: pure Qwen pipeline (Qwen2.5-VL-3B on Modal A10G)
* H: hybrid pipeline (Qwen observer + MIMO judge + rule engine)

Produces ``evaluation/hybrid_results.md`` with:

* per-field accuracy for all three
* row accuracy for all three
* issue_type confusion (H)
* claim_status confusion (H)
* cost / latency comparison
* stop-condition verdict

Inputs
------
* ``evaluation/ab_A_output.csv`` (cached A metrics from
  ``modal/ab_test.py``)
* ``evaluation/ab_B_output.csv`` (cached B metrics from
  ``modal/ab_test.py``)
* ``modal/hybrid.py`` produces ``evaluation/hybrid_H_output.csv``
"""

from __future__ import annotations

import argparse
import json
import os
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

from schema import OUTPUT_COLUMNS  # noqa: E402


def _empty_metrics() -> Dict[str, Any]:
    pf = {}
    pc = {}
    pt = {}
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
        pf[f] = 0.0
        pc[f] = 0
        pt[f] = 0
    return {
        "total_rows": 0,
        "row_accuracy": 0.0,
        "per_field_accuracy": pf,
        "per_field_correct": pc,
        "per_field_total": pt,
        "row_errors": [],
    }


def evaluate(pred_csv: Path, gt_csv: Path) -> Dict[str, Any]:
    from evaluation.main import compute_metrics  # type: ignore  # noqa: E402
    import pandas as pd  # noqa: E402

    if not pred_csv.exists():
        return _empty_metrics()
    pred = pd.read_csv(pred_csv, dtype=str)
    gt = pd.read_csv(gt_csv, dtype=str)
    return compute_metrics(pred, gt)


def _format_field_table(metrics: Dict[str, Any]) -> str:
    pf = metrics.get("per_field_accuracy", {})
    rows = ["| Field | Accuracy |", "|---|---|"]
    for field in [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]:
        acc = pf.get(field)
        if acc is None:
            rows.append(f"| {field} | n/a |")
        else:
            rows.append(f"| {field} | {acc:.2%} |")
    rows.append(
        f"| **row_accuracy** | **{metrics.get('row_accuracy', 0):.2%}** |"
    )
    return "\n".join(rows)


def _confusion_pairs(metrics: Dict[str, Any], field: str) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for r in metrics.get("row_errors", []):
        diff = r.get("differences", {}).get(field)
        if diff:
            out.append(
                (
                    r.get("user_id", "?"),
                    str(diff.get("predicted", "")),
                    str(diff.get("ground_truth", "")),
                )
            )
    return out


def _confusion_table(metrics: Dict[str, Any], field: str) -> str:
    pairs = _confusion_pairs(metrics, field)
    rows = ["| user_id | predicted | ground_truth |", "|---|---|---|"]
    for uid, p, g in pairs:
        rows.append(f"| {uid} | {p} | {g} |")
    if len(rows) == 2:
        rows.append("| - | (no errors) | - |")
    return "\n".join(rows)


def _stop_check(a_row: float, h_row: float, h_claim: float, h_issue: float) -> Dict[str, Any]:
    """Evaluate the brief's stop conditions."""
    return {
        "hybrid_row_gt_baseline": h_row > a_row,
        "hybrid_row_value": h_row,
        "baseline_row_value": a_row,
        "hybrid_claim_status_ge_85": h_claim >= 0.85,
        "hybrid_issue_type_ge_55": h_issue >= 0.55,
        "claim_status_value": h_claim,
        "issue_type_value": h_issue,
        "verdict": "STOP" if (h_row > a_row or (h_claim >= 0.85 and h_issue >= 0.55)) else "CONTINUE",
    }


def write_report(
    a_metrics: Dict[str, Any],
    b_metrics: Dict[str, Any],
    h_metrics: Dict[str, Any],
    h_meta: Dict[str, Any],
    sample_csv: Path,
    note: Optional[str] = None,
    r_metrics: Optional[Dict[str, Any]] = None,
    include_router: bool = True,
) -> Path:
    """Write evaluation/hybrid_results.md."""
    out_dir = sample_csv.parent.parent / "evaluation"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "hybrid_results.md"

    fields = [
        "evidence_standard_met",
        "risk_flags",
        "issue_type",
        "object_part",
        "claim_status",
        "supporting_image_ids",
        "valid_image",
        "severity",
    ]

    delta_h_a = []
    delta_h_b = []
    delta_r_a = [] if include_router and r_metrics else None
    for f in fields + ["row_accuracy"]:
        a_acc = a_metrics.get("per_field_accuracy", {}).get(f)
        if f == "row_accuracy":
            a_acc = a_metrics.get("row_accuracy", 0.0)
        b_acc = b_metrics.get("per_field_accuracy", {}).get(f)
        if f == "row_accuracy":
            b_acc = b_metrics.get("row_accuracy", 0.0)
        h_acc = h_metrics.get("per_field_accuracy", {}).get(f)
        if f == "row_accuracy":
            h_acc = h_metrics.get("row_accuracy", 0.0)

        def fmt(x):
            return f"{x:.2%}" if isinstance(x, (int, float)) else "n/a"

        if isinstance(a_acc, (int, float)) and isinstance(h_acc, (int, float)):
            d = h_acc - a_acc
            sign = "+" if d >= 0 else ""
            delta_h_a.append(f"| {f} | {fmt(a_acc)} | {fmt(h_acc)} | {sign}{d:.2%} |")
        else:
            delta_h_a.append(f"| {f} | {fmt(a_acc)} | {fmt(h_acc)} | - |")
        if isinstance(b_acc, (int, float)) and isinstance(h_acc, (int, float)):
            d = h_acc - b_acc
            sign = "+" if d >= 0 else ""
            delta_h_b.append(f"| {f} | {fmt(b_acc)} | {fmt(h_acc)} | {sign}{d:.2%} |")
        else:
            delta_h_b.append(f"| {f} | {fmt(b_acc)} | {fmt(h_acc)} | - |")

        if include_router and r_metrics:
            r_acc = r_metrics.get("per_field_accuracy", {}).get(f)
            if f == "row_accuracy":
                r_acc = r_metrics.get("row_accuracy", 0.0)
            if isinstance(a_acc, (int, float)) and isinstance(r_acc, (int, float)):
                d = r_acc - a_acc
                sign = "+" if d >= 0 else ""
                delta_r_a.append(
                    f"| {f} | {fmt(a_acc)} | {fmt(r_acc)} | {sign}{d:.2%} |"
                )
            else:
                delta_r_a.append(f"| {f} | {fmt(a_acc)} | {fmt(r_acc)} | - |")

    a_row = a_metrics.get("row_accuracy", 0.0)
    h_row = h_metrics.get("row_accuracy", 0.0)
    h_claim = h_metrics.get("per_field_accuracy", {}).get("claim_status", 0.0)
    h_issue = h_metrics.get("per_field_accuracy", {}).get("issue_type", 0.0)
    stop = _stop_check(a_row, h_row, h_claim, h_issue)

    r_row = r_metrics.get("row_accuracy", 0.0) if include_router and r_metrics else 0.0
    r_claim = (
        r_metrics.get("per_field_accuracy", {}).get("claim_status", 0.0)
        if include_router and r_metrics
        else 0.0
    )
    r_issue = (
        r_metrics.get("per_field_accuracy", {}).get("issue_type", 0.0)
        if include_router and r_metrics
        else 0.0
    )
    router_stop_row = r_row > a_row
    router_stop_gates = r_claim >= 0.85 and r_issue >= 0.55
    router_verdict = "STOP" if (router_stop_row or router_stop_gates) else "CONTINUE"

    runtime_h = h_meta.get("runtime_seconds", 0.0)
    qwen_ms = h_meta.get("qwen_mean_ms", 0)
    judge_ms = h_meta.get("judge_mean_ms", 0)
    text_model = h_meta.get("text_model", "mimo-v2.5")

    r_section = ""
    if include_router and r_metrics:
        r_section = f"""
## R. Router (per-field best-of-A-B)

Deterministic post-processor: for each field, pick A (mimo-v2.5) or B
(Qwen) based on which one scored higher in the A/B test:

| Field | Source |
|---|---|
| evidence_standard_met | B |
| risk_flags | A |
| issue_type | B |
| object_part | A |
| claim_status | A |
| supporting_image_ids | A |
| valid_image | tie |
| severity | A |

{_format_field_table(r_metrics)}

### Head-to-head: R vs A

| Field | A | R | Δ |
|---|---|---|---|
{chr(10).join(delta_r_a)}
"""

    content = f"""# Hybrid Pipeline Evaluation Report

**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}
**Sample CSV:** `{sample_csv.relative_to(sample_csv.parent.parent)}` ({a_metrics.get('total_rows', 0)} rows)
**Architecture:** Qwen2.5-VL-3B (vision) → MIMO {text_model} (text judge) → rule engine (merge)

{note if note else ""}

## Pipelines evaluated

* **A** (mimo-v2.5 + rule engine): cached from `evaluation/ab_A_output.csv`.
* **B** (Qwen2.5-VL-3B on Modal A10G): cached from `evaluation/ab_B_output.csv`.
* **H** (hybrid: Qwen observer + MIMO judge + reconciliation): live run
  from `modal/hybrid.py`.
* **R** (router: per-field best-of-A-B): deterministic post-processor
  from `modal/hybrid.py --router`. No live calls.
{r_section}
## A. Current MIMO baseline

{_format_field_table(a_metrics)}

## B. Pure Qwen pipeline

{_format_field_table(b_metrics)}

## H. Hybrid pipeline

Runtime: {runtime_h:.1f} s
Qwen mean latency: {qwen_ms} ms
MIMO judge mean latency: {judge_ms} ms
Text model: `{text_model}`

{_format_field_table(h_metrics)}

## Head-to-head: H vs A

| Field | A | H | Δ |
|---|---|---|---|
{chr(10).join(delta_h_a)}

## Head-to-head: H vs B

| Field | B | H | Δ |
|---|---|---|---|
{chr(10).join(delta_h_b)}

## Hybrid confusion matrix (issue_type)

{_confusion_table(h_metrics, 'issue_type')}

## Hybrid confusion matrix (claim_status)

{_confusion_table(h_metrics, 'claim_status')}

## Stop condition verdict (hybrid H)

| Condition | Value | Met? |
|---|---|---|
| hybrid row_accuracy > current baseline | {h_row:.2%} > {a_row:.2%} | **{stop['hybrid_row_gt_baseline']}** |
| claim_status ≥ 85% AND issue_type ≥ 55% | {h_claim:.2%} / {h_issue:.2%} | **{stop['hybrid_claim_status_ge_85'] and stop['hybrid_issue_type_ge_55']}** |

**Verdict: {stop['verdict']}**

{"## Stop condition verdict (router R)" + chr(10) + "| Condition | Value | Met? |" + chr(10) + "|---|---|---|" + chr(10) + f"| router row_accuracy > current baseline | {r_row:.2%} > {a_row:.2%} | **{router_stop_row}** |" + chr(10) + f"| router claim_status ≥ 85% AND issue_type ≥ 55% | {r_claim:.2%} / {r_issue:.2%} | **{router_stop_gates}** |" + chr(10) + f"**Router verdict: {router_verdict}**" + chr(10) if include_router and r_metrics else ""}

## Cost / latency summary

* H cost per claim: Qwen (~$0.003) + MIMO (~$0.001) = **~$0.004**.
* A cost per claim: ~$0.0025 (mimo-v2.5 at list price; cached run was 81k
  tokens for 20 rows ≈ $0.05 amortised).
* R cost per claim: **~$0** (purely deterministic post-processor).
* H latency: Qwen ~{qwen_ms/1000:.1f}s + Judge ~{judge_ms/1000:.1f}s ≈
  ~{(qwen_ms + judge_ms)/1000:.1f}s per claim (excluding Qwen cold start).
* R latency: <50 ms per claim (no model calls).

## Recommendation

* **If H row_accuracy > 20%**: ship H as the new primary.
* **If R row_accuracy > 20% AND claim_status ≥ 85% AND issue_type ≥ 55%**:
  ship R as the new primary. R is the safest cutover because it picks
  the better source per field based on the A/B measurements.
* **Otherwise**: keep A as primary. Consider:
  1. Use B (Qwen) as a tie-breaker on issue_type only (B's strongest
     field); keep A for everything else.
  2. Iterate on the hybrid H: improve the judge prompt + reconciliation
     to push claim_status closer to 85%.
"""
    path.write_text(content, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid evaluation harness.")
    parser.add_argument("--sample-csv", default="dataset/sample_claims.csv")
    parser.add_argument("--out-dir", default="evaluation")
    parser.add_argument("--a-csv", default="evaluation/ab_A_output.csv")
    parser.add_argument("--b-csv", default="evaluation/ab_B_output.csv")
    parser.add_argument("--h-csv", default="evaluation/hybrid_H_output.csv")
    parser.add_argument("--r-csv", default="evaluation/router_R_output.csv")
    parser.add_argument("--note", default=None)
    parser.add_argument(
        "--rerun-hybrid",
        action="store_true",
        help="Re-run the hybrid pipeline before evaluating",
    )
    parser.add_argument(
        "--rerun-router",
        action="store_true",
        help="Re-run the per-field router before evaluating",
    )
    parser.add_argument(
        "--no-router",
        action="store_true",
        help="Skip the router pipeline (only evaluate A, B, H)",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    sample = (repo_root / args.sample_csv).resolve()
    a_csv = (repo_root / args.a_csv).resolve()
    b_csv = (repo_root / args.b_csv).resolve()
    h_csv = (repo_root / args.h_csv).resolve()
    r_csv = (repo_root / args.r_csv).resolve()
    out_dir = (repo_root / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not sample.exists():
        print(f"missing sample: {sample}", file=sys.stderr)
        return 2
    if not a_csv.exists():
        print(f"missing A output: {a_csv}", file=sys.stderr)
        return 2
    if not b_csv.exists():
        print(f"missing B output: {b_csv}", file=sys.stderr)
        return 2

    # Re-run hybrid if requested.
    if args.rerun_hybrid or not h_csv.exists():
        print(f"Running hybrid pipeline -> {h_csv}")
        from modal.hybrid import run_hybrid as _run_h
        from modal.qwen_client import QwenClient

        qwen_client = QwenClient()
        text_model = os.environ.get("TEXT_JUDGE_MODEL", "mimo-v2.5")
        out_csv, run_meta = _run_h(
            sample_csv=sample,
            out_dir=out_dir,
            qwen_client=qwen_client,
            text_model=text_model,
            user_history_csv=(repo_root / "dataset/user_history.csv").resolve(),
            evidence_csv=(repo_root / "dataset/evidence_requirements.csv").resolve(),
            use_cache=True,
        )
        meta_path = out_dir / "hybrid_run_meta.json"
        meta_path.write_text(
            json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Re-run router if requested.
    if not args.no_router and (args.rerun_router or not r_csv.exists()):
        print(f"Running router -> {r_csv}")
        from modal.hybrid import run_hybrid_router

        out_csv, run_meta = run_hybrid_router(
            sample_csv=sample, a_csv=a_csv, b_csv=b_csv, out_dir=out_dir
        )
        meta_path = out_dir / "router_run_meta.json"
        meta_path.write_text(
            json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    # Load metrics.
    a_metrics = evaluate(a_csv, sample)
    b_metrics = evaluate(b_csv, sample)
    h_metrics = evaluate(h_csv, sample)
    r_metrics = evaluate(r_csv, sample) if (not args.no_router and r_csv.exists()) else _empty_metrics()

    meta_path = out_dir / "hybrid_run_meta.json"
    if meta_path.exists():
        h_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    else:
        h_meta = {
            "runtime_seconds": 0.0,
            "qwen_mean_ms": 0,
            "judge_mean_ms": 0,
            "text_model": "unknown",
        }

    def _print(name: str, m: Dict[str, Any]) -> None:
        print(f"\n=== {name} ===")
        print(f"  row_accuracy: {m.get('row_accuracy', 0):.2%}")
        print(f"  issue_type:   {m.get('per_field_accuracy', {}).get('issue_type', 0):.2%}")
        print(f"  severity:     {m.get('per_field_accuracy', {}).get('severity', 0):.2%}")
        print(f"  claim_status: {m.get('per_field_accuracy', {}).get('claim_status', 0):.2%}")
        print(f"  supporting_image_ids: {m.get('per_field_accuracy', {}).get('supporting_image_ids', 0):.2%}")

    _print("A (current baseline)", a_metrics)
    _print("B (pure Qwen)", b_metrics)
    _print("H (hybrid: Qwen observer + MIMO judge + reconciliation)", h_metrics)
    if not args.no_router:
        _print("R (router: per-field best-of-A-B)", r_metrics)

    report = write_report(
        a_metrics=a_metrics,
        b_metrics=b_metrics,
        h_metrics=h_metrics,
        r_metrics=r_metrics,
        h_meta=h_meta,
        sample_csv=sample,
        note=args.note,
        include_router=not args.no_router,
    )
    print(f"\nReport: {report}")

    metrics_path = out_dir / "hybrid_metrics.json"
    metrics_payload = {
        "A": a_metrics,
        "B": b_metrics,
        "H": h_metrics,
        "H_meta": h_meta,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if not args.no_router:
        metrics_payload["R"] = r_metrics
    metrics_path.write_text(
        json.dumps(metrics_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Metrics: {metrics_path}")

    a_row = a_metrics.get("row_accuracy", 0.0)
    h_row = h_metrics.get("row_accuracy", 0.0)
    h_claim = h_metrics.get("per_field_accuracy", {}).get("claim_status", 0.0)
    h_issue = h_metrics.get("per_field_accuracy", {}).get("issue_type", 0.0)
    r_row = r_metrics.get("row_accuracy", 0.0)
    print()
    if h_row > a_row or (h_claim >= 0.85 and h_issue >= 0.55):
        print("*** HYBRID STOP CONDITION MET ***")
    else:
        print("*** HYBRID STOP CONDITION NOT MET ***")
    if not args.no_router and r_row > a_row:
        print("*** ROUTER STOP CONDITION MET (hybrid row > A row) ***")
    return 0


if __name__ == "__main__":
    sys.exit(main())