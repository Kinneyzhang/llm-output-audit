# Technical Domain Synthetic Cases Adapter Scaffold

## Purpose

Sanitized software-engineering cases for repo/package/docs/config facts.

## Why it matters for llm-output-audit v2

Fills the gap public fact-check datasets do not cover: GitHub metadata, source-code defaults, package requirements, docs features.

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
