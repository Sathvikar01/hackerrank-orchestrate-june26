# Submission manifest

## Architecture

Single multimodal model (`mimo-v2.5`) with a deterministic post-processing
rule engine. All readable images for a claim are sent in one chat
completion; the rule engine normalizes enums, merges OpenCV-detected
image-quality flags, and enforces cross-field consistency
(e.g. `wrong_object` → `claim_status=contradicted`).

## Models

| Role | Model | Provider |
|---|---|---|
| Primary VLM | `mimo-v2.5` | MIMO (`https://api.xiaomimimo.com/v1`) |

NVIDIA NIM inference was unavailable on the provided API key (HTTP 403
on every tested vision model). MIMO v2.5 was the only multimodal model
that accepted image inputs and returned well-structured JSON.

## Experiments

| ID | Description | Sample row accuracy |
|---|---|---|
| v1 | First prompt, `max_tokens=2048` (JSON truncated mid-stream) | 10% |
| v2 | `max_tokens=8192`, severity rubric, issue-type guidance, injection defense | 20% |
| v3 | + OpenCV image quality (aggressive thresholds) | 15% |
| v4 | + tuned OpenCV thresholds (avoid false positives) | 25% |
| v5 | + few-shot example in prompt | 20% |

The final submission uses v2 prompt + tuned OpenCV module, matching the
v4 / v6 configuration.

## Evaluation metrics (sample_claims.csv, 20 rows)

| Field | Accuracy |
|---|---|
| evidence_standard_met | 85% |
| risk_flags | 55% |
| issue_type | 40% |
| object_part | 90% |
| claim_status | 85% |
| supporting_image_ids | 70% |
| valid_image | 90% |
| severity | 45% |
| **Row accuracy (all enum fields match)** | **20%** |

Variance ±5–10% across runs due to model non-determinism.

## Runtime

- Test set (44 rows, 82 images): ~789 s wall-clock (~13 min), sequential
- Average per claim: ~18 s (MIMO v2.5 reasoning model)
- Cache hits on re-runs: 100% (instant)

## Cost

- ~193,500 total tokens for the 44-row test set
- ~82 images processed
- Cost is a small fraction of a dollar (MIMO pricing not published; the
  volume is negligible regardless of per-token rate)
- Reproducible disk cache makes prompt iteration free

## Reproducibility

- `temperature=0`, `max_tokens=8192`
- Disk cache keyed on `(prompt_version, model, prompt, image_hashes)`
- Deterministic rule engine (no randomness)
- `.env` for secrets (gitignored)
- Fixed prompt version (`v2`)

## Files in this submission

```
code/                       runnable solution
  main.py                   CLI entry point
  pipeline.py               load data, call VLM, write output
  models.py                 VLM client, caching, retries
  prompts.py                prompt templates
  rules.py                  deterministic post-processing
  schema.py                 allowed values and enums
  config.py                 environment-driven configuration
  image_quality.py          OpenCV blur/brightness/glare/border
  evaluation/main.py        evaluation metrics
  README.md                 setup + architecture
  requirements.txt
output.csv                  predictions for dataset/claims.csv
output_sample_v6.csv        predictions for dataset/sample_claims.csv
evaluation/
  evaluation_report.md      metrics + operational analysis
  *.json                    raw evaluation + runtime reports
reports/
  repo_analysis.md
  leaderboard_strategy.md
architecture/
  decision_record.md
submission/
  final_audit.md
  submission_manifest.md
```
