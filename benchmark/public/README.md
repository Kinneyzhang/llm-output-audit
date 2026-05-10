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

## Smoke conversion

The repository includes small JSONL samples under `benchmark/public/samples/`. They are synthetic/sample-shaped records, not full upstream datasets.

Example:

```bash
python3 scripts/build_public_benchmark.py --output /tmp/loa-public-cases --clean-public
```

or run one adapter directly:

```bash
python3 benchmark/public/averitec/adapter.py \
  --input benchmark/public/samples/averitec.sample.jsonl \
  --output benchmark/cases \
  --sample 10
```

Generated public cases live in `benchmark/cases/public-*` and are safe for CI.

The generated smoke cases include the full v2 `actual-*` artifact set generated from expected artifacts so `scripts/eval_auditor.py` can exercise actual-vs-expected metrics deterministically in CI. Real auditor runs should write those files from the auditor pipeline instead. The deterministic native scaffold can be invoked with `scripts/audit_v2.py --file ARTICLE --output-dir OUT`.
