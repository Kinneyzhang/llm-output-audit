# FEVER Adapter Scaffold

## Purpose

Classic fact verification: claim -> Wikipedia evidence -> Supported/Refuted/NotEnoughInfo.

## Why it matters for llm-output-audit v2

Best for basic retrieval + entailment + NEI behavior. Weak for real-world temporal web claims and long-form article workflows.

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
