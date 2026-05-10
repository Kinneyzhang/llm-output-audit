# Public Benchmark Adapters

This directory contains scaffolds for public, reproducible benchmark datasets.

Do not download or commit large raw datasets here. Adapter scripts should convert raw records from local caches or downloaded sources into the unified benchmark case schema.

## Layers

- `fever/`: classic claim verification against Wikipedia evidence.
- `averitec/`: real-world claims with question-answer evidence and richer labels.
- `factcheck-bench/`: fine-grained evaluation of automatic fact-checkers across claim/sentence/document levels.
- `factscore/`: long-form factual precision via atomic facts.
- `technical-domain/`: synthetic/sanitized software engineering cases under our control.

## Adapter contract

Each adapter should eventually support:

```bash
python3 adapter.py --input RAW_DATA --output benchmark/cases/<target> --sample 50
```

Adapters must be deterministic and must not call an LLM.
