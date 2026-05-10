# Benchmark Strategy for LLM Output Audit v2

Status: design scaffold  
Scope: public benchmark + technical-domain benchmark + private workflow validation.

## 1. Why this exists

Private BuJo research articles are valuable because they represent the real workflow this toolkit must support. They are not enough to prove the auditor is generally accurate.

A credible v2 benchmark suite needs three layers:

```text
Public Core Benchmark
  -> objective, reproducible factuality capability
Technical Domain Benchmark
  -> software/docs/repo/package/source-code auditing capability
Private Workflow Benchmark
  -> real user workflow and product usefulness
```

Each layer answers a different question. Mixing them into one score would be misleading.

## 2. Layer 1: Public Core Benchmark

Purpose: measure general fact-checking ability against externally labeled data.

Candidate datasets:

- FEVER
- AVeriTeC
- Factcheck-Bench
- FActScore / LongFact / SAFE-style samples
- Optional hallucination sets such as HaluEval or HalluLens for refusal/unknown behavior

What this layer measures:

- claim verification
- evidence retrieval
- supported/refuted/not-enough-evidence/conflict labels
- atomic fact extraction
- grounded verdict generation
- false-positive and false-negative behavior

What it does not measure well:

- local deployment notes
- private user context
- project-specific source code defaults
- whether suggestions are useful for this user's writing workflow

## 3. Layer 2: Technical Domain Benchmark

Purpose: measure the auditor on software engineering facts that public fact-checking datasets underrepresent.

Candidate case types:

- GitHub repository metadata: stars, license, archived status, primary language
- GitHub source/doc claims: config defaults, feature support, environment variables
- package registry facts: PyPI/npm versions, dependencies, Python/Node requirements
- release notes: latest version, breaking changes, deprecations
- documentation claims: feature support, integration support, deployment requirements

What this layer measures:

- project-scoped evidence planning
- canonical source preference
- source-code/file verification
- package/repo deterministic checks
- prevention of generic web query drift

This layer should include synthetic and sanitized cases in the public repo. It can also include local private cases for projects installed on the user's machine, but those must remain ignored.

## 4. Layer 3: Private Workflow Benchmark

Purpose: validate whether the auditor is useful in the user's actual writing/research/deployment workflow.

Sources:

- BuJo research articles
- LLM Wiki distilled pages
- deployment notes
- product usage guides
- research plans
- local experiment writeups

What this layer measures:

- local-context handling
- `not_publicly_verifiable` behavior
- `local_verify` behavior
- assumption vs fact classification
- usefulness and safety of suggestions
- whether reports reduce human review effort

Privacy rule: this layer is local-only by default and must not be committed to the public repo.

## 5. Unified case schema

All benchmark layers should be normalized into the same internal shape:

```json
{
  "case_id": "...",
  "layer": "public_core | technical_domain | private_workflow",
  "source_dataset": "fever | averitec | factcheck_bench | factscore | synthetic | private_bujo",
  "input_type": "claim | article | atomic_fact | qa_evidence_case",
  "metadata": {},
  "expected_claims": [],
  "expected_evidence": [],
  "expected_verdicts": [],
  "human_review": {}
}
```

The evaluator should not care whether a case started as FEVER, AVeriTeC, or a private BuJo article after normalization.

## 6. Scoring policy

Report scores by layer and article/case type. Do not collapse everything into one global score.

Minimum score groups:

- claim extraction quality
- evidence relevance
- source authority
- subject match
- verdict accuracy
- false refuted rate
- false supported rate
- not-enough-evidence accuracy
- suggestion usefulness
- patch safety

## 7. Dataset adapter policy

Adapters should be deterministic and small:

```text
raw dataset record -> normalized benchmark case
```

Adapters should not run LLMs. LLM calls belong in the auditor/evaluator pipeline, not in dataset conversion.

Adapters should support:

- `--sample N`
- `--split train/dev/test` where applicable
- `--output benchmark/cases/...`
- local cache paths

## 8. Initial implementation order

1. Add public benchmark scaffold docs.
2. Add adapter stubs for FEVER, AVeriTeC, Factcheck-Bench, FActScore.
3. Add one synthetic public case per dataset shape.
4. Extend `eval_auditor.py` to understand `layer` and `source_dataset`.
5. Only then ingest real public datasets.
6. Keep private BuJo validation as local-only product validation.

## 9. Decision

Private BuJo benchmark is not removed. It is reclassified:

```text
private BuJo cases = product validation set
public datasets = core capability benchmark
technical synthetic/public cases = software-domain reliability benchmark
```

This prevents overfitting to personal articles while preserving the exact use case the tool is meant to serve.
