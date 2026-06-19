# Multi-Crop Experiment Report

**Generated:** 2026-06-19 08:41:34 UTC
**Sample CSV:** `dataset\sample_claims.csv` (20 rows)

## Status

**B (multicrop) = deferred (Modal not deployed).** All claims fell back to the deterministic stub. Re-run with `QWEN_ENDPOINT_URL` set after `modal deploy`.

**Note:** Modal not deployed; B is deterministic stub. Re-run after modal deploy.

## Crops

Per image, three crops:

1. **full** - the original image, resized to 1024 px max dimension.
2. **center** - centred 50%-area crop.
3. **edge_density** - the 50%-area window with the highest Sobel edge
   density. Falls back to centre when the image is too uniform.

Each crop is sent independently to Qwen2.5-VL. Predictions are merged:

* `issue_type`: most severe non-"unknown" answer.
* `object_part`: most common non-"unknown" answer.
* `severity`: most severe non-"unknown" answer.
* `visible_damage`: OR across crops.
* `evidence_sufficient`: OR across crops.
* `quality_flags`: union.

## A. Single-shot Qwen (baseline for comparison)

Runtime: 1.2 s

| Field | Accuracy |
|---|---|
| issue_type | 40.00% |
| severity | 45.00% |
| object_part | 90.00% |
| claim_status | 85.00% |
| **row_accuracy** | **20.00%** |

## B. Multi-crop Qwen

Runtime: 1.3 s
Total crop calls: 20
Succeeded claims: 0 / 20
Transport failures: 20
Schema parse failures: 0

| Field | Accuracy |
|---|---|
| issue_type | 0.00% |
| severity | 0.00% |
| object_part | 0.00% |
| claim_status | 0.00% |
| **row_accuracy** | **0.00%** |

## Head-to-head deltas

| Field | A (single) | B (multicrop) | Delta |
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

## Cost projection

B never produced a successful inference, so warm latency is unknown. Re-run after deployment to populate this section.

Modelled estimate (Qwen2.5-VL-3B on A10G, single image, 1024 px): **3-5 s warm per crop**, **3 crops per claim**, **~$0.018-$0.030 per claim**, **~$0.02 cold-start overhead** per burst.

## Recommendation

* If multicrop improves issue_type or severity by >= 5 points vs single:
  use multicrop in production.
* If multicrop improves < 5 points: stick with single-shot, multi-crop is
  not worth the 3x cost.
* Edge-density crop is the most expensive step; it adds no value on images
  that are already centred on the damage. Consider enabling it only when
  the single-shot prediction has confidence < some threshold.
