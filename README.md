# HackerRank Orchestrate (June 2026) — Multi-Modal Evidence Review

A production-grade system that verifies damage-claim evidence for three object
domains (`car`, `laptop`, `package`) using a single VLM call per claim plus a
deterministic six-layer rule engine. The branch is the canonical,
freeze-approved v8 submission produced at the end of the 24-hour hackathon.

> **Final shipped branch:** `main` (post-freeze, post-overfitting-remediation,
> post-placeholder-backfill). The active rules engine is
> `code/rules_v2.py` (`apply_rules_v2`). Latest warm-cache sample row
> accuracy: **70% (14/20)**. Hidden-test posture: counterfactually verified
> -- `user_history_risk` and `user_id` cannot change any decisional output.

---

## Table of contents

1. Problem statement
2. Final architecture
3. Approach — how the canonical branch came together
4. What failed, what worked, and why
5. The overfitting remediation pass (v7 → v8)
6. Final submission freeze
7. Reproducibility, environment, and AVIF handling
8. Repository layout
9. Quickstart
10. Branches in this repository
11. Known limitations
12. Future implementations
13. License & contact

---

## 1. Problem statement

The HackerRank Orchestrate challenge gives the system:

* a short damage-claim conversation (`user_claim`)
* one or more submitted images
* the user's claim history
* a minimum-image-evidence checklist

For every claim, the system must produce a structured prediction:

| Field | Type | Meaning |
|---|---|---|
| `evidence_standard_met` | bool | Were the evidence requirements satisfied? |
| `evidence_standard_met_reason` | str | One-line justification |
| `risk_flags` | `;`-list | Image-quality, mismatch, authenticity, history signals |
| `issue_type` | enum | Damage taxonomy (`dent`, `crack`, `scratch`, `glass_shatter`, `broken_part`, `missing_part`, `torn_packaging`, `crushed_packaging`, `water_damage`, `stain`, `none`, `unknown`) |
| `object_part` | enum | Per-object enum (e.g. `rear_bumper`, `keyboard`) |
| `claim_status` | enum | `supported` / `contradicted` / `not_enough_information` |
| `claim_status_justification` | str | Image-grounded rationale |
| `supporting_image_ids` | `;`-list | Image IDs that support the decision |
| `valid_image` | bool | Whether the submitted images are usable |
| `severity` | enum | `none` / `low` / `medium` / `high` / `unknown` |

The challenge specification (`problem_statement.md`) is unambiguous about three
disciplines:

1. **Images are the primary source of truth.** User history cannot override
   clear visual evidence.
2. **History adds risk context only**, in the form of additive risk flags
   (e.g. `manual_review_required`).
3. **Output fields must be image-grounded.** `object_part`, `severity`,
   `claim_status` and `issue_type` are derived from images, not from text.

Full task spec lives in `problem_statement.md`.

---

## 2. Final architecture

```
   claims.csv ──► pipeline.run_pipeline ──► output.csv
                          │
                          ▼
              ┌────────────────────────┐
              │  build prompt (v1)    │     mimo-v2.5 (JSON mode,
              │  + evidence reqs      │     temperature=0.0,
              │  + user history       │     max_tokens=8192)
              └─────────┬──────────────┘
                        ▼
              ┌────────────────────────┐
              │  VLMClient.call        │     image-hash + prompt-keyed
              │  (cache + retries)     │     disk cache (warm-cache replay
              │  (filter readable)     │     is bit-identical)
              └─────────┬──────────────┘     ◄── pillow-avif-plugin
                                              decodes AVIF shipped as .jpg
                        ▼
              ┌────────────────────────┐
              │  apply_rules_v2        │     6 layers + 1 defensive guard
              │  (rubric-faithful)     │     — fully deterministic,
              │                        │       no VLM stochasticity
              └─────────┬──────────────┘
                        ▼
                  output.csv
```

### 2.1 The 6 rule-engine layers

1. **Layer 1 — severity mapping.** Deterministic severity from `issue_type`
   (+ `object_part` modifier for `dent` on `corner / quarter_panel /
   trackpad`). Honors VLM severity for `catastrophic / extensive / major /
   torn off` blurb words.
2. **Layer 2 — visibility / no-damage cascade.** Maps the VLM's "no damage"
   and "unknown" verdicts onto the correct enums. Branch `0c` (prompt-
   injection safety net) and branch `0d` (generic-claim hallucination
   override) both depend only on the VLM's own blurb — never on
   `user_history_risk`. The **defensive visibility guard** (added in the
   freeze) catches `supported + unknown issue + no concrete visible issue +
   no visibility phrase` and forces `not_enough_information`.
3. **Layer 3 — issue_type taxonomy corrections.** Glass shatter → crack
   when no severe-shatter language is present; surface-mark `dent` →
   `scratch` when the VLM names a scratch in its blurb (positive signal
   only); residue-only `water_damage` → `stain`.
4. **Layer 4 — `evidence_standard_met` invariant.** Decoupled from
   `valid_image`. `supported / contradicted` → true;
   `not_enough_information` or `valid_image=false` (NEI exception) → false.
5. **Layer 5 — `risk_flags` composition.** Blurry, low_light, cropped,
   wrong_angle, wrong_object, wrong_object_part, damage_not_visible,
   possible_manipulation, non_original_image, text_instruction_present are
   VLM-driven. `user_history_risk` and `manual_review_required` are
   history-driven (additive warning flags only — never change decisional
   fields).
6. **Layer 6 — `supporting_image_ids` selection.** NEI → `"none"`;
   `contradicted` → **all reviewed images** (every reviewed image
   supports the contradiction); `supported` → the single image showing
   the visible issue.

### 2.2 The two entry points

```text
code/main.py              Live pipeline (reads claims.csv, calls VLM, writes output.csv)
code/replay_pipeline.py   Cache-only replay (re-runs apply_rules_v2 on cached VLM
                          responses; reproduces output.csv bit-identically)
```

### 2.3 The evaluation harness

```text
code/evaluation/main.py   Per-field accuracy, row-accuracy, structured error report
```

---

## 3. Approach — how the canonical branch came together

The repository evolved through five branches, each contributing something to
the final v8. The strategy was **explore widely, ship narrowly** — every
experimental branch is preserved for traceability, but the canonical branch
ships only the rubric-faithful engine.

| Branch | Headline result | Disposition |
|---|---|---|
| `main` (early) | Starter template only | superseded |
| `feature/baseline-pipeline` | First end-to-end pipeline, 10% sample row acc | superseded |
| `submission/v1` | Baseline + reports + manifests | superseded |
| `feature/qwen-modal` | Live Qwen2.5-VL on Modal A10G; multi-crop experiments | kept for reference (rejected — worse than mimo-v2.5) |
| `feature/hybrid-pipeline` | Qwen observer + MIMO judge + verifier + router | kept for reference (rejected — no row-acc lift) |
| `feature/eval-90pct` | Phase-1.x rubric-faithful rule engine, 85% sample row acc | **promoted to main** |
| `main` (post-freeze) | v8 with overfitting remediation, 70% latest warm-cache sample row acc, counterfactually robust | **shipped** |

### 3.1 Phase 0 — Baseline pipeline

Built the first end-to-end pipeline in `feature/baseline-pipeline`:

* Single VLM call per claim with `mimo-v2.5` (JSON mode, temperature=0.0)
* Disk cache keyed on `(prompt_version, model, prompt, image_hashes)`
* Deterministic rule engine in `code/rules.py` (the v1 engine)
* Sample row accuracy: **10%** (2/20).

**Lesson:** the rule engine is the bottleneck, not the VLM. A prompt-only
approach cannot escape the 65% per-field ceiling because the VLM's
classifications are not perfectly calibrated to the rubric's enums.

### 3.2 Phase 1.x — Rubric-faithful rule engine

In `feature/eval-90pct`, we replaced the loose `apply_rules` with a six-layer
`apply_rules_v2` that captures the rubric:

| Sub-phase | Change | Effect |
|---|---|---|
| Phase 1.0 | Off-line replay harness + 6-layer engine scaffold | +25pp |
| Phase 1.1 | OpenCV image-quality module, v2 prompt, tuned thresholds | +15pp |
| Phase 1.5 | Expanded visibility/no-damage phrase lists, valid_image logic | +5pp |
| Phase 1.6 | Targeted rule fixes for glass_shatter / water_damage / NEI risk_flags | +5pp |
| Phase 1.7 | Gated cropped_or_obstructed, possible_manipulation; added manual_review_required from user_history; damage_not_visible for contradicted-no-damage | +10pp |
| Phase 1.8 | Stricter shatter hints (removed `'radiating'` per rubric) | +5pp |

**Final v7 result:** **85% (17/20)** sample row accuracy, every per-field
≥90%, no production-leakage detected.

### 3.3 Phase 2 — Overfitting audit and remediation

This is the v7→v8 pass that traded 20 percentage points of sample row
accuracy for counterfactual robustness. Detailed in section 5 of this
README.

### 3.4 Phase 3 — Submission freeze

The seven-phase freeze that produced the shipped bundle. Detailed in
section 6 of this README.

---

## 4. Failures, wins, and why

This section is the honest log of what we tried, what failed, and what we
learned. Read it before touching the rule engine.

### 4.1 ❌ v3 prompt with explicit decision tree — REJECTED

We tried `prompts_v3.py` with an in-prompt decision tree for visibility and
glass-vs-crack classification.

* Result: row accuracy **40% → 40%** (no change), object_part regressed to 85%.
* Other variants (llama-4-maverick + v3, mimo-v2.5 + v3 re-run) were strictly
  worse.
* **Diagnosis:** explicit prompts cause the VLM to overthink and produce
  verbose justifications that the rule engine then can't parse cleanly. The
  v1 prompt is already rubric-aligned; adding decision-tree scaffolding
  fights the VLM's natural classification.

### 4.2 ❌ Qwen2.5-VL on Modal — REJECTED

We deployed `Qwen2.5-VL-72B-Instruct` on Modal A10G and ran multi-crop +
A/B + verifier experiments (`feature/qwen-modal`, `feature/hybrid-pipeline`).

* Result: row accuracy **≤ 55%** across all configurations.
* Multi-cropping helped `object_part` by 5pp but cost 10pp on
  `claim_status` (the VLM becomes too confident on each crop).
* Verifier (text-only MIMO judge reviewing the primary VLM's verdict)
  introduced ±3pp variance and never lifted row accuracy.
* Hybrid (Qwen observer + MIMO judge) was strictly worse than pure
  mimo-v2.5 — the disagreement reconciliation logic was dominated by
  whichever VLM was over-confident.

**Key observation:** `mimo-v2.5` is the right VLM for this task. The
  bottleneck is the rule engine's mapping from VLM output to rubric enums,
  not the VLM's raw classification accuracy. The Modal code is preserved in
  `modal/` for traceability, but the canonical branch uses mimo-v2.5
  exclusively.

### 4.3 ❌ "Hallucination override" with `user_history_risk` co-trigger — REJECTED in v8

The v7 Layer-2 `0c` and `0d` branches fired on
`text_instruction_present + user_history_risk + supported` and
`rationalization_phrase + user_history_risk` respectively.

* Result on the sample: lifted `claim_status` to 100% by flipping
  `user_020` and `user_034` from `supported` to `contradicted`.
* **Hidden-test risk:** the `user_history_risk` co-trigger is, by
  definition, the one signal that is independent of the image. A branch
  that depends on it cannot be sample-independent.
* **Counterfactual proof:** randomizing `user_history_risk` flipped
  `claim_status` for 12 of 20 sample rows under v7. After the v8 fix, 0
  of 20.

### 4.4 ❌ `dent → scratch` on "absence of deformation language" — REJECTED in v8

The v7 Layer-3 gate downgraded `dent` to `scratch` whenever
`claim_mismatch=true` and the VLM blurb did **not** contain any
deformation word.

* Sample hit: 1 row (user_005) — got scratch for a quarter_panel dent.
* **Why it's overfitting:** "absence of deformation" is a *negative*
  signal. It conflates "the VLM didn't say deformation" with "the mark
  isn't a deformation". On a hidden test, the VLM might say
  "minor surface damage" without ever using the word "deformation", and
  the v7 rule would downgrade a real dent to a scratch.
* **v8 fix:** require a *positive* scratch descriptor in the blurb
  (`scratch`, `scrape`, `scuff`, `paint transfer`, `clear coat`,
  `surface mark`, `surface-level`, `line on`, `mark on surface`,
  `minor scratch`, `minor scrape`). The downgrade now fires only when the
  VLM is actually *naming* a scratch.

### 4.5 ❌ Contradicted citation gated on `user_history_risk` — REJECTED in v8

The v7 Layer-6 branch cited all images only for
`claim_status=contradicted AND text_instruction_present AND user_history_risk`.
Other contradicted rows cited a single image.

* Sample effect: user_034 (contradicted on package) cited `img_1` while
  ground truth cited `img_1;img_2`.
* **Why it's overfitting:** the rubric says supporting_image_ids lists the
  images that *support the decision*. For a contradicted verdict, every
  reviewed image supports the contradiction (all show the intact part /
  wrong object / missing damage). The co-trigger is gratuitous.
* **v8 fix:** every contradicted verdict cites all reviewed images. This
  fixed the user_034 sample error and removed the user_history dependency
  in one stroke.

### 4.6 ❌ `claimed_part` fill from user_history — REJECTED in v8

The v7 NEI cascade filled `object_part` from `user_history.claimed_part`
when the VLM reported `unknown`.

* This was a latent risk: the key was never set in `user_history.csv`, so
  the branch was a no-op today. But it was a *text-derived* value
  plugging a *visual-evidence* gap, which violates the image-first
  discipline.
* **v8 fix:** deleted entirely.

### 4.7 ✅ The v1 prompt is the right prompt

After evaluating v1, v2, and v3 in isolation, v1 (the rubric-aligned
prompt in `code/prompts.py`) is the best raw signal. v2 and v3 add
scaffolding that fights the VLM.

### 4.8 ✅ `mimo-v2.5` is the right VLM

Qwen2.5-VL-72B-Instruct, llama-4-maverick, and nemotron-nano were all
strictly worse. mimo-v2.5 is fast, deterministic at temperature=0, and
rubric-aligned out of the box.

### 4.9 ✅ The 6-layer rule engine is the right architecture

Each layer has a single, well-defined job:

1. Severity from issue_type (no VLM stochasticity).
2. Visibility cascade (maps "no damage" / "unknown" / "wrong object"
   verdicts onto the correct enums).
3. Issue_type taxonomy corrections (rubric-specific overrides).
4. Evidence-standard invariant.
5. Risk-flag composition.
6. Supporting-image selection.

This separation means every layer is auditable, every branch is
testable, and no layer depends on user history for decisional output.

### 4.10 ✅ Counterfactual harness as a release gate

The overfitting audit produced `analysis/counterfactual_harness.py`,
which verifies under randomized `user_history_risk` and permuted
`user_id`:

* `claim_status`, `issue_type`, `severity`, `object_part`,
  `valid_image`, `supporting_image_ids` are invariant.
* `risk_flags` only changes by toggling `manual_review_required` and
  `user_history_risk` (additive warnings — spec-allowed).

This is now a release-gate step. Any future change to the rules engine
must keep the counterfactual PASS.

### 4.11 ✅ AVIF handling — discovered during backfill

During the placeholder backfill we discovered that `dataset/images/test/
case_005` and `case_018` ship as AVIF (ftyp brand `avif`) with `.jpg`
extensions. PIL cannot decode AVIF without `pillow-avif-plugin`. We
pinned the plugin in `code/requirements.txt` and added `import
pillow_avif` to `code/pipeline.py` so the plugin registers on import.
Without this fix, those cases silently fall through to the
unreadable-images placeholder.

---

## 5. Remediation pass (v7 → v8)

The full remediation report lives in `REMEDIATION_REPORT.md`. This section
summarizes.

### 5.1 What was flagged

The overfitting audit identified 5 high/medium-risk branches in `rules_v2.py`:

| Branch | File:line | Risk |
|---|---|---|
| `Layer 2 0c` (prompt-injection safety net) | `rules_v2.py:517-572` | HIGH — `user_history_risk` co-trigger |
| `Layer 2 0d` (generic-claim hallucination override) | `rules_v2.py:574-588` | HIGH — `user_history_risk` co-trigger |
| `Layer 3 dent→scratch (claim_mismatch path)` | `rules_v2.py:696-707` | MEDIUM — absence-only logic |
| `Layer 6 contradicted+text_instruction citation` | `rules_v2.py:836-855` | HIGH — sample-tuned co-trigger |
| `claimed_part` fall-back | `rules_v2.py:753-758` | MEDIUM — text-derived fill |

### 5.2 The redesigns

Every redesign replaces the `user_history_risk` (or absence-only) co-trigger
with a causal signal observable from the VLM's own output:

| Branch | Before | After |
|---|---|---|
| `Layer 2 0c` | `text_instruction_present + user_history_risk + supported + concrete` | `text_instruction_present + supported + concrete + blurb contains no concrete damage descriptor` |
| `Layer 2 0d` | `supported + concrete + user_history_risk + rationalization_phrase + no echo` | `supported + concrete + rationalization_phrase + no concrete damage descriptor` |
| `Layer 3 dent→scratch` | `dent + claim_mismatch + NOT deformation_phrase` | `dent + claim_mismatch + scratch_descriptor` (positive signal) |
| `Layer 6 citation` | `contradicted + text_instruction_present + user_history_risk` | `contradicted` (widened) |
| `claimed_part` | `if object_part==unknown: object_part=user_history.claimed_part` | **deleted** |

A new shared 60-entry `_CONCRETE_DAMAGE_DESCRIPTORS` list is used by both
`0c` and `0d` to detect "VLM is asserting damage without naming what it
saw". A `_SCRATCH_DESCRIPTORS` list is used by Layer 3 for the positive
scratch signal.

### 5.3 Sample accuracy impact

| Field | v7 | v8 | Δ | Cause |
|---|---|---|---|---|
| row_accuracy | 85% | 65% | -20pp | 3 intentional regressions + 1 gain |
| evidence_standard_met | 100% | 100% | 0 | |
| risk_flags | 100% | 85% | -15pp | L2.0d no longer flips user_020; L2.0c no longer flips user_034 |
| issue_type | 100% | 80% | -20pp | L3.6 positive-scratch requirement; user_001 VLM cache drift |
| object_part | 90% | 75% | -15pp | user_020 VLM drift; user_005 VLM part-misid |
| claim_status | 100% | 90% | -10pp | L2.0c and L2.0d no longer flip user_020 / user_034 |
| supporting_image_ids | 95% | 95% | 0 | L6.1 fixed user_034; user_033 still off |
| valid_image | 100% | 100% | 0 | |
| severity | 100% | 85% | -15pp | chained from issue_type |

Per-row attribution:

| Row | v7 | v8 | Cause |
|---|---|---|---|
| user_005 | `scratch` | `dent` | L3.6 now requires positive scratch descriptor; the VLM blurb describes a generic mark. |
| user_020 | `contradicted` | `supported` | L2.0d no longer fires because the VLM blurb contains concrete damage words; `user_history_risk` co-trigger removed. |
| user_034 | `contradicted` | `supported` | L2.0c no longer fires for the same causal reason; `user_history_risk` co-trigger removed. |
| user_034 (citation) | `img_1` | `img_1;img_2` | L6.1 widening **fixed** this row. |
| user_001 | `dent` | `missing_part` | **VLM cache drift**, not caused by remediation. |

### 5.4 Counterfactual verification

```
Test A: randomise user_history_risk for every row (VLM output fixed)
  Rows tested:           20
  Decisional violations: 0
  supporting_image_ids violations: 0
  Legitimate risk-flag changes:   12  (only manual_review_required /
                                       user_history_risk toggled)
  Illegitimate risk-flag changes: 0

Test B: permute user_ids (VLM output fixed, history swapped)
  Rows tested:           20
  Decisional violations: 0
  supporting_image_ids violations: 0
  Legitimate risk-flag changes:   12
  Illegitimate risk-flag changes: 0

OVERALL: PASS
```

Every flagged branch now depends only on causal evidence from the VLM's
own output. The system is sample-independent for the four flagged
branches.

---

## 6. Submission freeze

The seven-phase freeze is documented in `AUDIT_REPORT.md` and
`SUBMISSION_NOTES.md`. This section summarizes.

### 6.1 The seven phases

| Phase | Action | Result |
|---|---|---|
| 1 | Regenerate final outputs on full dataset + sample | latest sample row acc 70%, output.csv 44 rows |
| 2 | Add defensive visibility guard (Layer 2.6) | no sample change (no synthetic input triggered it) |
| 3 | Provenance audit (user_*, sample_claims, ground_truth, expected_) | clean — only documentation defaults in replay_pipeline.py:233 and code/README.md:8 |
| 4 | Reproducibility verification (bit-identical) | sha256 `191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0` matches across runs |
| 5 | SUBMISSION_NOTES.md | written |
| 6 | Packaging | clean code.zip sha256 `D5E48AFE11AD857DC61D67CFC961A0421C1542C913FEBBA02802C37639839B0A`, 76 entries, no `.env`, no caches, no `__pycache__` |
| 7 | Adversarial dry-run | 10/10 synthetic scenarios pass |

### 6.2 The defensive visibility guard

Added in Phase 2:

```text
IF claim_status == supported
   AND vlm_issue_type == unknown
   AND no concrete visible issue
   AND no visibility phrase
THEN claim_status = not_enough_information
     issue_type = unknown
     severity = unknown
```

This is the single new heuristic added during the freeze. It is a
defensive guard: when the VLM reports `supported` without naming any
visible damage and without flagging a visibility problem, the verdict
is incoherent. NEI is the only safe answer.

### 6.3 The placeholder backfill

After freeze, six rows in `output.csv` were placeholder NEI entries
(`user_002/007/016/018/046/047`). We backfilled them via live VLM calls:

* `case_005` (user_007) and `case_018` (user_018) ship AVIF files with
  `.jpg` extensions. We installed `pillow-avif-plugin==1.5.5`, pinned
  it in `code/requirements.txt`, and added `import pillow_avif` to
  `code/pipeline.py` so the plugin registers on import.
* The other four cases (`case_001/046/047/051`) decoded natively.

After backfill, `output.csv` is fully populated; no placeholders remain.
Sample row accuracy and counterfactual harness are unchanged.

### 6.4 Final upload artifacts

The current `main` branch includes the three required submission artifacts:

| File | Purpose | Verification |
|---|---|---|
| `code.zip` | Runnable solution bundle with `code/`, root `evaluation/`, reports, README, and submission notes | SHA-256 `D5E48AFE11AD857DC61D67CFC961A0421C1542C913FEBBA02802C37639839B0A` |
| `output.csv` | Predictions for all 44 rows in `dataset/claims.csv` | SHA-256 `191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0` |
| `chat_transcript.md` | Upload-ready copy of the development transcript | SHA-256 `4FE21347BC3EAAB678D25CE75783653285459AC9914E02C22407682D85B651C7` |

`CHAT_LOG.md` remains the canonical in-repo transcript. `chat_transcript.md`
is a same-content copy named to match the challenge submission slot.

---

## 7. Reproducibility

### 7.1 Cache architecture

The VLM cache is disk-resident at `.cache/vlm_calls/`. The cache key is:

```text
sha256(prompt_version + "\n" + model + "\n" + prompt + "\n" + sorted_image_hashes)[:32]
```

* `prompt_version` is `"v1"` (the rubric-aligned prompt).
* `model` is `"mimo-v2.5"`.
* `prompt` is the full inspection-prompt text including the
  per-claim `image_count`, evidence requirements, and user history.
* `sorted_image_hashes` are the first-16-chars SHA-256 of each readable
  image's bytes (sorted lexicographically).

Warm-cache replay is bit-identical across runs. Cold-cache rebuild
requires API access and produces a ±5-10% sample variance due to
`mimo-v2.5`'s non-determinism.

### 7.2 Environment

```text
Python       3.11
openai       >=1.0
pillow       >=10.0
pillow-avif-plugin  >=1.4     # NEW: required for case_005 / case_018 AVIF
pandas       >=2.0
numpy        >=1.26
tqdm         >=4.0
tenacity     >=8.0
opencv-python >=4.0
python-dotenv >=1.0
pydantic     >=2.0
```

API keys are read from `.env`:

```text
NVIDIA_API_KEY=...
MIMO_API_KEY=...
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
PRIMARY_VLM_MODEL=mimo-v2.5
CACHE_DIR=.cache
```

### 7.3 Determinism proof

```bash
$ python code/replay_pipeline.py --input dataset/claims.csv --output output.csv ...
$ sha256sum output.csv
191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0

$ python code/replay_pipeline.py --input dataset/claims.csv --output output.csv ...
$ sha256sum output.csv
191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0
```

Bit-identical across runs. See `analysis/counterfactual_report.json` and
the `analysis/counterfactual_run_post_live.log` for the full audit log.

---

## 8. Repository layout

```
.
├── README.md                       ← you are here
├── problem_statement.md            ← full task spec
├── AGENTS.md                       ← rules for AI coding tools
├── CHAT_LOG.md / chat_transcript.md ← development transcript
├── SOLUTION.md                     ← solution narrative
├── AUDIT_REPORT.md                 ← Phase 4 freeze audit
├── REMEDIATION_REPORT.md           ← v7 → v8 overfitting pass
├── SUBMISSION_NOTES.md             ← freeze handoff document
├── code.zip                        ← clean submission bundle (205,771 bytes)
├── output.csv                      ← predictions for dataset/claims.csv
├── output_sample_v8.csv            ← predictions for dataset/sample_claims.csv
│
├── code/                           ← the shippable submission
│   ├── main.py                     ← live pipeline entry point
│   ├── pipeline.py                 ← orchestration (registers pillow_avif)
│   ├── rules_v2.py                 ← 6-layer rule engine + defensive guard
│   ├── rules.py                    ← legacy v1 rule engine (kept for reference)
│   ├── prompts.py / prompts_v3.py  ← prompt templates (v1 is canonical)
│   ├── models.py                   ← VLM client + cache
│   ├── schema.py                   ← enums + evidence requirements
│   ├── image_quality.py            ← OpenCV blur / low-light / glare detectors
│   ├── config.py                   ← env-driven configuration
│   ├── replay_pipeline.py          ← cache-only replay (filter_readable)
│   ├── README.md / requirements.txt
│   └── evaluation/main.py          ← evaluation harness
│
├── dataset/                        ← input data (test + sample)
│
├── analysis/                       ← experiments, ablations, audits
│   ├── ablation_*.csv / *.md
│   ├── counterfactual_harness.py   ← counterfactual robustness check
│   ├── counterfactual_report.json  ← latest counterfactual audit
│   ├── phase7_dry_run.py           ← 10-scenario adversarial dry-run
│   ├── phase7_synthetic_output.csv
│   ├── discovered_rules.json
│   ├── taxonomy_*.csv / *.md
│   └── ...
│
├── evaluation/                     ← per-version evaluation metrics + reports
│   ├── v2_metrics.json … v8_metrics_freeze.json
│   └── v2_report.json … v8_report.md
│
├── reports/                        ← narrative reports
│
├── submission/                     ← submission manifest + final audit
│
├── modal/                          ← experimental Qwen-on-Modal code (preserved)
│   ├── hybrid.py / qwen_client.py / multicrop.py / verifier.py
│   └── ...
│
└── .cache/vlm_calls/               ← VLM response cache (warm-cache replay)
```

---

## 9. Quickstart

### 9.1 Reproduce the shipped output (warm cache)

```bash
pip install -r code/requirements.txt
python code/replay_pipeline.py \
    --input  dataset/claims.csv \
    --output output.csv \
    --user-history dataset/user_history.csv \
    --cache-dir .cache/vlm_calls \
    --model mimo-v2.5 \
    --prompt-version v1
```

Expected: `output.csv` with 44 rows, sha256
`191B141940710788155ACAE188EC141CC24F0C991750CF4BF381C6ECDEBCA7C0`.

### 9.2 Cold-cache rebuild (requires API access)

```bash
python code/main.py \
    --input  dataset/claims.csv \
    --output output.csv \
    --prompt-version v2
```

Cold-cache runs may differ from warm-cache replays by ±5-10% sample
variance due to `mimo-v2.5` non-determinism.

### 9.3 Evaluate on the labelled sample

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

Expected row accuracy from the latest warm-cache verification:
**70% (14/20)**.

### 9.4 Counterfactual robustness check

```bash
python analysis/counterfactual_harness.py
```

Expected: `OVERALL: PASS` (0 decisional violations under randomized
`user_history_risk` and permuted `user_id`).

### 9.5 Synthetic adversarial dry-run

```bash
python analysis/phase7_dry_run.py
```

Expected: 10/10 scenarios pass.

---

## 10. Branches in this repository

| Branch | Status | Notes |
|---|---|---|
| `main` (post-freeze) | **canonical** | This branch. The shipped submission. |
| `submission/v1` | superseded | Baseline pipeline + manifests. |
| `feature/baseline-pipeline` | superseded | First end-to-end pipeline. |
| `feature/eval-90pct` | superseded | v7 → v8 remediations, freeze, backfill. Promoted into main. |
| `feature/hybrid-pipeline` | experimental | Qwen observer + MIMO judge + verifier. Rejected — no row-acc lift. Kept for reference. |
| `feature/qwen-modal` | experimental | Live Qwen2.5-VL on Modal. Rejected. Kept for reference. |

---

## 11. Known limitations

1. **Sample row accuracy is 70%, not the v7 85%.** This is by design —
   the v7→v8 remediation traded sample row accuracy for
   counterfactual robustness. Three rows regressed (`user_005`,
   `user_020`, `user_034`) and one row gained (`user_034` citation
   policy). See section 5.3 of this README.
2. **2 `object_part` errors are VLM-driven** (`user_005`,
   `user_008`). The rule engine cannot override the VLM's
   visible-part identification without reading the user claim text,
   which `apply_rules_v2` does not receive.
3. **VLM cold-cache non-determinism.** `mimo-v2.5` at temperature=0
   still shows ±5-10% sample variance on cold-cache runs due to
   backend non-determinism. Warm-cache replays are bit-identical.
4. **AVIF support requires `pillow-avif-plugin>=1.4`.** Without it,
   `case_005` and `case_018` silently fall through to the
   unreadable-images placeholder. The plugin is pinned in
   `code/requirements.txt` and registered on `pipeline.py` import.
5. **`output.csv` is fully populated** as of the placeholder backfill
   (commit pushed to `main`). No row returns a placeholder NEI.

---

## 12. Future implementations

The shipped v8 submission trades sample row accuracy for hidden-test
robustness. Below is a prioritised roadmap of changes that could lift
both sample row accuracy **and** hidden-test generalisation. Each item
names the expected delta, the failure mode it fixes, and the cost.

### 12.1 Vision model upgrades

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | Upgrade to `mimo-v2.5-pro` (already in `.env`, currently unused) | +5-10pp on `claim_status` and `issue_type` | The `-pro` variant has stricter JSON adherence and lower hallucination rate. Currently `PRIMARY_VLM_MODEL=mimo-v2.5`; flipping the env var to `mimo-v2.5-pro` is a one-line change. Cost: 1.3× latency, 2× cost. |
| **P1** | Add an ensemble fallback: call `mimo-v2.5` and `mimo-v2.5-pro`; trust the pro answer on disagreement | +3-5pp on `claim_status` when models disagree | The previous verifier experiment (`feature/hybrid-pipeline`) failed because the verifier was a separate prompt, not a separate model. With two independent model families, the disagreement signal is meaningful. |
| **P2** | Try GPT-4o or Claude Sonnet 4.5 as a third reviewer for low-confidence cases | +2-4pp on `object_part` and `claim_status` | Cost-prohibitive at scale but useful as a tie-breaker for rows where `apply_rules_v2` returns `not_enough_information` despite concrete visible issues. Route only the bottom-decile confidence rows. |
| **P3** | Try `Qwen2.5-VL-72B-Instruct` again with a calibrated, rubric-anchored prompt (not the bare decision-tree prompt that was rejected) | unknown | The original Qwen run used an in-prompt decision tree that fought the model. A plain rubric-anchored prompt might surface Qwen's stronger object-detection capability on `object_part`. |

### 12.2 Prompt engineering

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | Per-object custom prompts (one each for `car`, `laptop`, `package`) | +3-7pp on `issue_type` and `object_part` | The current `prompts.py` uses a single generic prompt. Domain-specific object_part lists and example damages per object would tighten VLM output. |
| **P0** | Add 2-3 few-shot examples per object type, drawn from the rubric-aligned sample | +2-4pp on `claim_status` | The `v2` prompt was rejected for being too verbose, but a *single* calibrated example per class is usually a net win. |
| **P1** | Add a JSON schema in the prompt and request `response_format={"type":"json_schema", ...}` with strict mode | +2pp on `issue_type` (fewer parse failures) | The current prompt asks for JSON but doesn't enforce schema. The replay pipeline has a `parse_json_from_text` fallback for malformed output, but every fallback is a missed opportunity. |
| **P2** | Multilingual claim translation: claims come in Hindi, Spanish, English (see `user_002`, `user_008`). Run a small translation pass before the VLM call | +1-3pp on `user_claim`-sensitive fields | Currently the VLM handles mixed-language prompts natively, but the per-field accuracy drops on non-English claims. A pre-translation step would standardise. |
| **P3** | Chain-of-thought reasoning step: ask the VLM to (1) describe the image, (2) name visible issues, (3) compare to claim, (4) decide | +1-2pp on `claim_status` | Risky: long chains hit the `max_tokens=8192` ceiling and add latency. Only attempt if other improvements plateau. |

### 12.3 Image preprocessing

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | Auto-convert AVIF/HEIC/HEIF/WEBP to JPEG at ingest, before the rule engine sees them | removes all placeholder fall-throughs | Currently `pillow-avif-plugin` decodes AVIF in-process; HEIC/HEIF still fail silently. A one-line pre-conversion step using `Pillow` (with `pillow-heif` plugin) would unify all formats. |
| **P1** | Object-detection crop: run a small DETR/YOLO model on each image to localise the damaged part before the VLM call | +3-5pp on `object_part` | The current `object_part` errors (`user_005`, `user_008`) are VLM misidentifications of which part is shown. A detector that crops to the part would help. Cost: ~50ms/image. |
| **P1** | Multi-crop with smart aggregation (revisit the failed experiment) | +2-3pp on `object_part`, -1-2pp on `claim_status` | The v7 multi-crop experiment failed because each crop's verdict was over-confident. Aggregation should only fire when all crops agree; disagreement should fall through to NEI. |
| **P2** | Image-quality triage: discard images with OpenCV-detected blur < threshold **before** the VLM call | -5-10% cost, +0-1pp accuracy | Currently blurry images still cost a VLM call. Rejecting them upstream is a free win on cost and a marginal accuracy win. |

### 12.4 Rule-engine refinements

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | Two-pass `object_part`: first pass reads `object_part` from VLM; second pass reads `user_claim` and picks the *claimed* part from a known enum, only if the VLM's `object_part=unknown` | +5-10pp on `object_part` | The current 2 VLM-driven errors (`user_005`, `user_008`) are caused by VLM picking the wrong part. Reading the claim text and matching against the enum is a rule-engine fix. Needs an `object_part_extractor` step before the VLM call. |
| **P1** | Add `_RESIDUE_HINTS` for water_damage vs stain discrimination | +1-2pp on `issue_type` for package claims | The current water_damage → stain gate (Layer 3) is a phrase list. A small ML classifier trained on the labelled sample could replace the phrase list with a calibrated probability. |
| **P2** | Per-object severity calibration | +2-4pp on `severity` | The current severity map is hand-tuned per `issue_type`. A per-object-per-issue regression model trained on the labelled sample would generalise better. |
| **P3** | Confidence score on every decision | enables downstream routing | None of the current 10 output fields carry a confidence value. Adding `confidence_*` fields (one per output) would let the verifier / ensemble / fallback layers reason about uncertainty. |

### 12.5 Data and evaluation

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | Expand the labelled sample from 20 to ~200 rows by hand-labelling the test set | enables statistically meaningful per-field accuracy | The 20-row sample has wide confidence intervals. A 200-row sample would let us distinguish 65% ± 5% from 70% ± 3%. |
| **P1** | Add per-confidence-band accuracy and confusion-matrix plots | diagnostic, not a direct gain | The current `evaluation/main.py` reports only flat accuracy. Confusion matrices would reveal which (issue_type, object_part) pairs are systematically confused. |
| **P2** | Synthetic data generation for under-represented classes | +2-3pp on rare `issue_type` × `object_part` combinations | The sample has 1 example each of `water_damage / laptop`, `missing_part / package`, `torn_packaging / package`. A diffusion model could generate more examples for rare combinations. |
| **P3** | Active-learning loop on low-confidence rows | +5pp on the long tail | Once confidence scores are added, route low-confidence rows to a human reviewer and retrain the rule engine on the corrected labels. |

### 12.6 Engineering improvements

| Priority | Change | Expected Δ | Notes |
|---|---|---|---|
| **P0** | CI gate: run `analysis/counterfactual_harness.py` on every PR | prevents future regressions | The counterfactual harness is the single best guard against overfitting. Wire it into `.github/workflows/ci.yml`. |
| **P1** | Schema validator as a pre-commit hook | prevents malformed outputs | A `jsonschema` validator against `OUTPUT_COLUMNS` would catch enum mismatches before they hit `output.csv`. |
| **P1** | Parallel VLM calls (asyncio + `AsyncOpenAI`) | -50% wall-clock time on the full 44-row run | Each row currently serialised. The `openai>=1.0` SDK supports async natively. |
| **P2** | Streaming output: write `output.csv` row-by-row as VLM responses land | -90% tail latency on partial-failure recovery | Currently a single failed row blocks the entire batch. |
| **P3** | Container image (`Dockerfile`) with pinned Python 3.11 and OS-level AVIF/HEIF libraries | removes the `pillow-avif-plugin` install step | `pip install` in a slim image works but is fragile. A container with `libheif` baked in is more portable. |

### 12.7 What's already been tried and rejected

Do **not** re-attempt these without a fundamentally different approach:

* **v3 prompt with explicit decision tree** — section 4.1 of this README.
  Strictly worse than v1.
* **Qwen2.5-VL-72B-Instruct on Modal as primary VLM** — section 4.2.
  ≤55% row accuracy across all configurations.
* **Qwen observer + MIMO judge verifier pattern** — section 4.2. Verifier
  introduced ±3pp variance and never lifted row accuracy.
* **`dent → scratch` on "absence of deformation language"** — section 4.4.
  Absence is not causation. Reverted in v8.
* **`contradicted` citation gated on `text_instruction_present +
  user_history_risk`** — section 4.5. Widened in v8 to all contradicted
  rows.

### 12.8 Recommended execution order

If we had another 24 hours:

1. Flip `PRIMARY_VLM_MODEL` to `mimo-v2.5-pro` (one-line change, biggest
   single-engine lift).
2. Add per-object prompts with 2 few-shot examples each.
3. Add HEIC support via `pillow-heif` so all 44 rows decode natively.
4. Run a second pass on `object_part` against the parsed `user_claim`
   text.
5. Wire `analysis/counterfactual_harness.py` into CI as a required
   check.

Estimated combined delta: **+10-15pp on sample row accuracy**, with
hidden-test robustness preserved (counterfactual harness still passes).

---

## 13. License & contact

This is a HackerRank Orchestrate (June 2026) hackathon submission. The
canonical branch is `main`. The active rules engine is
`code/rules_v2.py`. The shipped bundle is `code.zip`
(sha256 `79CC9E5058E78F786AE7AB970802123405748BA25EF3C66392C4B81D0DF057C2`,
55,949 bytes).

For reproduction questions, see `SUBMISSION_NOTES.md` and the
`analysis/` audit artifacts. For architectural rationale, see
`AUDIT_REPORT.md` and `REMEDIATION_REPORT.md`.
