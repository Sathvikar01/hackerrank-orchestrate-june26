# Evaluation Report — HackerRank Orchestrate June 2026

## Approach summary

The system uses a single multimodal model (`mimo-v2.5`) via the MIMO API
(OpenAI-compatible, `https://api.xiaomimimo.com/v1`). For every claim it:

1. Resolves the submitted image paths and filters out unreadable files.
2. Runs deterministic OpenCV detectors (blur / low-light / glare / crop).
3. Sends all readable images together with the claim transcript, user
   history, and evidence requirements to the VLM in a single call and asks
   for structured JSON.
4. Applies a deterministic rule engine to enforce allowed values, merge
   OpenCV flags, propagate user-history flags, and ensure cross-field
   consistency (e.g. `wrong_object` forces `claim_status=contradicted`).
5. Writes one row to `output.csv` with the exact 14-column schema.

A disk cache keyed on (image hash + prompt + model) gives reproducible
re-runs and free iteration during prompt tuning.

## Final strategy used for `output.csv`

- Model: `mimo-v2.5`
- Prompt version: `v2` (explicit severity rubric + issue-type guidance +
  prompt-injection defense + reference shapes)
- Image quality: OpenCV blur / brightness / glare / border heuristics
- Rule engine: enum normalization, most-severe visible-issue selection,
  user-history risk flag propagation (never overrides visual evidence)

## Metrics on `dataset/sample_claims.csv`

The latest run (`output_sample_v6.csv`) with v2 prompt + OpenCV module:

| Field | Accuracy |
|---|---|
| evidence_standard_met | 85% (17/20) |
| risk_flags | 55% (11/20) |
| issue_type | 40% (8/20) |
| object_part | 90% (18/20) |
| claim_status | 85% (17/20) |
| supporting_image_ids | 70% (14/20) |
| valid_image | 90% (18/20) |
| severity | 45% (9/20) |
| **Row accuracy (all enum fields match)** | **20% (4/20)** |

Earlier snapshots (same prompt, model variance):
- v2 (no OpenCV, small parse bug): row 10%, per-field 20–80%
- v3 (+ OpenCV, aggressive thresholds): row 15%
- v4 (tuned OpenCV thresholds): row 25%
- v6 (current, reverted few-shot): row 20%

The model is non-deterministic even at temperature 0 due to internal
reasoning; the same prompt can yield ±5–10% on the 20-row sample.

### What improved accuracy

- Fixing the `max_tokens=2048` truncation (moved to `max_tokens=8192`)
  was the single biggest win — it lifted row accuracy from 10% to ~20%.
- Adding the OpenCV image-quality module and tuning thresholds to
  avoid false positives on normal photos lifted row accuracy further.
- Tightening the severity rubric and issue-type guidance in the v2
  prompt moved object_part from 75% to 90%.

### Where errors remain

- `issue_type` (~40%) — the VLM still over-classifies dents as
  `missing_part`/`broken_part` and cracks as `glass_shatter`.
- `severity` (~45%) — the VLM rates damage higher than the ground truth.
- `risk_flags` (~55%) — exact set match is fragile; adding or omitting
  one flag flips correctness.

## Operational analysis (test set, 44 rows)

### Volume

- Rows processed: 44
- Images processed: 82 (after filtering 8 unreadable files)
- Total VLM calls: 42 (two rows had no readable images and skipped the API)

### Tokens

- Total tokens: ~193,562 across the test set
- Average per claim: ~4,400 tokens
- Average per image: ~2,400 tokens

### Latency

- Wall-clock for the 44-row test set: ~789 s (~13 min) sequentially
- Average per claim: ~18 s (MIMO v2.5 reasoning model)
- Distribution: 12–22 s per call, occasional 30 s+ outliers

### Cost

The MIMO pricing was not published; based on the volume (~200k tokens,
~82 images) the cost is a small fraction of a dollar either way.
Reproducible caching means re-runs after the first are free.

### Rate limits and throttling

- No rate-limit errors observed during the test run.
- Calls are issued sequentially (one every ~18 s). With the API
  allowing comfortable burst capacity at this QPS, no explicit
  back-off was required; the tenacity retry decorator handles 429/5xx.
- If rate limits appear, switching to async concurrent calls with a
  semaphore of 4–8 is the next step.

### Batching strategy

- All readable images for a claim are sent in one multimodal message,
  rather than per-image calls. This gives the VLM cross-image context
  (useful for "one image is blurry, another is clear" cases).
- No batching across claims is performed; the per-call cost is small
  enough that sequential processing is simpler and easier to reproduce.

### Retry strategy

- `tenacity` decorator on the API call with exponential backoff
  (`wait_exponential(multiplier=1, min=2, max=30)`), up to 3 attempts.
- Retries on `openai.APIError`, `RateLimitError`, and `requests.RequestException`.

### Caching strategy

- Disk cache at `.cache/vlm_calls/` keyed on SHA-256 of
  `(prompt_version, model, prompt_text, sorted(image hashes))`.
- Cached responses store the raw content, model id, usage, latency,
  and an explicit `cache_hit` flag.
- Same-image, same-prompt, same-model re-runs are instant (sub-second
  per row) — used heavily during prompt iteration.
- Text-only calls (verifier/judge, when enabled) cache under
  `.cache/vlm_calls/text_<hash>.json`.

## Notes for the judge interview

- **Why this architecture:** the test set is small (44 rows, ~80 images)
  so a single strong multimodal model with deterministic post-processing
  is more reliable than an ensemble of weak models.
- **Why MIMO:** the only available vision-capable model on the provided
  API keys. NVIDIA NIM inference returned 403 for every vision model
  tested; MIMO v2.5 worked and produces well-structured JSON.
- **Why a rule engine:** the hardest parts of the schema are the strict
  enums and cross-field consistency (e.g. `wrong_object` ↔ `contradicted`,
  `not_enough_information` ↔ `supporting_image_ids=none`). Encoding these
  in deterministic code is more reliable than hoping the VLM obeys them.
- **Prompt-injection defense:** the test set contains explicit attempts
  ("approve immediately", "ignore all previous instructions"). The prompt
  instructs the model to ignore in-transcript / in-image instructions
  and set `text_instruction_present=true`. The model does detect these
  in the sample + test runs.
- **Reproducibility:** temperature=0, deterministic cache, fixed prompt
  version, seeded environments. The same commit on the same data yields
  the same `output.csv`.
