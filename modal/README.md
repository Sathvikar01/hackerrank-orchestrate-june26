# `modal/` - Qwen2.5-VL deployment

This folder ships the Qwen2.5-VL vision-language model as a serverless GPU
function on Modal, along with all the harness scripts to run the A/B test,
multi-crop experiment, text-only verifier, and optimization loop.

## Files

| File | Purpose |
|---|---|
| `__init__.py` | Marks the folder as a regular Python package so local imports win against the installed Modal Labs SDK. |
| `qwen_service.py` | Modal app: Qwen2.5-VL inference class + HTTPS web endpoint. |
| `qwen_schema.py` | JSON-schema validator + fuzzy coercion for the structured output. |
| `qwen_client.py` | HTTP client for the deployed endpoint, with caching + retry. |
| `ab_test.py` | A/B test runner (current pipeline vs Qwen). |
| `multicrop.py` | Multi-crop experiment (full + center + edge-density). |
| `verifier.py` | Text-only MIMO verifier (never inspects images). |
| `verifier_report.py` | Markdown renderer for verifier JSON. |
| `optimize.py` | 10-iteration optimization loop harness. |
| `optimize_report.py` | Markdown renderer for optimization history. |

## Quickstart

```bash
# 1. Install the Modal CLI (already present on this machine).
modal --version

# 2. Authenticate.
modal token set

# 3. Deploy the service.
modal deploy modal/qwen_service.py

# 4. Copy the endpoint URL Modal prints and export it.
export QWEN_ENDPOINT_URL=https://<workspace>--qwen-vl-predict-damage-http.modal.run

# 5. Run the A/B test.
python modal/ab_test.py --sample-csv dataset/sample_claims.csv --out-dir evaluation

# 6. Run the multi-crop experiment.
python modal/multicrop.py --sample-csv dataset/sample_claims.csv --out-dir evaluation

# 7. Run the verifier (text-only).
python modal/verifier.py --qwen-csv evaluation/multicrop_B_output.csv

# 8. Run the optimization loop on Qwen (10 iterations max).
python modal/optimize.py --pipeline B --max-iterations 10
```

## Infrastructure model

* GPU: **A10G** (24 GB GDDR6). Scales to zero after 60 s of idle.
* Default model: **Qwen2.5-VL-3B-Instruct**. Override with
  `export QWEN_MODEL_ID=Qwen/Qwen2.5-VL-7B-Instruct` before deploy if you
  want to A/B test the 7B (tighter memory; can OOM with multi-image inputs).
* Container image: pinned `torch==2.4.1`, `transformers==4.49.0`,
  `qwen-vl-utils==0.0.8`. `attn_implementation="sdpa"` (safe; flash-attn
  optional).
* Model cache: persisted in a Modal Volume so re-deploys do not re-download.

## Cost projection

| Metric | Value |
|---|---|
| A10G cost | $0.0006125/GPU-second (Modal, mid-2026) |
| Cold start (load 3B + vision tower + CUDA init) | ~25-45 s, ~$0.015-$0.028 |
| Warm latency, single image | ~3-5 s, ~$0.002-$0.003 |
| Warm latency, multi-image (3 images) | ~6-10 s, ~$0.004-$0.006 |
| Cost per claim (warm, single image) | ~$0.002 |
| Cost per 1,000 claims | ~$2.00 (warm) or ~$2.02 with one cold start |
| Cost per sample (20 rows) | <$0.05 |

## Stop condition gate

The brief requires issue_type or severity to improve by >= 15 percentage
points vs the current pipeline before Qwen replaces it. With the current
deployment workflow (Modal not deployed in this environment) we cannot
measure B's real numbers; the A/B harness reports "B = deferred" until
you set `QWEN_ENDPOINT_URL`. Run the harness again after deployment to
populate real B numbers.