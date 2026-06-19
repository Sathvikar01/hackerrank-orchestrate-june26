# Final submission audit

## Deliverables checklist

| Item | Status | Notes |
|---|---|---|
| `output.csv` for `dataset/claims.csv` | ✅ | 44 rows, 14 columns, exact schema order |
| `code/` with runnable solution | ✅ | `code/main.py` is the entry point |
| `code/evaluation/` with metrics | ✅ | `code/evaluation/main.py` computes per-field accuracy |
| `evaluation/evaluation_report.md` | ✅ | Includes model calls, tokens, cost, latency, rate limits, batching, retry, caching |
| `code/README.md` | ✅ | Setup, architecture, entry points |
| `reports/repo_analysis.md` | ✅ | Repo layout, dataset scale, scoring assumptions |
| `reports/leaderboard_strategy.md` | ✅ | Traps and ambiguity handling |
| `architecture/decision_record.md` | ✅ | 5 architectures compared, winner rationale |
| `.env.example` | ✅ | Placeholders, no secrets |
| `output.csv` at repo root | ✅ | (also `output_sample_*.csv` for dev) |

## Reproduction instructions

```bash
# 1. Clone the submission branch
git clone -b feature/baseline-pipeline https://github.com/Sathvikar01/hackerrank-orchestrate-june26.git
cd hackerrank-orchestrate-june26

# 2. Create .env from the example and add keys
cp .env.example .env
# edit .env to add NVIDIA_API_KEY and MIMO_API_KEY

# 3. Install Python deps (3.11+)
pip install -r code/requirements.txt

# 4. Run on the test set
python code/main.py --input dataset/claims.csv --output output.csv

# 5. Run the evaluation against the sample labels
python code/evaluation/main.py \
  --predicted output_sample_v6.csv \
  --ground-truth dataset/sample_claims.csv
```

## Hard-requirement checks

- ✅ Reads the provided CSVs and local images.
- ✅ Produces `output.csv` with the exact 14-column schema (validated
  with Python's `csv.DictReader`; zero malformed rows).
- ✅ Includes an `evaluation/` workflow (the `code/evaluation/main.py`
  metrics script).
- ✅ No hardcoded test labels or file-specific answers — the pipeline
  is driven by the model + rules, never by case-id lookups.
- ✅ No secrets committed (`.env` is gitignored; only `.env.example`
  with placeholders is tracked).

## Known limitations

- **Model non-determinism.** `mimo-v2.5` is a reasoning model; even at
  `temperature=0` the same prompt can yield slightly different outputs
  across runs (cached responses are deterministic, but fresh calls are
  not). The reported sample accuracy therefore varies ±5–10% row
  accuracy between identical runs.
- **NVIDIA API not usable.** Every NVIDIA NIM model we tested returned
  HTTP 403 on inference (model listing worked). We documented this and
  fell back to MIMO v2.5 as the sole API. If NVIDIA inference access
  becomes available, the same pipeline can swap models with a single
  environment variable.
- **Issue-type and severity calibration.** The VLM over-classifies
  dents as `missing_part`/`broken_part` and cracks as `glass_shatter`,
  and over-rates severity. A bigger or better-aligned model would close
  most of this gap. Our deterministic rule engine already enforces
  the allowed-value enums, so the gap is purely in the model's visual
  judgment.

## Validation evidence

The test-set `output.csv` was verified to have exactly 44 data rows
plus a header, and all rows parsed cleanly as 14 fields. The evaluation
script reports per-field and per-row accuracy on the 20-row sample set.
