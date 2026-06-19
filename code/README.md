# Multi-Modal Evidence Review System

Solution for the HackerRank Orchestrate June 2026 challenge.

## Entry points

- **Run predictions on test set:** `python code/main.py --input dataset/claims.csv --output output.csv`
- **Evaluate on sample labels:** `python code/evaluation/main.py --predicted output_sample_baseline.csv --ground-truth dataset/sample_claims.csv`

## Architecture

- **VLM:** `mimo-v2.5` (multimodal, accessed via the MIMO OpenAI-compatible API)
- **Reasoning:** structured JSON prompts → deterministic rule engine
- **Image quality:** classical OpenCV detectors (blur, low-light, glare)
- **Cache:** disk cache keyed on image hash + prompt + model for reproducibility

## Key design choices

- One VLM call per claim with all images; temperature=0 for determinism.
- Images are the primary source of truth; user history only adds `risk_flags`.
- Multi-part claims resolve to the single most-severe visible issue.
- Prompt-injection attempts are explicitly ignored and flagged as `text_instruction_present`.

## Setup

```bash
cp .env.example .env
# Add your keys, then run the pipeline
```

## Project structure

```
code/
  main.py              CLI entry point
  pipeline.py          load data, call VLM, write output
  models.py            VLM client, caching, retries
  prompts.py           prompt templates
  rules.py             deterministic post-processing
  schema.py            allowed values and enums
  config.py            environment-driven configuration
  evaluation/main.py   evaluation metrics against sample labels
```
