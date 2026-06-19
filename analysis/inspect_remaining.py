"""Inspect raw VLM outputs for the rows that still fail in v2_all_on.

For each remaining-error row, print: VLM's issue_type, severity, claim_status,
justification, evidence_standard_met_reason, visible_issues, image_quality_flags,
and the GT values. This is to design more aggressive rule-engine heuristics.
"""
import json, os, sys
from pathlib import Path

REPO_ROOT = Path(r"C:\Users\arsat\OneDrive\Desktop\hackerrank2\hackerrank-orchestrate-june26")
sys.path.insert(0, str(REPO_ROOT))
os.environ.setdefault("NVIDIA_API_KEY", "dummy")
os.environ.setdefault("MIMO_API_KEY", "dummy")
sys.path.insert(0, str(REPO_ROOT / "code"))

import pandas as pd
from analysis.ablation_rules_v2 import load_metadata, load_gt, parse_raw, make_apply_v2, load_user_history

metadata = load_metadata()
gt_df = load_gt()
user_history_map = load_user_history()
all_on = make_apply_v2()
m = all_on  # callable

# Run the fixed v2 engine to find remaining errors
rows = []
for idx, meta in enumerate(metadata):
    gt_row = gt_df.iloc[idx]
    user_id = str(gt_row["user_id"])
    vlm = parse_raw(meta.get("raw_content", ""))
    det = meta.get("deterministic_quality_flags", []) or []
    obj = str(gt_row["claim_object"]).lower().strip()
    uh = user_history_map.get(user_id, {})
    if vlm:
        out = all_on(vlm, uh, det, obj)
    else:
        continue
    rows.append({"idx": idx, "user_id": user_id, "gt": gt_row.to_dict(), "pred": out, "vlm": vlm})

# Find rows with any error
def norm(s): return str(s).strip().lower()
def parse_rf(s):
    if not s or norm(s) == "none": return set()
    return {norm(f) for f in str(s).split(";") if f.strip()}

fields = ["issue_type", "severity", "claim_status", "supporting_image_ids", "valid_image", "evidence_standard_met", "object_part", "risk_flags"]

for r in rows:
    errs = []
    for f in fields:
        pv = r["pred"].get(f, "")
        gv = r["gt"].get(f, "")
        if f == "risk_flags":
            ok = parse_rf(pv) == parse_rf(gv)
        else:
            ok = norm(pv) == norm(gv)
        if not ok:
            errs.append(f)
    if errs:
        print("=" * 80)
        print(f"idx={r['idx']} user={r['user_id']} obj={r['gt']['claim_object']}  FAILING: {errs}")
        print(f"  user_claim: {str(r['gt']['user_claim'])[:140]}")
        v = r["vlm"]
        print(f"  VLM issue_type={v.get('issue_type')!r} severity={v.get('severity')!r} claim_status={v.get('claim_status')!r}")
        print(f"  VLM justification: {str(v.get('claim_status_justification',''))[:200]}")
        print(f"  VLM evidence_reason: {str(v.get('evidence_standard_met_reason',''))[:200]}")
        print(f"  VLM visible_issues: {v.get('visible_issues', [])}")
        print(f"  VLM wrong_object={v.get('wrong_object')} wrong_object_part={v.get('wrong_object_part')} claim_mismatch={v.get('claim_mismatch')}")
        print(f"  VLM image_quality_flags: {v.get('image_quality_flags', [])}")
        print(f"  PRED: issue={r['pred'].get('issue_type')!r} sev={r['pred'].get('severity')!r} status={r['pred'].get('claim_status')!r} supp={r['pred'].get('supporting_image_ids')!r} valid={r['pred'].get('valid_image')!r} evid={r['pred'].get('evidence_standard_met')!r} part={r['pred'].get('object_part')!r}")
        print(f"  GT:   issue={r['gt'].get('issue_type')!r} sev={r['gt'].get('severity')!r} status={r['gt'].get('claim_status')!r} supp={r['gt'].get('supporting_image_ids')!r} valid={r['gt'].get('valid_image')!r} evid={r['gt'].get('evidence_standard_met')!r} part={r['gt'].get('object_part')!r} risk={r['gt'].get('risk_flags')!r}")
        print(f"  PRED risk: {r['pred'].get('risk_flags')!r}")
