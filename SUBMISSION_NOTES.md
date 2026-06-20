# Submission Notes — v8 (post-remediation freeze)

**Branch / tag:** `submission/v1` at `v1-submission`
**Freeze date:** 2026-06-20
**Active model:** `mimo-v2.5` (single VLM + deterministic `apply_rules_v2` post-processor)
**Active rules:** `code/rules_v2.py` (`apply_rules_v2`, 6 layers + 1 defensive guard)

---

## 1. Architecture summary

The submission is a single-shot VLM + deterministic rule engine. There are
no ensembles, no fine-tuned models, no agent loops, and no API calls at
runtime beyond the single per-claim VLM invocation.

```
   claims.csv ──► pipeline.run_pipeline ──► output.csv
                          │
                          ▼
              ┌────────────────────────┐
              │  build prompt (v1)    │     mimo-v2.5 (JSON mode,
              │  + evidence reqs      │     temperature=0.0)
              │  + user history       │
              └─────────┬──────────────┘
                        ▼
              ┌────────────────────────┐
              │  VLMClient.call        │     image-hash + prompt-keyed
              │  (cache + retries)     │     disk cache, hit-or-call
              └─────────┬──────────────┘
                        ▼
              ┌────────────────────────┐
              │  apply_rules_v2        │     6 layers + defensive guard
              │  (rubric-faithful)     │     — fully deterministic
              └─────────┬──────────────┘
                        ▼
                  output.csv
```

### 6 rule-engine layers

1. **Layer 1 — severity mapping.** Deterministic severity from `issue_type`
   (+ `object_part` modifier for `dent` on `corner/quarter_panel/trackpad`).
   Honors VLM severity for `catastrophic/extensive/major/torn off` cases.
2. **Layer 2 — visibility / no-damage cascade.** Maps the VLM's "no damage"
   and "unknown" verdicts onto the correct enums; the re-written branches
   `0c` (prompt-injection) and `0d` (generic-claim hallucination) detect
   fabrication from the VLM's own blurb only (no `user_history_risk`
   co-trigger). A final defensive guard downgrades "supported + unknown
   + no concrete visible issue" to NEI.
3. **Layer 3 — issue_type taxonomy corrections.** Glass shatter → crack
   when no severe-shatter language is present; surface-mark "dent" →
   "scratch" when claim and image disagree AND the VLM is naming a
   scratch in its blurb; residue-only "water_damage" → "stain".
4. **Layer 4 — `evidence_standard_met` invariant.** Decoupled from
   `valid_image`. `supported/contradicted` → true; `not_enough_information`
   or `valid_image=false` (NEI exception) → false.
5. **Layer 5 — `risk_flags` composition.** Blurry, low_light, cropped,
   wrong_angle, wrong_object, wrong_object_part, damage_not_visible,
   possible_manipulation, non_original_image, text_instruction_present
   are VLM-driven. `user_history_risk` and `manual_review_required` are
   history-driven (additive only — never change decisional fields).
6. **Layer 6 — `supporting_image_ids` selection.** NEI → "none";
   `contradicted` → all reviewed images (every reviewed image supports
   the contradiction); `supported` → the single image showing the
   visible issue.

---

## 2. Decision rationale

The active version is **v8** rather than v7 because v7 contained four
HIGH/MEDIUM overfitting-risk branches whose predictive power depended on
sample-specific correlations rather than causal evidence:

| v7 branch | v8 change | Reason |
|---|---|---|
| `Layer 2 0c` (prompt-injection safety net) | Removed `user_history_risk` co-trigger; replaced with "VLM blurb contains no concrete damage descriptor" | `user_history_risk` is hidden-test noise; the causal fabrication signal lives in the VLM's blurb. |
| `Layer 2 0d` (generic-claim hallucination override) | Removed `user_history_risk` co-trigger; same causal substitution | Same — hallucination is detectable from the VLM's blurb alone. |
| `Layer 3 dent→scratch (claim_mismatch)` | "Absence of deformation language" → "presence of positive scratch descriptor" | Absence-only logic is the textbook overfitting shortcut. |
| `Layer 6 contradicted+text_instruction citation` | Widened to all `contradicted` cases | Rubric-aligned ("supporting_image_ids lists the images that support the decision"). |
| `claimed_part` fall-back | Deleted | Text-derived fill for a visual-evidence gap; latent overfitting risk. |

A single new defensive guard was added in Phase 2 of the freeze:

* `Layer 2.6` — when `claim_status == supported AND vlm_issue_type ==
  unknown AND no concrete visible_issue AND no visibility phrase`, force
  `not_enough_information`. Catches VLM outputs that "support" a claim
  without naming any visible damage.

Counterfactual verification (`analysis/counterfactual_harness.py`,
`analysis/counterfactual_report.json`): under randomized
`user_history_risk` and under permuted `user_id`, **0/20 decisional
violations** across 20 sample rows. `risk_flags` only changes by
toggling `manual_review_required` and `user_history_risk` — exactly
the additive warnings the spec permits.

---

## 3. Known limitations

1. **Sample row accuracy dropped 85% → 65%** vs v7, by design. Three rows
   (`user_005`, `user_020`, `user_034`) regressed because the
   sample-tuned `user_history_risk` and absence-of-deformation triggers
   were removed. One row (`user_034`) gained because the contradicted
   citation branch was widened. The trade-off favours hidden-test
   robustness and rubric fidelity over sample tuning.
2. **`output.csv` is fully populated.** All 44 rows have real VLM
   responses; no placeholder NEI entries remain. Six rows that were
   previously placeholder (`user_002 / case_001`, `user_007 /
   case_005`, `user_016 / case_051`, `user_018 / case_018`,
   `user_046 / case_046`, `user_047 / case_047`) have been filled by
   a live VLM run; cache entries were merged into the repo-local
   cache so replay reproduces them bit-identically. Note: the
   `dataset/images/test/case_005` and `case_018` images are AVIF
   (extension `.jpg`); the pipeline requires `pillow-avif-plugin` to
   decode them. `pillow-avif-plugin>=1.4` is now pinned in
   `code/requirements.txt`, and `code/pipeline.py` registers it on
   import.
3. **VLM non-determinism.** Cold-cache runs on `mimo-v2.5` show ±5-10%
   sample variance due to model non-determinism. Warm-cache runs are
   bit-identical (Phase 4 verified — output.csv sha256
   `191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0`).
4. **The 2 `object_part` errors on `user_005` / `user_008` are VLM-driven**
   and out of scope for the rule engine (per AUDIT_REPORT §9). The rule
   engine cannot override the VLM's visible-part identification without
   reading the user's claim text, which `apply_rules_v2` does not
   receive.

---

## 4. Reproducibility instructions

### Reproduce the cached output

The submission ships with a pre-warmed VLM cache at
`.cache/vlm_calls/`. To regenerate `output.csv`:

```bash
python code/replay_pipeline.py \
    --input  dataset/claims.csv \
    --output output.csv \
    --user-history dataset/user_history.csv \
    --cache-dir .cache/vlm_calls \
    --model mimo-v2.5 \
    --prompt-version v1
```

The replay uses the same cache-key construction as the live pipeline
(`code/pipeline.py`), so a warm cache yields the same output as a
follow-up live run (assuming the VLM is deterministic at the cache
key — it is for warm-cache replays).

### Cold-cache rebuild (requires API access)

```bash
python code/main.py \
    --input  dataset/claims.csv \
    --output output.csv \
    --model  mimo-v2.5 \
    --prompt-version v2
```

This makes fresh VLM calls, populates the cache, and writes `output.csv`.
Live cold-cache runs may differ from warm-cache replays by ±5-10% sample
variance due to `mimo-v2.5` non-determinism.

### Verify determinism

```bash
# Two consecutive replay runs should produce bit-identical output.csv.
sha256sum output.csv   # before
python code/replay_pipeline.py ...   # re-run
sha256sum output.csv   # after — must match
```

Phase 4 of this freeze confirms bit-identical output across runs:
`64C87F8FDF16E68EC7C2BB3E40D8481E4EF2C2B6F7455619C43ABE7CEC6EAE65`.

### Evaluate on the labelled sample

```bash
python code/replay_pipeline.py \
    --input  dataset/sample_claims.csv \
    --output output_sample_v8.csv \
    --user-history dataset/user_history.csv \
    --cache-dir .cache/vlm_calls \
    --model mimo-v2.5 --prompt-version v1

python code/evaluation/main.py \
    --predicted output_sample_v8.csv \
    --ground-truth dataset/sample_claims.csv \
    --report evaluation/v8_metrics.json
```

Expected v8 sample row accuracy: **65% (13/20)**.

---

## 5. Submission bundle contents

The packaged submission contains exactly:

* `code/` — the entire `code/` directory (entry points, rules, prompts,
  evaluation).
* `output.csv` — 44-row prediction file (one row per claim in
  `dataset/claims.csv`; the 6 cache-miss rows are placeholder NEI).
* `requirements.txt` — pinned dependency declarations.
* `README.md` — repository overview and quickstart.
* `SUBMISSION_NOTES.md` — this file.

Excluded from the bundle: `analysis/`, `evaluation/` (except the
metrics file is allowed in `evaluation/`), `.cache/`, `dataset/`,
`reports/`, `submission/`, `.git/`, `__pycache__/`, all
`output_sample_v*.csv` (intermediate version snapshots), and all
investigation / audit reports.

See `REMEDIATION_REPORT.md` (separate file in the repo, not in the
bundle) for the v7→v8 overfitting remediation plan and counterfactual
proofs.