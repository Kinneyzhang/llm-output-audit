# LLM Output Audit Benchmark

This directory defines the v2 benchmark scaffold for evaluating the auditor itself.

## Privacy rule

Do not commit private BuJo research articles, local deployment notes, secrets, or host-specific paths. Private benchmark cases should live outside this public repository or under ignored local directories.

Committed cases should be synthetic, sanitized, or public-domain.

## Case layout

```text
benchmark/cases/<case-id>/
  original.md
  metadata.json
  expected-claims.json
  expected-verdicts.json
  human-review.md
  notes.md

  # Optional deterministic source pack for native v2 smoke cases.
  source-pack.json

  # Optional actual auditor outputs, committed only for public smoke cases.
  actual-claims.json
  actual-evidence.jsonl
  actual-verdicts.json
  actual-review-queue.json
  actual-suggestions.json
  actual-report.md
  actual-manifest.json
```

## Evaluation layers

1. Claim extraction
2. Evidence planning/retrieval
3. Verdict judging
4. Suggestion usefulness/safety

## v2 artifact contract

See [`docs/artifact-contract.md`](../docs/artifact-contract.md) for the normalized `actual-*` files. The short version: any auditor implementation can be evaluated if it writes `actual-claims.json`, `actual-evidence.jsonl`, `actual-verdicts.json`, `actual-review-queue.json`, and `actual-suggestions.json`.

Generate oracle smoke artifacts for a case:

```bash
python3 scripts/audit_v2.py --case benchmark/cases/000-smoke --oracle --output-dir /tmp/loa-v2/000-smoke
```

Evaluate case-local or external actual artifacts:

```bash
python3 scripts/eval_auditor.py benchmark/cases --output /tmp/eval.md --json-output /tmp/eval.json
python3 scripts/eval_auditor.py benchmark/cases --actual-root /tmp/loa-v2 --output /tmp/eval.md --json-output /tmp/eval.json
```

## Local private seeds

The current BuJo batch audit is useful as a private seed set, but should not be copied into this public repo. Use it to create human reviews locally, then sanitize selected examples if they should become public benchmark cases.
