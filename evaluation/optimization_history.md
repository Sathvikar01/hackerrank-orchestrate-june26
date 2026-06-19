# Optimization Loop Report (A)

**Generated:** 2026-06-19 09:00:02 UTC
**Iterations run:** 3
**Stop reason:** rolled_back (stop: 3 consecutive no-improvement)
**Kept:** 0 | **Rolled back:** 3

## Baseline vs final

| Metric | Baseline | Final | Δ |
|---|---|---|---|
| row_accuracy | 20.00% | 20.00% | 0.00pp |
| issue_type | 40.00% | 40.00% | - |
| severity | 45.00% | 45.00% | - |
| object_part | 90.00% | 90.00% | - |

## Iteration log

| iter | field | hypothesis | outcome | Δrow | Δissue | Δseverity |
|---|---|---|---|---|---|---|
| 0 | - | - | baseline | - | - | - |
| 1 | issue_type | reorder_dent_scratch_ties | rolled_back | +0% | +0% | +0% |
| 2 | severity | downgrade_severity_high_to_medium | rolled_back | +0% | +0% | +0% |
| 3 | object_part | claim_part_priority | rolled_back (stop: 3 consecutive no-improvement) | +0% | +0% | +0% |

## Notes

* Each iteration: confusion matrix -> pick largest error category -> apply
  matching hypothesis -> re-evaluate -> keep or roll back.
* Stop conditions: 3 consecutive no-improvement iterations, OR
  issue_type/severity improves by >= 15 percentage points.
* The hypothesis catalog is intentionally small (3 entries). When all
  are exhausted without improvement, the loop stops.
* Against the A pipeline (mimo-v2.5) the loop ran 3 iterations,
  tried every hypothesis, and stopped at the no-improvement limit. The
  baseline is the same as v6: row_accuracy=20%, issue_type=40%,
  severity=45%.
* Run against the B pipeline (Qwen) with ``--pipeline B`` and a deployed
  Modal endpoint to repeat the exercise on the new vision backbone.
