# Evaluation Methodology for LLM Output Audit v2

v2 must be benchmark-driven. This document defines how to evaluate the auditor itself.

## 1. Evaluation targets

Evaluate four layers independently:

1. Claim extraction
2. Evidence retrieval and planning
3. Verdict judgment
4. Revision suggestions

A good Markdown report is not enough. The system must produce structured artifacts that can be scored.

## 2. Benchmark layers

v2 uses three benchmark layers. They must be reported separately.

### Public Core Benchmark

Purpose: objective, reproducible fact-checking capability.

Examples:

- FEVER
- AVeriTeC
- Factcheck-Bench
- FActScore / LongFact / SAFE-style samples

This layer answers: can the auditor perform basic evidence-backed factuality evaluation?

### Technical Domain Benchmark

Purpose: software-engineering and documentation auditing.

Examples:

- GitHub metadata and source files
- package registry facts
- official docs feature claims
- release-note claims
- configuration defaults

This layer answers: can the auditor avoid generic web drift and verify project-specific technical claims?

### Private Workflow Benchmark

Purpose: product validation in the user's actual workflow.

Examples:

- BuJo research articles
- LLM Wiki pages
- local deployment notes
- research plans
- product usage guides

This layer answers: is the auditor actually useful for the user's durable writing and research workflow?

Private BuJo articles are product-validation cases, not the sole gold benchmark. They should not be committed to the public repository.

## 3. Benchmark case layout

```text
benchmark/cases/<case-id>/
  original.md
  metadata.json
  expected-claims.json
  expected-verdicts.json
  human-review.md
  notes.md
```

Private content should live in ignored directories such as `benchmark/private-*` or outside the repo. Use sanitized/synthetic cases for open-source CI.

## 4. Human review rubric

For each selected claim, reviewers answer:

- Was the claim correctly extracted?
- Is the claim atomic?
- Is the subject/scope/time context correct?
- Are the selected sources about the same subject?
- Is the evidence authoritative enough?
- Does the verdict follow from evidence?
- Is the suggested edit useful?
- Is the suggested edit safe to apply?

## 5. Metrics

### Claim extraction

- precision: extracted claims that are actually verifiable factual claims
- recall: expected important claims that were extracted
- atomicity rate: claims that contain only one checkable fact
- span accuracy: claims with correct source line/quote

### Retrieval and evidence

- evidence relevance
- source authority
- subject match rate
- contextual precision
- contextual recall
- missing canonical source rate

### Verdicts

- supported/refuted/NEI/conflict accuracy
- false positive rate for `refuted`
- false negative rate for known errors
- over-confirmation rate for weak evidence
- not-publicly-verifiable routing accuracy

### Suggestions

- useful suggestion rate
- patch-ready rate
- safe-to-apply precision
- unnecessary deletion/hedging rate

## 6. Regression policy

Any v2 core change should run:

```bash
python3 scripts/eval_auditor.py benchmark/cases --output eval-report.md
```

A change should not be accepted if it improves one metric by worsening a more important safety metric, especially:

- refuted false positives
- unsafe patch suggestions
- canonical-source mismatches
- subject mismatch in evidence

## 7. CI policy

Public CI should use synthetic or sanitized cases only. It should not require secrets or paid LLM calls.

CI can validate:

- schemas are valid JSON
- benchmark cases have required files
- deterministic evaluator reads cases
- sample artifacts match schemas
- no private paths or secrets are committed

Full LLM-backed evaluation can run locally or in a private environment.

## 8. Reporting format

`eval-report.md` should include:

- version/commit under test
- benchmark case list
- metric summary
- parsed human-review scorecards when available
- aggregate scorecard counts
- risky cases by quality dimension
- product decisions grouped across cases
- failures by layer
- worst cases
- regression vs previous run
- recommended next fixes

The report should separate product issues from implementation bugs.
