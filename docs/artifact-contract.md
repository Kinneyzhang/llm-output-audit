# v2 Artifact Contract

`llm-output-audit` v2 separates the auditor pipeline from evaluation by requiring every auditor run to emit normalized artifacts. The evaluator can score any implementation that writes this contract, whether the implementation is the legacy v1 CLI, a native v2 pipeline, an MCP tool, or a benchmark oracle smoke run.

## Directory layout

A run directory contains:

```text
actual-claims.json
actual-evidence.jsonl
actual-verdicts.json
actual-review-queue.json
actual-suggestions.json
actual-report.md
actual-manifest.json
```

Benchmark cases use the same names inside each case directory or under an external `--actual-root/<case-id>/` directory.

## Artifacts

### `actual-claims.json`

Array of normalized atomic claims.

Required practical fields:

- `claim_id`: stable ID such as `c-001`
- `claim_text`: exact claim text
- `claim_type`: e.g. `ATTR`, `STATUS`, `NUMBER`, `FEATURE`
- `subject`: main entity when known
- `verifiability`: `public`, `local`, `mixed`, or `unknown`

The evaluator currently compares claim text overlap with `expected-claims.json`.

### `actual-evidence.jsonl`

One JSON object per evidence record.

Required practical fields follow `benchmark/schemas/evidence.schema.json`:

- `evidence_id`
- `claim_id`
- `source_type`
- `authority`
- `subject_match`
- `quote`
- `retrieved_at`
- `supports`
- `contradicts`
- `scores`

This is the v2 Evidence Ledger. Future judges must cite evidence IDs, not free-form snippets.

### `actual-verdicts.json`

Array of verdict records, one per audited claim.

Required practical fields follow `benchmark/schemas/verdicts.schema.json`:

- `claim_id`
- `truth_verdict`
- `audit_action`
- `evidence_ids`
- `confidence`
- `reason`

Allowed `truth_verdict` values:

- `supported`
- `partially_supported`
- `refuted`
- `not_enough_evidence`
- `conflicting_evidence`
- `not_publicly_verifiable`
- `not_a_factual_claim`

The evaluator compares actual verdicts with `expected-verdicts.json` on shared claim IDs.

### `actual-review-queue.json`

Human review work queue. This is how v2 avoids pretending every case can be auto-decided.

Common queues:

- `Must Fix`
- `Should Fix`
- `Needs Citation`
- `Needs Local Verification`
- `Human Review`
- `Safe`
- `Ignored`

### `actual-suggestions.json`

Patch-ready edit suggestions.

Common severities:

- `must_fix`
- `should_fix`
- `citation_needed`
- `local_verify`
- `optional`
- `none`

Suggestions may be conservative. `safe_to_apply: true` should only be used for low-risk citation/hedging edits, not factual rewrites that require human judgment.

### `actual-manifest.json`

Run metadata:

- contract version
- generation time
- source mode
- source path
- artifact file names
- artifact counts

## Current writer

`scripts/audit_v2.py` is the first native writer. It remains CI-safe when source packs are present or `--evidence-mode missing` is used, but real article runs can now use live source adapters plus an LLM-assisted judge.

Benchmark oracle smoke run:

```bash
python3 scripts/audit_v2.py \
  --case benchmark/cases/000-smoke \
  --oracle \
  --output-dir /tmp/loa-v2-artifacts/000-smoke
```

Native deterministic/hybrid v2 scaffold:

```bash
python3 scripts/audit_v2.py \
  --file path/to/article.md \
  --claim-extractor hybrid \
  --evidence-mode auto \
  --max-claims 80 \
  --output-dir /tmp/loa-v2-artifacts/native-run
```

This writes `article-profile.json`, `verification-plan.json`, and a human-readable `actual-report.md` in addition to the evaluator-facing `actual-*` JSON/JSONL artifacts. Native mode keeps at most `80` claims by default after article-aware filtering; override with `--max-claims N` when doing deeper review. See [`claim-extraction-strategy.md`](claim-extraction-strategy.md) for why v2 combines whole-article LLM extraction with claim-level evidence ledgers.

Evidence modes:

- `--evidence-mode auto` — default. Use deterministic `source-pack.json` when present; otherwise gather live evidence from available adapters.
- `--evidence-mode live` — force live evidence gathering when no source pack is used.
- `--evidence-mode missing` — offline/CI-safe mode that writes review records without network evidence.

Current live adapters include GitHub API metadata, Tavily web search when `TAVILY_API_KEY` is configured, Wikipedia fallback, local/private review checklists, and an LLM hybrid judge over retrieved snippets. Refutations are conservative: secondary web snippets alone do not create automatic `refuted` verdicts unless high-authority evidence supports the contradiction.

Native mode can consume a deterministic source pack. If `ARTICLE_DIR/source-pack.json` exists, it is loaded automatically; otherwise pass it explicitly:

```bash
python3 scripts/audit_v2.py \
  --file path/to/article.md \
  --source-pack path/to/source-pack.json \
  --output-dir /tmp/loa-v2-artifacts/native-run
```

A source pack is a JSON array of evidence records with `supports_claim_texts`, `contradicts_claim_texts`, or `missing_claim_texts`. The native judge converts those records into the Evidence Ledger and derives verdicts from support/contradiction/missing evidence instead of using Markdown-report text.

V1 trace conversion:

```bash
python3 scripts/audit_v2.py \
  --trace path/to/trace.jsonl \
  --output-dir /tmp/loa-v2-artifacts/from-v1
```

## Evaluation

Run evaluator against committed case-local artifacts:

```bash
python3 scripts/eval_auditor.py benchmark/cases \
  --output /tmp/loa-v2-eval.md \
  --json-output /tmp/loa-v2-eval.json
```

Run evaluator against external artifacts:

```bash
python3 scripts/eval_auditor.py benchmark/cases \
  --actual-root /tmp/loa-v2-artifacts \
  --output /tmp/loa-v2-eval.md \
  --json-output /tmp/loa-v2-eval.json
```

The evaluator reports:

- claim precision/recall
- verdict accuracy on shared IDs
- refuted false-positive candidates
- artifact coverage
- review queue counts
- suggestion severity counts

## Product rule

Every future v2 auditor change should be judged by artifacts and benchmark deltas, not by a manually inspected Markdown report alone.
