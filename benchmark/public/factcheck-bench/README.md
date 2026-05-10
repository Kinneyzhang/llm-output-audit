# Factcheck-Bench Adapter Scaffold

## Purpose

Fine-grained benchmark for evaluating automatic fact-checkers over claim/sentence/document levels, including detection and revision.

## Why it matters for llm-output-audit v2

Best fit for evaluating the auditor itself, not only article factuality.

## Target normalized fields

- `layer`
- `source_dataset`
- `input_type`
- `metadata`
- `expected_claims`
- `expected_evidence`
- `expected_verdicts`
- `human_review` when available

## Adapter status

Scaffold only. Do not ingest the full dataset until the unified benchmark schema and evaluator behavior are stable.
