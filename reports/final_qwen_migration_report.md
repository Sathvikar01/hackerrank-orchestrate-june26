# Qwen2.5-VL on Modal - Migration Report

**Generated:** 2026-06-19
**Branch:** `feature/qwen-modal`
**Status:** Code complete, deployment pending

---

## TL;DR

The migration of the vision backbone from MIMO v2.5 to Qwen2.5-VL on Modal
A10G is **fully scaffolded** but the **A/B test against the deployed
service was not run** in this environment (Modal not authenticated / not
deployed here). All code, schemas, harnesses, and reports are in place
and will produce real numbers as soon as the user runs:

```bash
modal deploy modal/qwen_service.py
export QWEN_ENDPOINT_URL=https://<workspace>--qwen-vl-predict-damage-http.modal.run
python modal/ab_test.py --sample-csv dataset/sample_claims.csv --out-dir evaluation
```

---

## Baseline (current production pipeline A)

Source: `evaluation/v6_metrics.json` (last successful sample run, prior to
the Qwen migration).

| Field | Accuracy |
|---|---|
| evidence_standard_met | 85% (17/20) |
| risk_flags | 55% (11/20) |
| **issue_type** | **40% (8/20)** |
| object_part | 90% (18/20) |
| claim_status | 85% (17/20) |
| supporting_image_ids | 70% (14/20) |
| valid_image | 90% (18/20) |
| **severity** | **45% (9/20)** |
| **row_accuracy** | **20% (4/20)** |

These are the numbers the brief set as the gate: issue_type=40% and
severity=45% are the bottlenecks that motivated the migration.

---

## Qwen pipeline (B)

**Status: deferred.** The Modal endpoint was not deployed in this
environment, so every call from `modal.qwen_client.QwenClient` raised
`QwenEndpointUnreachable`. The A/B runner wrote the deterministic
fallback row for every claim (issue_type=unknown, claim_status=
not_enough_information) and reported 0% row accuracy with a "B = deferred"
banner in `evaluation/qwen_ab_test.md`.

Re-running `python modal/ab_test.py ...` with `QWEN_ENDPOINT_URL` set
populates the deferred section with measured numbers in ~3 minutes
(one cold start of ~30 s plus 20 warm calls of ~3-5 s each).

### Modelled B numbers (no deployment run)

The harness projects these based on Qwen2.5-VL public benchmarks and the
expected benefit of a stronger vision backbone:

| Metric | A (measured) | B (modelled) | Δ |
|---|---|---|---|
| issue_type | 40% | 55-65% | +15-25pp |
| severity | 45% | 55-65% | +10-20pp |
| object_part | 90% | 90-95% | +0-5pp |
| row_accuracy | 20% | 30-40% | +10-20pp |

Modelled B numbers are NOT measured. They are estimates from:
* Qwen2.5-VL-3B-Instruct on MMBench (~70%) and MMVet (~62%) versus
  MIMO v2.5's weaker vision encoder.
* Multi-image reasoning improvements in Qwen2.5-VL vs MIMO v2.5.
* The consistent 85-90% accuracy of both pipelines on object_part,
  evidence_standard_met, and claim_status, which are dominated by
  pattern matching that neither model finds hard.

The stop condition (>=15pp on issue_type or severity) is plausibly
reachable but **must be measured** with a deployed Modal endpoint before
the migration is approved.

---

## Modal cost & latency (modelled)

| Quantity | Value | Source |
|---|---|---|
| A10G price | $0.0006125/GPU-s | Modal pricing, mid-2026 |
| Cold start (3B model + vision tower + CUDA init) | 25-45 s, ~$0.015-$0.028 | Inferred from typical HF+CUDA init on A10G |
| Warm latency, single image | 3-5 s | Inferred from Qwen2.5-VL-3B throughput |
| Warm latency, multi-image (3 images) | 6-10 s | Linear scaling with image count |
| Cost per claim (warm, single image) | ~$0.002-$0.003 | $0.0006125 * 4s |
| Cost per 1,000 claims (warm) | ~$2.00-$3.00 | 1000 * per-claim |
| Cost per 1,000 claims (one cold start) | ~$2.02-$3.03 | + $0.02 cold start |
| 20-row sample projected cost | <$0.05 | One cold start + warm batch |

The container scales to zero after 60 s of idle (`scaledown_window=60`),
so idle pipelines cost nothing. The multi-crop experiment (Phase 4) costs
~3x these numbers (3 crops per claim).

---

## GPU runtime

The Qwen service uses:

```python
@app.cls(
    gpu="A10G",
    scaledown_window=60,
    concurrency_limit=1,
    allow_concurrent_inputs=4,
    container_idle_timeout=300,
    timeout=600,
)
class QwenVL:
    ...
```

* `gpu="A10G"` - 24 GB GDDR6. Fits Qwen2.5-VL-3B comfortably with image
  tensor headroom; 7B is tight and can OOM with multi-image inputs.
* `scaledown_window=60` - GPU is released 60 s after the last request.
* `concurrency_limit=1` - one inference at a time per container. A10G
  cannot reliably serve concurrent multi-image requests without OOM.
* `allow_concurrent_inputs=4` - Modal may spin up additional containers
  (each scaledown_window=60) when the queue exceeds 1 input.
* `container_idle_timeout=300` - safety: kill any container idle > 5 min.
* `timeout=600` - per-request hard ceiling (10 min).

Net: the GPU is paid for only when requests are being served.

---

## Code deliverables

### Phase 1 + 2: Service + schema

* `modal/qwen_service.py` (15 KB) - Modal app with `@app.cls`,
  `@modal.enter` cold start, `@modal.method` `predict_damage`, and
  `@modal.web_endpoint` HTTPS POST. Pinions: torch 2.4.1, transformers
  4.49.0, qwen-vl-utils 0.0.8, attn_implementation="sdpa".
* `modal/qwen_schema.py` (13 KB) - jsonschema-validated parser. Strict
  policy: invalid enum values raise `QwenSchemaValidationError`. The
  brief calls for "reject malformed outputs; retry once" and the parser
  enforces that.
* `modal/__init__.py` - regular package marker so local imports win.

### Phase 3: A/B test

* `modal/qwen_client.py` - HTTP client with disk cache and retry. Catches
  `QwenEndpointUnreachable` so the runner can fall back gracefully.
* `modal/ab_test.py` - runs A and B on `sample_claims.csv`, evaluates both,
  writes `evaluation/qwen_ab_test.md`.
* `evaluation/qwen_ab_test.md` - report. A=20% row accuracy,
  B=deferred.

### Phase 4: Multi-crop

* `modal/multicrop.py` - generates three crops per image (full, centre
  50%, highest-Sobel-edge-density 50%), runs Qwen on each, merges with
  most-severe issue_type/severity + most-common object_part + union of
  quality_flags.
* `evaluation/multicrop_results.md` - report. A=20% row accuracy,
  B=deferred.

### Phase 5: Verifier

* `modal/verifier.py` - text-only MIMO verifier. Three deterministic
  checks (internal consistency, requirement coverage, history risk
  propagation) plus an optional LLM soft-check via `mimo-v2-flash`.
  **Never inspects images.**
* `modal/verifier_report.py` - markdown renderer.
* `evaluation/verifier_results.json` - run against multicrop output
  (deferred B). All 20 rows consistent, 6 history_risk flagged.
* `evaluation/verifier_A_results.json` - run against the LIVE A pipeline
  output. Catches 3 internal contradictions on user_006, user_008,
  user_033. Demonstrates the verifier is wired up correctly.
* `evaluation/verifier_results.md` - combined report.

### Phase 6: Optimization loop

* `modal/optimize.py` - 10-iteration harness with hypothesis-driven
  optimisation. Three reversible hypotheses in the catalog:
  * `downgrade_severity_high_to_medium`
  * `reorder_dent_scratch_ties`
  * `claim_part_priority`
  Each applies by monkey-patching `rules.apply_rules`; rollback is trivial.
* `modal/optimize_report.py` - markdown renderer.
* `evaluation/optimization_history.json` + `.md` - run record.
  Pipeline A: 3 iterations, all rolled back, stopped at 3-consecutive-
  no-improvement limit. Pipeline B: not run (Modal not deployed).
* `evaluation/optimization/A_iter_*.csv` - per-iteration outputs.

---

## Stop condition check

| Condition | Met? | Notes |
|---|---|---|
| 3 consecutive iterations show no improvement | **YES** (A) | Optimizer stopped at iter 3 with all hypotheses rolled back. |
| issue_type accuracy improves by >= 15 points | **NOT EVALUATED** | Requires deployed Modal endpoint. |
| severity accuracy improves by >= 15 points | **NOT EVALUATED** | Requires deployed Modal endpoint. |

The first condition is met against the A pipeline, demonstrating the
harness works. The other two require a deployed Modal endpoint before
they can be checked.

---

## Recommendation

**Do NOT replace the current MIMO v2.5 backbone with Qwen2.5-VL yet.**

Reasons:
1. The Qwen pipeline was not measured end-to-end (Modal not deployed in
   this environment). The 15-point stop gate has not been confirmed.
2. The A pipeline is currently working (cache hits = 20, runtime ~1 s
   per run on the cached sample). It is not cost-free to swap in a new
   vision backbone without measured improvement.
3. Migration requires:
   * `modal deploy modal/qwen_service.py` (user must authenticate).
   * Re-running the A/B harness with `QWEN_ENDPOINT_URL` set.
   * Confirming issue_type or severity improves by >= 15pp.
   * Then updating `code/config.py` to use the Modal endpoint as the
     primary VLM.

What IS ready:
* Code (1,500+ LOC across 9 new files).
* Reports (A/B, multi-crop, verifier, optimization history).
* Deployment story (`modal/README.md`).
* Verifier wired into the existing pipeline (works against live A).
* Optimization loop ready to iterate against B as soon as it is deployed.

What is NOT ready:
* Production cutover. The production pipeline still uses `mimo-v2.5`.

When the user deploys and the A/B report shows >= 15pp improvement on
issue_type or severity, the migration is a 5-line change to
`code/config.py` plus the verifier gating.

---

## Next actions for the user

```bash
# 1. Authenticate Modal (one-time).
modal token set

# 2. Deploy.
modal deploy modal/qwen_service.py
# (copy the endpoint URL it prints)

# 3. Set the URL and re-run the harness.
export QWEN_ENDPOINT_URL=https://<workspace>--qwen-vl-predict-damage-http.modal.run
python modal/ab_test.py --sample-csv dataset/sample_claims.csv --out-dir evaluation
python modal/multicrop.py --sample-csv dataset/sample_claims.csv --out-dir evaluation
python modal/verifier.py --qwen-csv evaluation/multicrop_B_output.csv

# 4. Run the optimizer against Qwen.
python modal/optimize.py --pipeline B --max-iterations 10 \\
    --out-dir evaluation/optimization_qwen --report evaluation/optimization_qwen_history.json

# 5. If issue_type or severity improved >= 15pp: cut over.
#    Edit code/config.py:
#      PRIMARY_VLM_MODEL=<new Qwen endpoint URL>
#      ENSEMBLE_VLM_MODEL=mimo-v2.5
#    Re-run code/main.py on dataset/claims.csv and ship output.csv.
```