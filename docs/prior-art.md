# Prior Art for LLM Output Audit v2

This document records external systems and research ideas that should shape v2. The goal is not to clone any product, but to adopt proven abstractions.

## 1. LLM observability products

### LangSmith

Useful ideas:

- trace-first debugging
- datasets and experiments
- annotation queues
- pairwise and single-run human review
- rubric-based feedback

How v2 uses this:

- `review-queue.json` is a first-class artifact.
- Human review templates use rubrics.
- Low-quality audit runs can be promoted into benchmark cases.

### Langfuse / Galileo / Arize Phoenix

Useful ideas:

- traces, spans, sessions, observations
- online/offline evaluation
- datasets generated from production traces
- experiment comparison over time
- retrieval and RAG evaluations attached to traces

How v2 uses this:

- `trace.jsonl` becomes structured span telemetry, not just debugging output.
- Each audit stage has input/output/latency/error metadata.
- Benchmark evaluation can compare runs across versions.

## 2. RAG evaluation frameworks

### RAGAS / TruLens / DeepEval

Useful ideas:

- context relevance
- groundedness / faithfulness
- answer relevance
- contextual precision / recall
- LLM-as-judge with rubrics

How v2 adapts this:

- `evidence_relevance`: evidence is about the same claim and subject.
- `source_authority`: source is canonical/official/primary/secondary/weak.
- `evidence_coverage`: evidence covers subject, predicate, object, and qualifiers.
- `verdict_groundedness`: verdict is entailed by evidence ids.
- `suggestion_faithfulness`: proposed edits are supported by evidence ids.

## 3. Fact-checking research

### FEVER

Useful ideas:

- claim retrieval
- evidence sentence retrieval
- claim verification
- verdicts require evidence

How v2 adapts this:

- Evidence ledger records exact quotes/spans.
- Verdicts cite evidence ids.
- Refuted claims require contradiction evidence, not just weak search results.

### AVeriTeC

Useful ideas:

- real-world claim verification
- question-answer pairs supported by web evidence
- verdict labels: supported, refuted, not enough evidence, conflicting evidence
- metadata such as claim date and context

How v2 adapts this:

- Evidence planner generates verification questions.
- Evidence records can include question-answer pairs.
- `conflicting_evidence` and `not_publicly_verifiable` are explicit states.

## 4. Long-form factuality

### FActScore

Useful ideas:

- decompose long-form text into atomic facts
- validate each atomic fact against reliable knowledge sources
- compute factual precision instead of binary document judgment

How v2 adapts this:

- Claim graph keeps atomic claims.
- Score long articles by section and claim class.
- Do not claim whole-article reliability when only a sample of claims was checked.

### SAFE / LongFact

Useful ideas:

- search-augmented factuality evaluation
- multi-step reasoning over search results
- fact-level precision/recall-like metrics

How v2 adapts this:

- Evidence planner can run iterative search when first evidence is weak.
- Search expansion must be bounded by source authority and subject match.
- Long-form reports expose public/local/configuration precision separately.

## 5. OpenFactCheck / Factcheck-Bench

Useful ideas:

- modular fact-checking systems
- evaluating the fact-checker itself
- document/sentence/claim-level evaluation
- detection and revision evaluation

How v2 adapts this:

- Separate `audit_v2.py` from `eval_auditor.py`.
- Benchmark cases evaluate claim extraction, evidence, verdict, and suggestions.
- Revision quality is measured, not assumed.

## 6. LLM-as-judge practice

Useful ideas:

- rubrics are mandatory
- structured output beats free text
- judge outputs need calibration against human labels
- different tasks need different evaluators

How v2 adapts this:

- Use article-type and claim-type specific rubrics.
- LLM judge output must reference evidence ids.
- Canonical deterministic evidence cannot be overridden by LLM judgment.

## 7. What not to copy

- Do not turn the project into a hosted SaaS.
- Do not require paid observability platforms.
- Do not make LLM Wiki required.
- Do not rely on one generic LLM judge for every task.
- Do not add endless source adapters before fixing claim/evidence modeling.
