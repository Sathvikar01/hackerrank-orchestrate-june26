# Repository analysis

## Challenge

HackerRank Orchestrate June 2026 — 24-hour hackathon. Build a system that
verifies visual evidence for damage claims across three object types
(`car`, `laptop`, `package`). For each claim, output a structured row
with 14 columns including `claim_status` (supported / contradicted /
not_enough_information), `issue_type`, `object_part`, `severity`, and
`risk_flags`.

## Repo layout

```
AGENTS.md            # mandatory agent rules + log file path
problem_statement.md # I/O schema and allowed values
README.md            # quickstart
code/                # solution lives here
  main.py            # CLI entry point
  evaluation/main.py # evaluation entry point
dataset/
  sample_claims.csv  # 20 labeled rows (dev)
  claims.csv         # 44 input-only rows (test)
  user_history.csv   # 47 users with history flags
  evidence_requirements.csv  # 11 rules
  images/{sample,test}/<case_NNN>/img_*.jpg
```

## Dataset scale (verified)

| Set | Rows | Cases | Images |
|---|---|---|---|
| sample_claims.csv | 20 | 20 | 29 |
| claims.csv (test) | 44 | 43 | 82 |

8 test images are unreadable by PIL (`PIL.UnidentifiedImageError`).
The pipeline filters them and emits a `not_enough_information` row.

## Output schema (exact order)

`user_id, image_paths, user_claim, claim_object, evidence_standard_met,
evidence_standard_met_reason, risk_flags, issue_type, object_part,
claim_status, claim_status_justification, supporting_image_ids,
valid_image, severity`

`evidence_standard_met`, `valid_image` are boolean (`true` / `false`).
`issue_type`, `object_part`, `severity`, `claim_status`, `risk_flags`
are strict enums (see `code/schema.py`).

## Scoring assumptions

The challenge does not publish an exact scoring formula. Inferred:
- Exact-match accuracy on the enum columns (`claim_status`, `issue_type`,
  `object_part`, `severity`, `evidence_standard_met`, `valid_image`).
- Set-match on `risk_flags` (semicolon-separated).
- LLM-judge groundedness on the two free-text justification columns.
- Reproducibility + judge-interview quality matter for ranking.

## Hidden assumptions surfaced

- Multi-part claims (one row, multiple parts mentioned) must be resolved
  to a single `issue_type` / `object_part`. Strategy: most-severe visible.
- User history flags propagate to `risk_flags` only, never to
  `claim_status`.
- Prompt-injection attempts (instructions inside the transcript) must be
  ignored and flagged.
- The four "image-quality / authenticity" risk flags can be produced by
  the VLM and/or by deterministic CV detectors.
