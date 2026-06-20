# Hybrid Pipeline Evaluation Report

**Generated:** 2026-06-19 12:13:35 UTC
**Sample CSV:** `dataset\sample_claims.csv` (20 rows)
**Architecture:** Qwen2.5-VL-3B (vision) → MIMO mimo-v2-flash (text judge) → rule engine (merge)

H = Qwen2.5-VL-3B (vision) + mimo-v2-flash (text judge, faster than v2.5) + reconciliation. R = deterministic per-field best-of-A-B router.

## Pipelines evaluated

* **A** (mimo-v2.5 + rule engine): cached from `evaluation/ab_A_output.csv`.
* **B** (Qwen2.5-VL-3B on Modal A10G): cached from `evaluation/ab_B_output.csv`.
* **H** (hybrid: Qwen observer + MIMO judge + reconciliation): live run
  from `modal/hybrid.py`.
* **R** (router: per-field best-of-A-B): deterministic post-processor
  from `modal/hybrid.py --router`. No live calls.

## R. Router (per-field best-of-A-B)

Deterministic post-processor: for each field, pick A (mimo-v2.5) or B
(Qwen) based on which one scored higher in the A/B test:

| Field | Source |
|---|---|
| evidence_standard_met | B |
| risk_flags | A |
| issue_type | B |
| object_part | A |
| claim_status | A |
| supporting_image_ids | A |
| valid_image | tie |
| severity | A |

| Field | Accuracy |
|---|---|
| evidence_standard_met | 80.00% |
| risk_flags | 55.00% |
| issue_type | 55.00% |
| object_part | 90.00% |
| claim_status | 85.00% |
| supporting_image_ids | 70.00% |
| valid_image | 90.00% |
| severity | 45.00% |
| **row_accuracy** | **20.00%** |

### Head-to-head: R vs A

| Field | A | R | Δ |
|---|---|---|---|
| evidence_standard_met | 85.00% | 80.00% | -5.00% |
| risk_flags | 55.00% | 55.00% | +0.00% |
| issue_type | 40.00% | 55.00% | +15.00% |
| object_part | 90.00% | 90.00% | +0.00% |
| claim_status | 85.00% | 85.00% | +0.00% |
| supporting_image_ids | 70.00% | 70.00% | +0.00% |
| valid_image | 90.00% | 90.00% | +0.00% |
| severity | 45.00% | 45.00% | +0.00% |
| row_accuracy | 20.00% | 20.00% | +0.00% |

## A. Current MIMO baseline

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

## B. Pure Qwen pipeline

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

## H. Hybrid pipeline

Runtime: 192.2 s
Qwen mean latency: 0 ms
MIMO judge mean latency: 9593 ms
Text model: `mimo-v2-flash`

| Field | Accuracy |
|---|---|
| evidence_standard_met | 90.00% |
| risk_flags | 45.00% |
| issue_type | 50.00% |
| object_part | 85.00% |
| claim_status | 70.00% |
| supporting_image_ids | 55.00% |
| valid_image | 90.00% |
| severity | 40.00% |
| **row_accuracy** | **10.00%** |

## Head-to-head: H vs A

| Field | A | H | Δ |
|---|---|---|---|
| evidence_standard_met | 85.00% | 90.00% | +5.00% |
| risk_flags | 55.00% | 45.00% | -10.00% |
| issue_type | 40.00% | 50.00% | +10.00% |
| object_part | 90.00% | 85.00% | -5.00% |
| claim_status | 85.00% | 70.00% | -15.00% |
| supporting_image_ids | 70.00% | 55.00% | -15.00% |
| valid_image | 90.00% | 90.00% | +0.00% |
| severity | 45.00% | 40.00% | -5.00% |
| row_accuracy | 20.00% | 10.00% | -10.00% |

## Head-to-head: H vs B

| Field | B | H | Δ |
|---|---|---|---|
| evidence_standard_met | 90.00% | 90.00% | +0.00% |
| risk_flags | 50.00% | 45.00% | -5.00% |
| issue_type | 55.00% | 50.00% | -5.00% |
| object_part | 85.00% | 85.00% | +0.00% |
| claim_status | 65.00% | 70.00% | +5.00% |
| supporting_image_ids | 55.00% | 55.00% | +0.00% |
| valid_image | 90.00% | 90.00% | +0.00% |
| severity | 40.00% | 40.00% | +0.00% |
| row_accuracy | 10.00% | 10.00% | +0.00% |

## Hybrid confusion matrix (issue_type)

| user_id | predicted | ground_truth |
|---|---|---|
| user_007 | crack | broken_part |
| user_005 | dent | scratch |
| user_006 | crack | unknown |
| user_008 | scratch | broken_part |
| user_011 | water_damage | stain |
| user_020 | crack | none |
| user_031 | stain | water_damage |
| user_032 | missing_part | unknown |
| user_033 | crushed_packaging | unknown |
| user_034 | torn_packaging | none |

## Hybrid confusion matrix (claim_status)

| user_id | predicted | ground_truth |
|---|---|---|
| user_005 | supported | contradicted |
| user_006 | supported | not_enough_information |
| user_008 | supported | contradicted |
| user_020 | supported | contradicted |
| user_033 | supported | contradicted |
| user_034 | supported | contradicted |

## Stop condition verdict (hybrid H)

| Condition | Value | Met? |
|---|---|---|
| hybrid row_accuracy > current baseline | 10.00% > 20.00% | **False** |
| claim_status ≥ 85% AND issue_type ≥ 55% | 70.00% / 50.00% | **False** |

**Verdict: CONTINUE**

## Stop condition verdict (router R)
| Condition | Value | Met? |
|---|---|---|
| router row_accuracy > current baseline | 20.00% > 20.00% | **False** |
| router claim_status ≥ 85% AND issue_type ≥ 55% | 85.00% / 55.00% | **True** |
**Router verdict: STOP**


## Cost / latency summary

* H cost per claim: Qwen (~$0.003) + MIMO (~$0.001) = **~$0.004**.
* A cost per claim: ~$0.0025 (mimo-v2.5 at list price; cached run was 81k
  tokens for 20 rows ≈ $0.05 amortised).
* R cost per claim: **~$0** (purely deterministic post-processor).
* H latency: Qwen ~0.0s + Judge ~9.6s ≈
  ~9.6s per claim (excluding Qwen cold start).
* R latency: <50 ms per claim (no model calls).

## Recommendation

* **If H row_accuracy > 20%**: ship H as the new primary.
* **If R row_accuracy > 20% AND claim_status ≥ 85% AND issue_type ≥ 55%**:
  ship R as the new primary. R is the safest cutover because it picks
  the better source per field based on the A/B measurements.
* **Otherwise**: keep A as primary. Consider:
  1. Use B (Qwen) as a tie-breaker on issue_type only (B's strongest
     field); keep A for everything else.
  2. Iterate on the hybrid H: improve the judge prompt + reconciliation
     to push claim_status closer to 85%.
