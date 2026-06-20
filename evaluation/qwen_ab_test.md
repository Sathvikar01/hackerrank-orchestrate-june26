# Qwen2.5-VL A/B Test Report

**Generated:** 2026-06-19 10:13:51 UTC
**Sample CSV:** `dataset\sample_claims.csv` (20 rows)

## Status

**B = live Modal run** - all rows evaluated via deployed Qwen2.5-VL.

**Note:** Modal deployed: hackerrank-orchestrate-qwen (Qwen2.5-VL-3B on A10G). B is the live Qwen pipeline.

## A. Current production pipeline (mimo-v2.5 + rule engine)

Runtime: 0.9 s
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

Runtime: 119.8 s
Succeeded: 20 / 20
Transport failures: 0
Schema parse failures: 0
Mean per-request latency: 0 ms (warm only)

| Field | Accuracy |
|---|---|
| evidence_standard_met | 90.00% |
| risk_flags | 50.00% |
| issue_type | 55.00% |
| object_part | 85.00% |
| claim_status | 65.00% |
| supporting_image_ids | 55.00% |
| valid_image | 90.00% |
| severity | 40.00% |
| **row_accuracy** | **10.00%** |

## Head-to-head deltas

| Field | A | B | Delta |
|---|---|---|---|
| evidence_standard_met | 85.00% | 90.00% | +5.00% |
| risk_flags | 55.00% | 50.00% | -5.00% |
| issue_type | 40.00% | 55.00% | +15.00% |
| object_part | 90.00% | 85.00% | -5.00% |
| claim_status | 85.00% | 65.00% | -20.00% |
| supporting_image_ids | 70.00% | 55.00% | -15.00% |
| valid_image | 90.00% | 90.00% | +0.00% |
| severity | 45.00% | 40.00% | -5.00% |
| row_accuracy | 20.00% | 10.00% | -10.00% |

## B confusion matrix (issue_type)

| user_id | claim_object | predicted | ground_truth |
|---|---|---|---|
| user_007 | car | crack | broken_part |
| user_005 | car | dent | scratch |
| user_006 | car | crack | unknown |
| user_008 | car | scratch | broken_part |
| user_011 | laptop | water_damage | stain |
| user_020 | laptop | crack | none |
| user_031 | package | stain | water_damage |
| user_033 | package | crushed_packaging | unknown |
| user_034 | package | torn_packaging | none |

## B confusion matrix (severity)

| user_id | claim_object | predicted | ground_truth |
|---|---|---|---|
| user_002 | car | medium | low |
| user_005 | car | medium | low |
| user_006 | car | medium | unknown |
| user_008 | car | medium | high |
| user_009 | laptop | high | medium |
| user_010 | laptop | high | medium |
| user_012 | laptop | medium | low |
| user_018 | laptop | high | medium |
| user_020 | laptop | medium | none |
| user_032 | package | medium | unknown |
| user_033 | package | high | low |
| user_034 | package | medium | none |

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
