# FActScore / Long-form factuality Adapter Scaffold

## Purpose

Long-form outputs decomposed into atomic facts and scored by factual precision against reliable sources.

## Why it matters for llm-output-audit v2

Best fit for atomic facts and section-level factual precision.

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
