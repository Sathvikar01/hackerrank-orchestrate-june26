# Qwen2.5-VL A/B Test Report

**Generated:** 2026-06-19 08:36:19 UTC
**Sample CSV:** `dataset\sample_claims.csv` (20 rows)

## Status

**B = deferred (Modal not deployed)** - every row returned the deterministic fallback (all fields = `unknown`/0%). Deploy `modal/qwen_service.py` and re-run to fill in real metrics.

**Note:** Modal not deployed in this run; B is deterministic fallback. Re-run after modal deploy modal/qwen_service.py and setting QWEN_ENDPOINT_URL.

## A. Current production pipeline (mimo-v2.5 + rule engine)

Runtime: 1.2 s
Tokens consumed: 81,264
Cache hits: 20

| Field | Accuracy |
|---|---|
| evidence_standard_met | 85.00% |
| risk_flags | 55.00% |
| issue_type | 40.00% |
| object_part | 90.00% |
| claim_status | 85.00% |
| supporting_image_ids | 70.00% |
| valid_image | 90.00% |
| severity | 45.00% |
| **row_accuracy** | **20.00%** |

## B. Qwen2.5-VL pipeline (Modal A10G)

Runtime: 0.0 s
Succeeded: 0 / 20
Transport failures: 20
Schema parse failures: 0
Mean per-request latency: 0 ms (warm only)

| Field | Accuracy |
|---|---|
| evidence_standard_met | 0.00%  (stub fallback) |
| risk_flags | 0.00%  (stub fallback) |
| issue_type | 0.00%  (stub fallback) |
| object_part | 0.00%  (stub fallback) |
| claim_status | 0.00%  (stub fallback) |
| supporting_image_ids | 0.00%  (stub fallback) |
| valid_image | 0.00%  (stub fallback) |
| severity | 0.00%  (stub fallback) |
| **row_accuracy** | **0.00%  (stub fallback)** |

## Head-to-head deltas

| Field | A | B | Delta |
|---|---|---|---|
| evidence_standard_met | 85.00% | 0.00% (stub) | -85.00% |
| risk_flags | 55.00% | 0.00% (stub) | -55.00% |
| issue_type | 40.00% | 0.00% (stub) | -40.00% |
| object_part | 90.00% | 0.00% (stub) | -90.00% |
| claim_status | 85.00% | 0.00% (stub) | -85.00% |
| supporting_image_ids | 70.00% | 0.00% (stub) | -70.00% |
| valid_image | 90.00% | 0.00% (stub) | -90.00% |
| severity | 45.00% | 0.00% (stub) | -45.00% |
| row_accuracy | 20.00% | 0.00% (stub) | -20.00% |

## B confusion matrix (issue_type)

| user_id | claim_object | predicted | ground_truth |
|---|---|---|---|
| - | - | (no errors) | - |

## B confusion matrix (severity)

| user_id | claim_object | predicted | ground_truth |
|---|---|---|---|
| - | - | (no errors) | - |

## Cost projection

B never produced a successful inference, so warm latency is unknown. Re-run after deployment to populate this section.

Modelled estimate (Qwen2.5-VL-3B on A10G, single image, 1024 px): **3-5 s warm**, **~$0.002-$0.003 per claim**, **~$0.02 cold-start overhead** per burst. 44-row test set fits in a single cold start.

## Recommendation

* If B improves issue_type or severity by >= 15 points vs A: replace the
  primary VLM with Qwen2.5-VL on Modal.
* If B improves 5-15 points: keep MIMO v2.5 as ensemble member and use Qwen
  for tie-breaking.
* If B improves < 5 points: keep current pipeline. Qwen still adds value as
  a verification cross-check.
* If Modal is not deployed: re-run this script after `modal deploy
  modal/qwen_service.py` to replace the deferred block above with measured
  numbers.
