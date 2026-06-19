# Verifier Report (Text-Only)

**Generated:** 2026-06-19 08:50:36 UTC
**Text judge model:** `mimo-v2-flash` (LLM path: disabled (deterministic only))
**Sample size:** 20 rows
**Runtime:** 0.66s

## Summary

| Metric | Value |
|---|---|
| Consistent | 20 / 20 (100.0%) |
| Inconsistent | 0 / 20 (0.0%) |
| History risk propagated | 6 / 20 (30.0%) |
| `user_history_risk` added | 6 |
| `manual_review_required` added | 2 |

## Verifier checks

The verifier runs three deterministic checks and an optional LLM check.
**It never inspects images.**

1. **Internal consistency** of the Qwen output:
   * `visible_damage=true` but `issue_type=none|unknown` -> contradiction.
   * `severity != none` but `visible_damage=false` -> contradiction.
   * `issue_type` is concrete but `object_part=unknown` -> contradiction.
   * `severity != unknown` but `evidence_sufficient=false` -> contradiction.
2. **Requirement coverage**:
   * For `REQ_GENERAL_OBJECT_PART`, `object_part=unknown` while a concrete
     `issue_type` was identified -> contradiction.
3. **User history risk propagation** (deterministic):
   * `past_claim_count >= 5` AND `(rejected + manual_review) >= 1` ->
     `user_history_risk`.
   * `last_90_days_claim_count >= 3` -> `user_history_risk`.
   * `history_flags` contains `fraud` or `high_risk` -> `manual_review_required`.
   * `rejected_claim >= 2` -> `manual_review_required`.
4. **LLM soft check** (when enabled): calls MIMO text model to flag
   contradictions or risk-flag additions that the deterministic checks
   cannot see (e.g. claim mentions "front" but Qwen says "rear").

## Per-row verdicts

| user_id | claim_object | consistent | errors | history flags | added flags |
|---|---|---|---|---|---|
| user_001 | car | yes |  |  |  |
| user_002 | car | yes |  |  |  |
| user_004 | car | yes |  | user_history_risk | user_history_risk |
| user_007 | car | yes |  |  |  |
| user_005 | car | yes |  | user_history_risk<br>manual_review_required | user_history_risk<br>manual_review_required |
| user_006 | car | yes |  |  |  |
| user_003 | car | yes |  |  |  |
| user_008 | car | yes |  | user_history_risk | user_history_risk |
| user_009 | laptop | yes |  |  |  |
| user_010 | laptop | yes |  |  |  |
| user_011 | laptop | yes |  |  |  |
| user_012 | laptop | yes |  |  |  |
| user_018 | laptop | yes |  | user_history_risk | user_history_risk |
| user_020 | laptop | yes |  |  |  |
| user_015 | package | yes |  |  |  |
| user_030 | package | yes |  |  |  |
| user_031 | package | yes |  | user_history_risk | user_history_risk |
| user_032 | package | yes |  |  |  |
| user_033 | package | yes |  | user_history_risk<br>manual_review_required | user_history_risk<br>manual_review_required |
| user_034 | package | yes |  |  |  |

## Inconsistent rows (full detail)

_None._

## Gate

Phase 5 is gated on Phase 3 showing improvement (`issue_type` or `severity`
+15 points). The verifier is wired up and validated on the live A
pipeline output above; it catches 0 internal inconsistencies
and propagates risk flags to 6 rows. It is ready to run against
real Qwen outputs as soon as the Modal deployment is live.


---

## Appendix: verifier_A_results

# Verifier Report (Text-Only)

**Generated:** 2026-06-19 08:50:36 UTC
**Text judge model:** `mimo-v2-flash` (LLM path: disabled (deterministic only))
**Sample size:** 20 rows
**Runtime:** 0.64s

## Summary

| Metric | Value |
|---|---|
| Consistent | 17 / 20 (85.0%) |
| Inconsistent | 3 / 20 (15.0%) |
| History risk propagated | 6 / 20 (30.0%) |
| `user_history_risk` added | 6 |
| `manual_review_required` added | 2 |

## Verifier checks

The verifier runs three deterministic checks and an optional LLM check.
**It never inspects images.**

1. **Internal consistency** of the Qwen output:
   * `visible_damage=true` but `issue_type=none|unknown` -> contradiction.
   * `severity != none` but `visible_damage=false` -> contradiction.
   * `issue_type` is concrete but `object_part=unknown` -> contradiction.
   * `severity != unknown` but `evidence_sufficient=false` -> contradiction.
2. **Requirement coverage**:
   * For `REQ_GENERAL_OBJECT_PART`, `object_part=unknown` while a concrete
     `issue_type` was identified -> contradiction.
3. **User history risk propagation** (deterministic):
   * `past_claim_count >= 5` AND `(rejected + manual_review) >= 1` ->
     `user_history_risk`.
   * `last_90_days_claim_count >= 3` -> `user_history_risk`.
   * `history_flags` contains `fraud` or `high_risk` -> `manual_review_required`.
   * `rejected_claim >= 2` -> `manual_review_required`.
4. **LLM soft check** (when enabled): calls MIMO text model to flag
   contradictions or risk-flag additions that the deterministic checks
   cannot see (e.g. claim mentions "front" but Qwen says "rear").

## Per-row verdicts

| user_id | claim_object | consistent | errors | history flags | added flags |
|---|---|---|---|---|---|
| user_001 | car | yes |  |  |  |
| user_002 | car | yes |  |  |  |
| user_004 | car | yes |  | user_history_risk | user_history_risk |
| user_007 | car | yes |  |  |  |
| user_005 | car | yes |  | user_history_risk<br>manual_review_required | user_history_risk<br>manual_review_required |
| user_006 | car | **no** | severity='none' but evidence_sufficient=false |  |  |
| user_003 | car | yes |  |  |  |
| user_008 | car | **no** | severity='high' but evidence_sufficient=false | user_history_risk | user_history_risk |
| user_009 | laptop | yes |  |  |  |
| user_010 | laptop | yes |  |  |  |
| user_011 | laptop | yes |  |  |  |
| user_012 | laptop | yes |  |  |  |
| user_018 | laptop | yes |  | user_history_risk | user_history_risk |
| user_020 | laptop | yes |  |  |  |
| user_015 | package | yes |  |  |  |
| user_030 | package | yes |  |  |  |
| user_031 | package | yes |  | user_history_risk | user_history_risk |
| user_032 | package | yes |  |  |  |
| user_033 | package | **no** | severity='none' but evidence_sufficient=false | user_history_risk<br>manual_review_required | user_history_risk<br>manual_review_required |
| user_034 | package | yes |  |  |  |

## Inconsistent rows (full detail)

- **user_006** (car): errors=["severity='none' but evidence_sufficient=false"]; history_flags=[]; recommended_risk_flags_to_add=[]; explanation='deterministic checks only'
- **user_008** (car): errors=["severity='high' but evidence_sufficient=false"]; history_flags=['user_history_risk']; recommended_risk_flags_to_add=['user_history_risk']; explanation='deterministic checks only'
- **user_033** (package): errors=["severity='none' but evidence_sufficient=false"]; history_flags=['user_history_risk', 'manual_review_required']; recommended_risk_flags_to_add=['user_history_risk', 'manual_review_required']; explanation='deterministic checks only'

## Gate

Phase 5 is gated on Phase 3 showing improvement (`issue_type` or `severity`
+15 points). The verifier is wired up and validated on the live A
pipeline output above; it catches 3 internal inconsistencies
and propagates risk flags to 6 rows. It is ready to run against
real Qwen outputs as soon as the Modal deployment is live.
