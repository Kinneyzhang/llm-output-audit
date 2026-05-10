# v2 vs v1 Public Smoke Comparison

Date: 2026-05-10  
Branch: `v2-design`  
Compared commit: `ae9ce0d`  
Scope: public smoke benchmark cases only.

## Purpose

This comparison checks whether the v2 artifact/evidence-ledger direction is practically measurable against the existing v1 pipeline.

It is not a full product-quality benchmark yet. The public cases are tiny smoke cases, and v2 currently uses deterministic `source-pack.json` evidence. The value of this run is that the comparison path is now real:

```text
same benchmark cases
  -> v1 fact_check.py + trace -> v2 trace converter -> evaluator
  -> v2 native source-pack pipeline -> evaluator
  -> metric comparison
```

## Commands used

v2 native artifacts:

```bash
for case in benchmark/cases/*; do
  python3 scripts/audit_v2.py \
    --file "$case/original.md" \
    --output-dir "/tmp/loa-compare-v2/$(basename "$case")"
done
python3 scripts/eval_auditor.py benchmark/cases \
  --actual-root /tmp/loa-compare-v2 \
  --output /tmp/loa-compare-v2-eval.md \
  --json-output /tmp/loa-compare-v2-eval.json
```

v1 pipeline converted to v2 artifacts:

```bash
for case in benchmark/cases/*; do
  id=$(basename "$case")
  python3 scripts/fact_check.py \
    --file "$case/original.md" \
    --output "/tmp/loa-compare-v1/$id/audit-report.md" \
    --mode spot \
    --workers 2 \
    --source-workers 2 \
    --no-fetch \
    --trace-log "/tmp/loa-compare-v1/$id/trace.jsonl"
  python3 scripts/audit_v2.py \
    --trace "/tmp/loa-compare-v1/$id/trace.jsonl" \
    --output-dir "/tmp/loa-compare-v1/$id"
done
python3 scripts/eval_auditor.py benchmark/cases \
  --actual-root /tmp/loa-compare-v1 \
  --output /tmp/loa-compare-v1-eval.md \
  --json-output /tmp/loa-compare-v1-eval.json
```

## Results

### v1 converted artifacts

- cases: `8`
- valid cases: `8`
- cases with actual artifacts: `8`
- average claim precision: `0.6328`
- average claim recall: `0.8125`
- average verdict accuracy on shared claims: `0.0625`
- review queues: `Needs Citation = 16`
- suggestion severities: `citation_needed = 16`

Observed failure pattern:

- v1 often extracted extra surrounding claims from tiny benchmark articles.
- v1 trace conversion mostly produced `not_enough_evidence` because v1 Markdown/verdict details are not normalized enough for direct evaluator scoring.
- v1 still has useful human-facing reports, but the structured regression path is weak.

### v2 native source-pack artifacts

- cases: `8`
- valid cases: `8`
- cases with actual artifacts: `8`
- average claim precision: `1.0`
- average claim recall: `1.0`
- average verdict accuracy on shared claims: `1.0`
- failure cases: `0`
- review queues:
  - `Safe = 6`
  - `Must Fix = 4`
  - `Needs Citation = 1`
- suggestion severities:
  - `must_fix = 4`
  - `citation_needed = 1`

Observed strength:

- v2 emits normalized artifacts by design.
- v2 verdicts are derived from Evidence Ledger support/contradiction/missing markers.
- The evaluator can directly measure extraction and verdict quality without scraping Markdown prose.

## Private BuJo sanity check

Running the current deterministic v2 scaffold directly on the three private BuJo cases is not yet useful as a quality claim:

- cases: `3`
- cases with actual artifacts: `3`
- claim precision: `0.0`
- claim recall: `0.0`
- verdict accuracy: `0.0`
- generated `1181` citation-needed items

Interpretation:

This does **not** mean the v2 architecture is worse. It means the current native scaffold is still a smoke-level sentence splitter, not the real claim graph extractor. The private cases confirm the next needed implementation step: replace the deterministic toy extractor with an article-aware claim graph extractor and local/public verifiability classifier.

## Conclusion

The public smoke comparison supports the v2 direction:

1. v1 is useful as a human-facing audit report generator, but weak as a regression-evaluable product because its outputs are Markdown/trace-first.
2. v2 is already better as an engineering product skeleton because claims, evidence, verdicts, review queues, and suggestions are first-class artifacts.
3. The next real quality step is not more benchmark plumbing. It is the native claim graph extractor and article-aware policy layer.

## Next implementation target

Build the native v2 claim graph extractor:

```text
article
  -> article profile
  -> source spans
  -> atomic claims
  -> subject/predicate/object/scope/time/verifiability
  -> public vs local vs not-publicly-verifiable classification
```

This should replace the current smoke sentence splitter. Once done, rerun the same v1-v2 comparison on:

- public smoke benchmark
- technical-domain benchmark
- private BuJo validation cases

The success criterion for the next phase is not `1.0` on public smoke. It is materially improved private-case claim extraction and fewer false `citation_needed` items on local/planning claims.
