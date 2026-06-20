"""Debug helper: print the v2_all_on row_errors and compare to baseline."""
import json, os, sys
from pathlib import Path

REPO_ROOT = Path(r"C:\Users\arsat\OneDrive\Desktop\hackerrank2\hackerrank-orchestrate-june26")
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")
sys.path.insert(0, str(REPO_ROOT / "code"))

from analysis.ablation_rules_v2 import (
    load_metadata, load_gt, load_user_history, parse_raw,
    make_apply_v2, apply_v1_adapter, run_config, compute_metrics,
)
import pandas as pd

metadata = load_metadata()
gt_df = load_gt()
user_history = load_user_history()

# Run baseline and all_on, compare
baseline = run_config("baseline_v1", apply_v1_adapter, metadata, gt_df, user_history)
all_on = run_config("v2_all_on", make_apply_v2(), metadata, gt_df, user_history)

b_errs = {e["index"]: e for e in baseline["metrics"]["row_errors"]}
a_errs = {e["index"]: e for e in all_on["metrics"]["row_errors"]}

print("=" * 80)
print("REGRESSIONS: rows correct in baseline but wrong in v2_all_on")
print("=" * 80)
for idx in sorted(set(b_errs) - set(a_errs)):
    e = b_errs[idx]
    a = all_on["pred_df"].iloc[idx]
    g = gt_df.iloc[idx]
    print(f"\n[baseline-correct->v2-wrong] idx={idx} user_id={e['user_id']} claim_object={a['claim_object']}")
    for f, d in e["differences"].items():
        print(f"  {f}: predicted={d['predicted']!r} gt={d['ground_truth']!r}")

print("\n" + "=" * 80)
print("FIXES: rows wrong in baseline but correct in v2_all_on")
print("=" * 80)
for idx in sorted(set(a_errs) - set(b_errs)):
    e = a_errs[idx]
    b = baseline["pred_df"].iloc[idx]
    g = gt_df.iloc[idx]
    print(f"\n[baseline-wrong->v2-correct] idx={idx} user_id={e['user_id']} claim_object={b['claim_object']}")
    for f, d in e["differences"].items():
        print(f"  {f}: predicted={d['predicted']!r} gt={d['ground_truth']!r}")

print("\n" + "=" * 80)
print("REMAINING ERRORS in v2_all_on (per row, per field)")
print("=" * 80)
for idx in sorted(a_errs):
    e = a_errs[idx]
    a = all_on["pred_df"].iloc[idx]
    g = gt_df.iloc[idx]
    print(f"\n[REMAINING] idx={idx} user_id={e['user_id']} claim_object={a['claim_object']}")
    print(f"  user_claim: {str(g['user_claim'])[:120]}")
    for f, d in e["differences"].items():
        print(f"  {f}: predicted={d['predicted']!r} gt={d['ground_truth']!r}")
