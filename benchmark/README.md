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
```

## Evaluation layers

1. Claim extraction
2. Evidence planning/retrieval
3. Verdict judging
4. Suggestion usefulness/safety

## Local private seeds

The current BuJo batch audit is useful as a private seed set, but should not be copied into this public repo. Use it to create human reviews locally, then sanitize selected examples if they should become public benchmark cases.
