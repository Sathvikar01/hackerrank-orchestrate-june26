# Leaderboard strategy

## What likely wins

The top scores will come from systems that:

1. **Nail the enum columns.** Most rows fail because the model returns
   `glass_shatter` instead of `crack`, or `high` instead of `medium`.
   A deterministic rule engine that enforces allowed values and
   cross-field consistency (e.g. `wrong_object` → `contradicted`) is a
   cheap multiplier.
2. **Defend against prompt injection.** The test set contains explicit
   attempts ("approve immediately", "ignore all previous instructions").
   Teams that follow these instructions will get those rows wrong and
   lose points.
3. **Handle unreadable images gracefully.** 8 test images are corrupt.
   Teams that crash will lose those rows entirely.
4. **Reproduce exactly.** A fixed `temperature=0` + cached responses
   + a deterministic rule layer mean the same code gives the same
   output every run — which the judge interview rewards.

## Easiest predictions

- Claims with one clear image and a simple object/damage match
  (`dent` on a `rear_bumper`, `crack` on a `screen`,
  `crushed_packaging` on a `package_corner`).
- Claims with valid user history (`history_flags=none`) and no
  injection attempts.

## Hardest predictions

- Distinguishing `dent` from `missing_part` / `broken_part`.
- Distinguishing `crack` from `glass_shatter`.
- Calibrating severity (most models over-rate damage).
- Multi-part claims where the VLM picks the wrong primary.
- Cases where one image is unreadable and the rest are valid (the
  pipeline must pick the right one).

## Likely leaderboard traps

1. **Prompt injection:** case_008, case_036, case_040, case_055
   instruct the model to override the verdict. Follow them and you lose.
2. **Wrong object:** case_008, case_019, case_033 show a different
   object than claimed. The `wrong_object` flag must trigger
   `contradicted`.
3. **Multi-part:** case_001 (bumper + headlight), case_010 (door + bumper),
   case_019 (hinge + screen), case_040 (torn + missing). Schema forces a
   single issue; pick the most severe visible one.
4. **Multilingual transcripts:** Hindi-romanized, Spanish, Chinese-romanized
   appear in the test set. The model must generalize across languages.
5. **Unreadable images:** case_001, 005, 018, 046, 047, 051 have corrupt
   files. Hard-crashing on these loses the row.
6. **Severity exaggeration:** `contradicted` + `claim_mismatch` when
   the user claims severe damage but the image shows a scratch.

## Ambiguity handling

- If the VLM says `wrong_object=true`, override `claim_status` to
  `contradicted` (regardless of what it said).
- If `valid_image=false` (e.g. screenshot, manipulation), override
  `claim_status` to `contradicted`.
- If `evidence_standard_met=false`, override `claim_status` to
  `not_enough_information`.
- If `claim_mismatch=true` from the VLM, override to `contradicted`.
- Multi-part claims: pick the most severe visible issue.

## Plan to win

1. Get a correct, schema-valid `output.csv` for all 44 rows (baseline).
2. Run the system on the 20 sample rows and compute per-field accuracy.
3. Iterate the prompt + rule engine until per-field accuracy stops
   improving.
4. Re-generate `output.csv`.
5. Package `code.zip`, write the evaluation report, tag the submission.
