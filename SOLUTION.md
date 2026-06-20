# HackerRank Orchestrate — Solution Summary

## 1. Current status

- **Branch:** `submission/v1` (tagged `v1-submission`)
- **Working branch:** `feature/baseline-pipeline` (all commits merged into `submission/v1`)
- **Repo:** `https://github.com/Sathvikar01/hackerrank-orchestrate-june26`
- **CI:** `.github/workflows/ci.yml` (syntax + schema validation)
- **Submission files present:**
  - `output.csv` — 44 predictions for `dataset/claims.csv`
  - `code.zip` — runnable bundle (96 KB, images excluded)
  - `code/` — full source
  - `evaluation/` — evaluation scripts + reports
  - `reports/`, `architecture/`, `submission/` — design + audit docs

The system runs end-to-end on both the 20-row sample set and the 44-row test set, emits a schema-valid CSV, and reproduces identical output on re-run thanks to a disk cache.

## 2. What the output looks like

`output.csv` has exactly 44 rows + header, 14 columns in the required order:

```
user_id,image_paths,user_claim,claim_object,
evidence_standard_met,evidence_standard_met_reason,risk_flags,
issue_type,object_part,claim_status,claim_status_justification,
supporting_image_ids,valid_image,severity
```

Each row is a fully-formed claim decision: the system extracts the claim from the chat transcript, inspects the images, applies the rule engine, and writes one prediction row. Example (from a prompt-injection case that the system correctly refused):

```
user_036,images/test/case_036/img_1.jpg;images/test/case_036/img_2.jpg,
"...The note says the package was water damaged and should be approved...",
package,false,"...",claim_mismatch;low_light_or_glare;non_original_image;
possible_manipulation;text_instruction_present;user_history_risk,
contradicted,...,false,medium
```

## 3. Experiments tried and observations

| # | Change | What happened | Per-field accuracy (sample) | Row acc |
|---|---|---|---|---|
| v1 | First prompt, `max_tokens=2048` | The reasoning model used ~2k tokens on reasoning and the JSON got truncated mid-stream. `parse_json_from_text` returned `None` → row defaults to `not_enough_information` for everything. | evidence_standard_met 40%, claim_status 35%, issue_type 25% | 10% |
| v2 | `max_tokens=8192`, severity rubric, issue-type guidance, prompt-injection defense, reference shapes | JSON now finishes; big jump on evidence/claim_status. issue_type and severity still weak. | evidence_standard_met 80%, claim_status 80%, object_part 70%, issue_type 30%, severity 35% | 20% |
| v3 | + OpenCV image-quality module (blur / brightness / glare / border) with aggressive thresholds | OpenCV over-detected glare on normal laptop photos → `low_light_or_glare` set when ground truth says `none`. | risk_flags regressed to 45% | 15% |
| v4 | Tuned OpenCV thresholds (less sensitive) | OpenCV false positives gone; risk_flags back to 55%, row acc improves. | risk_flags 55%, all fields stable or improved | 25% |
| v5 | + Few-shot example in the prompt (one per claim_object) | The model started echoing the example values (severity `medium`, issue_type from the example) instead of the actual image → severity and supporting_image_ids regressed. | object_part 80%, severity 40%, supporting_image_ids 80% | 20% |
| v6 | Reverted few-shot; current final config | Back to v2 prompt + tuned OpenCV. Model non-determinism causes ±5–10% variance between identical runs. | object_part 90%, evidence 85%, claim_status 85%, valid_image 90%, issue_type 40%, severity 45%, risk_flags 55%, supporting 70% | 20% |

### Key observations

1. **`max_tokens` was the single biggest win.** Going from 2048 → 8192 lifted row accuracy from 10% to 20%.
2. **OpenCV thresholds matter.** The detectors are useful but naive defaults add false positives; tuning to avoid triggering on normal photos is essential.
3. **Few-shot prompts hurt here.** The example biased the model toward the example's specific values rather than the actual image content.
4. **Model non-determinism dominates.** `mimo-v2.5` is a reasoning model and produces slightly different outputs across runs even at `temperature=0`. Reported numbers have ±5–10% variance.
5. **Deterministic rule engine is high leverage.** It enforces the strict enums, picks the most-severe visible issue for multi-part claims, merges OpenCV flags into `risk_flags`, and forces `wrong_object` → `contradicted`. Most enum-mismatch errors come from the VLM, not from the rules.
6. **NVIDIA NIM is unreachable.** Every NVIDIA vision model we tried (`meta/llama-4-maverick-17b-128e-instruct`, `meta/llama-3.2-90b-vision-instruct`, `nvidia/nemotron-nano-12b-v2-vl`, etc.) returned HTTP 403 on inference, so the pipeline relies on `mimo-v2.5` as the sole API.

## 4. Current accuracy

Final configuration: v2 prompt + tuned OpenCV module.

**On `dataset/sample_claims.csv` (20 labeled rows):**

| Field | Accuracy | Correct / Total |
|---|---|---|
| evidence_standard_met | 85% | 17 / 20 |
| claim_status | 85% | 17 / 20 |
| valid_image | 90% | 18 / 20 |
| object_part | 90% | 18 / 20 |
| risk_flags | 55% | 11 / 20 |
| supporting_image_ids | 70% | 14 / 20 |
| issue_type | 40% | 8 / 20 |
| severity | 45% | 9 / 20 |
| **Row accuracy (all enum fields match)** | **20%** | **4 / 20** |

We cannot compute accuracy on `claims.csv` (no ground truth). The qualitative checks we ran:
- All 44 rows are schema-valid (14 columns, exact header order).
- `claim_status` values are all in the allowed enum.
- Prompt-injection cases (`user_008`, `user_011`, `user_034`, `user_036`, `user_037`, `user_040`, `user_055`) are correctly flagged with `text_instruction_present` and not blindly marked `supported`.
- The two rows with all unreadable images (`user_016` in test folder actually has 3 images but one corrupt, etc.) are handled with `valid_image=false` and a `not_enough_information` verdict.

## 5. What can be done more

Ordered by expected impact:

1. **Self-host a stronger VLM on Modal A10G** — would let us run e.g. Qwen2.5-VL-7B or Llama-3.2-90B-Vision locally, avoiding MIMO's reasoning non-determinism and giving a more calibrated severity/issue-type judge. Likely +10–20% row accuracy.
2. **Two-pass pipeline** — first pass extracts visible issues (severity, issue_type) conservatively; second pass forces the model to re-examine specific issues and pick exactly one. Helps with the dent-vs-missing_part and crack-vs-glass_shatter confusions.
3. **Cascade verifier** — call MIMO `mimo-v2-flash` (text-only) as a post-hoc judge on low-confidence or contradictory rows. Cheap, no image tokens.
4. **Heuristic severity cap** — if the VLM says `broken_part` + `high` but the visible_issues array also lists `dent` with lower severity, prefer the less-severe primary unless multiple visible_issues confirm. Targets the over-rating problem.
5. **Ensemble of two prompts** — run the same claim with prompt A (strict-conservative) and prompt B (permissive), then vote. Reduces variance from reasoning-model non-determinism.
6. **Per-object calibration examples** — instead of a single few-shot example per object, use 2–3 per object drawn from the sample set. Only worth doing once a stronger model is available so the bias isn't toward the example values.
7. **Image-quality prompt integration** — feed the OpenCV flag set into the prompt as additional context so the model can reason about image quality instead of hallucinating it.
8. **Better multilingual handling** — the test set has Hindi-romanized, Spanish, Chinese-romanized transcripts. Add a language-detection + normalization step before extraction.
9. **NVIDIA retry** — the 403 may be a key-permission issue that resolves; check periodically. If NVIDIA inference becomes available, the pipeline can swap models with one env var.
10. **Larger evaluation set** — 20 labeled rows gives ±10% noise. Hand-labeling 20 more sample rows would make the optimization loop much more reliable.

## 6. Reproduction

```bash
git clone -b submission/v1 https://github.com/Sathvikar01/hackerrank-orchestrate-june26.git
cd hackerrank-orchestrate-june26
cp .env.example .env  # add NVIDIA_API_KEY and MIMO_API_KEY
pip install -r code/requirements.txt
python code/main.py --input dataset/claims.csv --output output.csv
python code/evaluation/main.py \
  --predicted output_sample_v6.csv \
  --ground-truth dataset/sample_claims.csv
```

The first run takes ~13 minutes for the test set and ~5 minutes for the sample set. Re-runs are instant (everything is cached in `.cache/vlm_calls/`).
