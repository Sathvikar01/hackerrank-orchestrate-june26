# Architecture decision record

## Options considered

| ID | Description | Pros | Cons |
|---|---|---|---|
| A | Single VLM call per claim | Simple, cheap, low latency | All decisions depend on one prompt |
| B | VLM + rule engine (chosen) | Deterministic enum safety, explainable | Two stages to debug |
| C | Multi-agent ensemble | Better robustness, cross-checking | 2–3× cost, more variance |
| D | Hierarchical reasoning (vision → text) | Clear separation | Two-stage errors compound |
| E | Best-of-N voting with verifier | Highest accuracy ceiling | 3–5× cost, overkill at N=44 |

## Winner: Architecture B (VLM + rule engine) with cascade verification disabled at baseline

### Why B

- **Test set is tiny** (44 rows, ~80 images). A single strong multimodal
  call is enough capacity; an ensemble is over-engineering.
- **Strict enums** are easier to enforce in code than to elicit reliably
  from a model. The rule engine normalizes misspellings, picks the
  closest allowed value, and forces consistency across fields.
- **Reproducibility** is highest when one model + deterministic rules
  produce the row. Cache + temp=0 make re-runs bit-identical.

### Components

1. **Primary VLM (`mimo-v2.5`).** All images + transcript + history
   + requirements in a single chat completion; structured JSON output.
   Temperature 0. Max tokens 8192 (the model spends ~2k on reasoning
   before the JSON; 2k truncated the JSON at 2048).
2. **OpenCV image-quality module** (`code/image_quality.py`).
   Deterministic blur / low-light / glare / border detectors. Outputs
   `blurry_image`, `low_light_or_glare`, `cropped_or_obstructed`.
3. **Rule engine** (`code/rules.py`).
   - Allowed-value normalization with fuzzy matching for `object_part`.
   - Pick the most-severe visible issue as the primary `issue_type` /
     `object_part` / `severity`.
   - Compose `risk_flags` from VLM + OpenCV + user history.
   - Enforce cross-field consistency (see leaderboard_strategy.md).
4. **Disk cache** (`code/models.py`).
   Keyed on (prompt version, model, prompt text, sorted image hashes).
   Enables free iteration during prompt tuning.
5. **Retries** via tenacity: exponential backoff, 3 attempts.

### What was NOT built (and why)

- **Ensemble / verifier cascade**: cost ~3×, expected gain small at
  this N. Reserved as a fallback.
- **Self-hosted VLM on Modal A10G**: NVIDIA NIM inference was forbidden
  on the provided key, and the test set is small enough that API-only
  is the simplest and cheapest path.
- **External knowledge base (e.g. car-model lookup)**: the schema does
  not ask for vehicle make/model; adding it would not move accuracy.

## Iteration history

| Version | Change | Row acc (sample) |
|---|---|---|
| v1 | First prompt, max_tokens=2048 (JSON truncated) | 10% |
| v2 | max_tokens=8192, severity rubric, issue guidance, injection rules | 20% |
| v2 + OpenCV (aggressive thresholds) | false positives in risk_flags | 15% |
| v2 + OpenCV (tuned thresholds) | best balance | 25% |
| v3 (few-shot example) | biased model toward example values | 20% |

## Final stack

- Model: `mimo-v2.5` via MIMO API (`https://api.xiaomimimo.com/v1`)
- Prompt: v2 (explicit rubric, injection defense, reference shapes)
- Image quality: OpenCV (blur / brightness / glare / border)
- Rule engine: deterministic enum + consistency enforcement
- Cache: SHA-256-keyed disk cache in `.cache/vlm_calls/`
- Retries: tenacity, exponential backoff, max 3 attempts
