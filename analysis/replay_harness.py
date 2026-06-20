"""Phase 0: Offline Replay Harness.

Re-applies a candidate rule engine to already-cached VLM outputs from
``evaluation/v6_report.json`` and re-scores against
``dataset/sample_claims.csv``. No API calls, no edits to existing pipeline
files, no risk of breaking the submission.

Import strategy
---------------
``code/rules.py`` does ``from schema import ...`` (not ``from code.schema``),
so we add ``<repo>/code`` to ``sys.path`` and import ``apply_rules`` directly.
``code/rules.py`` itself does NOT import from ``code/config``, so no API keys
are required. We still set dummy env vars for ``NVIDIA_API_KEY`` and
``MIMO_API_KEY`` defensively in case anything else indirectly pulls in
``code/config``. We also import the normalization helpers (``normalize_bool``,
``normalize_enum``, ``normalize_object_part``, ``normalize_severity``,
``select_primary_issue``, ``compose_risk_flags``, ``normalize_supporting_ids``)
from ``code.rules`` and ``schema.OUTPUT_COLUMNS`` from ``code.schema`` --
none of these need API keys.

Usage
-----
::

    python analysis/replay_harness.py [--candidate module:func]

With no flag, this reproduces v6 scores exactly (within +/-0%):
issue_type 0.40, severity 0.45, risk_flags 0.55, supporting_image_ids 0.70,
evidence_standard_met 0.85, claim_status 0.85, object_part 0.90,
valid_image 0.90, row_accuracy 0.20.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Path setup. We must add <repo>/code to sys.path BEFORE importing
# code.rules (which does ``from schema import ...``).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
CODE_DIR = REPO_ROOT / "code"
ANALYSIS_DIR = REPO_ROOT / "analysis"
DATASET_DIR = REPO_ROOT / "dataset"
EVAL_DIR = REPO_ROOT / "evaluation"

# Defensive: prevent any accidental import of code.config from blowing up
# because real API keys are missing. The harness never calls a VLM, but this
# keeps it import-safe in clean environments.
os.environ.setdefault("NVIDIA_API_KEY", "dummy-replay-harness")
os.environ.setdefault("MIMO_API_KEY", "dummy-replay-harness")

# Insert code/ first so ``from schema import ...`` resolves to code/schema.py.
sys.path.insert(0, str(CODE_DIR))

import pandas as pd  # noqa: E402

from rules import apply_rules as _baseline_apply_rules  # noqa: E402
from schema import OUTPUT_COLUMNS  # noqa: E402

REPLAY_OUTPUT_CSV = ANALYSIS_DIR / "replay_output.csv"
REPLAY_BASELINE_JSON = ANALYSIS_DIR / "replay_baseline.json"
SAMPLE_CLAIMS_CSV = DATASET_DIR / "sample_claims.csv"
USER_HISTORY_CSV = DATASET_DIR / "user_history.csv"
V6_REPORT_JSON = EVAL_DIR / "v6_report.json"


# ---------------------------------------------------------------------------
# Candidate rule engine.
# ---------------------------------------------------------------------------

def apply_candidate_rules(
    vlm_output: dict,
    user_history: dict,
    deterministic_quality_flags: list,
    claim_object: str,
) -> dict:
    """Drop-in replacement for ``code.rules.apply_rules`` with reordered args.

    Phase 0 default: simply delegate to the current ``code/rules.py apply_rules``
    so the harness reproduces v6 scores exactly. Later phases can swap this
    body for an improved candidate without touching anything else.
    """
    return _baseline_apply_rules(
        claim_object=claim_object,
        vlm_output=vlm_output,
        user_history=user_history,
        deterministic_quality_flags=deterministic_quality_flags,
    )


# ---------------------------------------------------------------------------
# Data loading.
# ---------------------------------------------------------------------------

def _load_vlm_outputs(v6_report_path: Path) -> List[Dict[str, Any]]:
    """Load 20 raw VLM JSON payloads from v6_report.json metadata."""
    with open(v6_report_path, "r", encoding="utf-8") as f:
        report = json.load(f)
    metadata = report.get("metadata", [])
    if len(metadata) != 20:
        # Sanity check; the brief says exactly 20 entries.
        print(
            f"WARNING: expected 20 metadata entries, got {len(metadata)}",
            file=sys.stderr,
        )
    parsed: List[Dict[str, Any]] = []
    for i, entry in enumerate(metadata):
        raw = entry.get("raw_content")
        if raw is None:
            raise ValueError(f"metadata[{i}] has no raw_content")
        if isinstance(raw, dict):
            parsed.append(raw)
        else:
            parsed.append(json.loads(raw))
    return parsed


def _load_user_history(path: Path) -> Dict[str, Dict[str, str]]:
    df = pd.read_csv(path, dtype=str).fillna("")
    return {str(row["user_id"]): row.to_dict() for _, row in df.iterrows()}


def _load_sample_claims(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str).fillna("")


# ---------------------------------------------------------------------------
# Candidate output CSV.
# ---------------------------------------------------------------------------

def _build_candidate_output(
    sample_claims: pd.DataFrame,
    vlm_outputs: List[Dict[str, Any]],
    user_history_map: Dict[str, Dict[str, str]],
    v6_metadata: List[Dict[str, Any]],
    apply_fn,
) -> pd.DataFrame:
    rows: List[Dict[str, str]] = []
    assert len(sample_claims) == len(vlm_outputs), (
        f"sample_claims has {len(sample_claims)} rows but v6_report has "
        f"{len(vlm_outputs)} raw outputs"
    )

    for idx in range(len(sample_claims)):
        claim_row = sample_claims.iloc[idx]
        user_id = str(claim_row["user_id"])
        claim_object = str(claim_row["claim_object"])
        image_paths = str(claim_row["image_paths"])
        user_claim = str(claim_row["user_claim"])

        vlm_output = vlm_outputs[idx]
        # deterministic_quality_flags were captured per-row in v6 metadata.
        # Fall back to [] if missing.
        dqf = v6_metadata[idx].get("deterministic_quality_flags") or []
        if dqf is None:
            dqf = []

        uh = user_history_map.get(user_id, {"history_flags": "none"})
        # Make sure history_flags exists.
        uh.setdefault("history_flags", "none")

        final = apply_fn(
            vlm_output=vlm_output,
            user_history=uh,
            deterministic_quality_flags=list(dqf),
            claim_object=claim_object,
        )

        rows.append(
            {
                "user_id": user_id,
                "image_paths": image_paths,
                "user_claim": user_claim,
                "claim_object": claim_object,
                "evidence_standard_met": final["evidence_standard_met"],
                "evidence_standard_met_reason": final["evidence_standard_met_reason"],
                "risk_flags": final["risk_flags"],
                "issue_type": final["issue_type"],
                "object_part": final["object_part"],
                "claim_status": final["claim_status"],
                "claim_status_justification": final["claim_status_justification"],
                "supporting_image_ids": final["supporting_image_ids"],
                "valid_image": final["valid_image"],
                "severity": final["severity"],
            }
        )

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


# ---------------------------------------------------------------------------
# Scoring. Re-implemented from code/evaluation/main.py::compute_metrics to
# avoid coupling and to keep the harness import-safe (compute_metrics itself
# imports from schema, which is fine, but we want zero risk of triggering
# code.config via the evaluation package).
# ---------------------------------------------------------------------------

SCORED_FIELDS = [
    "evidence_standard_met",
    "risk_flags",
    "issue_type",
    "object_part",
    "claim_status",
    "supporting_image_ids",
    "valid_image",
    "severity",
]


def _normalize(value: str) -> str:
    return str(value).strip().lower()


def _parse_risk_flags(flags: str) -> Set[str]:
    if not flags or _normalize(flags) == "none":
        return set()
    return {_normalize(f) for f in str(flags).split(";") if f.strip()}


def compute_metrics(predicted: pd.DataFrame, ground_truth: pd.DataFrame) -> Dict[str, Any]:
    predicted = predicted[OUTPUT_COLUMNS].copy()
    ground_truth = ground_truth[OUTPUT_COLUMNS].copy()

    if len(predicted) != len(ground_truth):
        raise AssertionError(
            f"Row counts differ: predicted={len(predicted)} gt={len(ground_truth)}"
        )

    per_field: Dict[str, Dict[str, Any]] = {
        f: {"correct": 0, "total": 0, "errors": []} for f in SCORED_FIELDS
    }

    row_correct = 0
    row_errors: List[Dict[str, Any]] = []

    for idx in range(len(predicted)):
        pred_row = predicted.iloc[idx]
        gt_row = ground_truth.iloc[idx]

        row_all_correct = True
        row_diff: Dict[str, Any] = {
            "index": idx,
            "user_id": pred_row.get("user_id", "?"),
            "claim_object": pred_row.get("claim_object", "?"),
            "differences": {},
        }

        for field in SCORED_FIELDS:
            p = pred_row.get(field, "")
            g = gt_row.get(field, "")
            correct = False

            if field == "risk_flags":
                p_set = _parse_risk_flags(str(p))
                g_set = _parse_risk_flags(str(g))
                correct = p_set == g_set
            else:
                correct = _normalize(str(p)) == _normalize(str(g))

            per_field[field]["total"] += 1
            if correct:
                per_field[field]["correct"] += 1
            else:
                row_all_correct = False
                row_diff["differences"][field] = {
                    "predicted": str(p),
                    "ground_truth": str(g),
                }
                per_field[field]["errors"].append(
                    {
                        "index": idx,
                        "user_id": pred_row.get("user_id", "?"),
                        "predicted": str(p),
                        "ground_truth": str(g),
                    }
                )

        if row_all_correct:
            row_correct += 1
        else:
            row_errors.append(row_diff)

    return {
        "total_rows": len(predicted),
        "row_accuracy": row_correct / len(predicted),
        "per_field_accuracy": {
            f: per_field[f]["correct"] / per_field[f]["total"] for f in SCORED_FIELDS
        },
        "per_field_correct": {f: per_field[f]["correct"] for f in SCORED_FIELDS},
        "per_field_total": {f: per_field[f]["total"] for f in SCORED_FIELDS},
        "row_errors": row_errors,
    }


def _print_metrics(metrics: Dict[str, Any]) -> None:
    print(f"Total rows: {metrics['total_rows']}")
    print(f"Row accuracy: {metrics['row_accuracy']:.2%}")
    print("Per-field accuracy:")
    for field, acc in metrics["per_field_accuracy"].items():
        print(
            f"  {field}: {acc:.2%} "
            f"({metrics['per_field_correct'][field]}/{metrics['per_field_total'][field]})"
        )
    if metrics["row_errors"]:
        print(f"Row errors ({len(metrics['row_errors'])}):")
        for re_ in metrics["row_errors"]:
            print(f"  idx={re_['index']} user_id={re_['user_id']} "
                  f"claim_object={re_['claim_object']}")
            for fname, diff in re_["differences"].items():
                print(f"    {fname}: pred={diff['predicted']!r} gt={diff['ground_truth']!r}")


# ---------------------------------------------------------------------------
# Candidate import helper for --candidate module:func.
# ---------------------------------------------------------------------------

def _load_candidate(spec: str):
    """Load ``module:func`` from spec. The returned callable should accept the
    same kwargs as ``apply_candidate_rules`` (``vlm_output``, ``user_history``,
    ``deterministic_quality_flags``, ``claim_object``) and return a dict.
    """
    if ":" not in spec:
        raise ValueError(
            f"--candidate must be in module:func form, got {spec!r}"
        )
    mod_name, func_name = spec.split(":", 1)
    # Allow relative module names by anchoring to repo root.
    if mod_name.startswith("analysis.") or mod_name.startswith("code."):
        sys.path.insert(0, str(REPO_ROOT))
    module = importlib.import_module(mod_name)
    func = getattr(module, func_name)
    if not callable(func):
        raise TypeError(f"{spec} is not callable")
    return func


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline replay harness for the rule engine.",
    )
    parser.add_argument(
        "--candidate",
        type=str,
        default=None,
        help="Optional module:func to use instead of the default apply_candidate_rules. "
             "Must accept (vlm_output, user_history, deterministic_quality_flags, claim_object).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=str(REPLAY_OUTPUT_CSV),
        help="Path to write the candidate output CSV.",
    )
    parser.add_argument(
        "--report",
        type=str,
        default=str(REPLAY_BASELINE_JSON),
        help="Path to write the metrics JSON report.",
    )
    args = parser.parse_args(argv)

    apply_fn = apply_candidate_rules
    label = "default (code/rules.py apply_rules)"
    if args.candidate:
        apply_fn = _load_candidate(args.candidate)
        label = f"candidate {args.candidate}"
    print(f"Using rule engine: {label}")

    # Load sources.
    with open(V6_REPORT_JSON, "r", encoding="utf-8") as f:
        v6_report = json.load(f)
    v6_metadata = v6_report["metadata"]
    vlm_outputs = _load_vlm_outputs(V6_REPORT_JSON)
    user_history_map = _load_user_history(USER_HISTORY_CSV)
    sample_claims = _load_sample_claims(SAMPLE_CLAIMS_CSV)

    # Build candidate output.
    candidate_df = _build_candidate_output(
        sample_claims=sample_claims,
        vlm_outputs=vlm_outputs,
        user_history_map=user_history_map,
        v6_metadata=v6_metadata,
        apply_fn=apply_fn,
    )

    # Write output CSV (exact 14-column order from OUTPUT_COLUMNS).
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    candidate_df.to_csv(output_path, index=False, quoting=csv.QUOTE_MINIMAL)
    print(f"Wrote candidate output: {output_path} "
          f"({len(candidate_df)} rows, {len(candidate_df.columns)} cols)")

    # Score.
    metrics = compute_metrics(candidate_df, sample_claims)
    _print_metrics(metrics)

    # Write metrics JSON.
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    print(f"Wrote metrics report: {report_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())