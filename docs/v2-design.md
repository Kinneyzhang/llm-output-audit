# LLM Output Audit v2 Design

Status: native v2 scaffold with LLM-assisted claim extraction and live evidence loop  
Branch: `v2-design`  
Scope: v2 remains an alpha design branch; v1 is still the stable release until benchmark coverage and live-source quality are broader.

## 1. Product goal

LLM Output Audit v2 is a portable audit system for long-form LLM outputs. It should help humans and agents decide whether a saved article, technical report, comparison, deployment note, or usage guide is accurate enough to publish, store in a knowledge base, or revise.

v2 is not just a deeper search script. It is a structured evidence-and-review workflow:

```text
Article
  -> Article Classifier
  -> Claim Graph
  -> Evidence Planner
  -> Evidence Ledger
  -> Hybrid Judge
  -> Review Queue
  -> Patch Suggestions
  -> Benchmark Harness
```

The current v1 CLI remains the stable baseline. v2 adds structured artifacts beside the Markdown report before any breaking CLI changes.

## 2. Design principles

1. Benchmark first: no major core change without a regression dataset.
2. Article-aware auditing: product guides, local deployment notes, research plans, and public explainers need different policies.
3. Structured claims: every claim needs source span, subject, predicate, object, scope, and verifiability metadata.
4. Evidence before verdict: a verdict must be grounded in evidence ids, not generic search snippets.
5. Deterministic sources win: canonical APIs, official docs, source files, and local verified files override LLM guesses.
6. Human review is part of the product: uncertain items become review queue entries with rubrics.
7. Suggestions must be patch-ready: old text, new text, severity, reason, evidence ids, and safe-to-apply flag.
8. MCP exposes stages: other agents should call individual stages, not only a black-box audit command.

## 3. Core artifacts

v2 audit output should be an artifact directory. The current evaluator-facing contract is documented in [`artifact-contract.md`](artifact-contract.md) and uses `actual-*` file names so benchmark expected files and auditor outputs can live side by side:

```text
audit-artifacts/
  actual-claims.json
  actual-evidence.jsonl
  actual-verdicts.json
  actual-review-queue.json
  actual-suggestions.json
  actual-manifest.json
  source-pack.json        # optional deterministic benchmark/local evidence input
```

The native scaffold now consumes `source-pack.json` when present and derives verdicts from evidence support/contradiction/missing markers. For real articles without a source pack, it can gather live evidence through the v1 Source Router bridge plus GitHub API, official docs/README retrieval, Wikipedia fallback, and local/private review checklists, then use an LLM hybrid judge over the retrieved snippets. This keeps the judge evidence-ledger-driven instead of hard-coding Markdown verdicts.

The native v2 engine may also emit richer intermediate planning files later:

```text
audit-artifacts/
  article-profile.json
  verification-plan.json
  verification-questions.json
  audit-report.md
  trace.jsonl
```

Markdown is a rendering layer. JSON/JSONL are the source of truth for automation and evaluation.

## 4. Article profile

The first stage classifies the document and chooses an audit policy.

Fields:

- `article_type`: `technical_explainer`, `product_usage_guide`, `local_deployment_note`, `project_comparison`, `research_plan`, `project_research`, `incident_postmortem`, `opinion_essay`
- `primary_subjects`: named projects, tools, repos, services, or concepts
- `audit_policy`: policy key used by routing and judging
- `requires_local_context`: whether local files/configs/logs may be authoritative
- `preferred_sources`: canonical source families
- `weak_sources`: sources that can support background but should not confirm current/project-specific claims

## 5. Claim graph

Claims are structured objects, not loose `[TYPE] text` lines.

Required fields:

- `claim_id`
- `source_span`: file, start_line, end_line, quote
- `claim_text`
- `claim_type`: `DATE`, `NUMBER`, `EVENT`, `ATTR`, `STATUS`, `FEATURE`, `REQUIREMENT`, `COMPAT`, `WORKFLOW`, `EVAL`, `CAUSAL`, `METADATA`, `ASSUMPTION`
- `subject`, `predicate`, `object`
- `scope`, `time_context`
- `verifiability`: `public`, `local`, `mixed`, `not_publicly_verifiable`, `not_factual`
- `importance`, `risk_level`

A claim without a subject is not ready for retrieval. It should go to clarification or be repaired using article context.

## 6. Evidence planning

Before searching, v2 creates a verification plan per claim:

- verification questions
- preferred source order
- target repos/docs/files
- forbidden or weak source classes
- local verification needs
- fallback behavior

Example: GPT Researcher defaults should search the GPT Researcher repo/docs/config first; they should not search generic pages about password iteration counts.

## 7. Evidence ledger

Evidence is stored as JSONL records with explicit support semantics.

Required ideas:

- `evidence_id`
- `claim_id`
- `source_type`: `github_api`, `github_file`, `official_docs`, `local_file`, `llm_wiki`, `paper`, `web`, etc.
- `authority`: `canonical`, `official`, `primary`, `secondary`, `weak`
- `subject_match`: `exact`, `partial`, `ambiguous`, `mismatch`
- `quote`, `url`, `retrieved_at`
- `supports`, `contradicts`, `missing`
- scores: `retrieval_relevance`, `source_authority`, `evidence_coverage`

A high score requires both relevant content and correct subject match.

## 8. Verdict model

Separate truth verdict from editing action.

Truth verdict:

- `supported`
- `partially_supported`
- `refuted`
- `not_enough_evidence`
- `conflicting_evidence`
- `not_publicly_verifiable`
- `not_a_factual_claim`

Audit action:

- `keep`
- `cite`
- `hedge`
- `rewrite`
- `remove`
- `local_verify`
- `human_review`
- `ignore`

The v1 symbols can be rendered from these fields, but they should not be the primary data model.

## 9. Hybrid judge

v2 judgment order:

1. Deterministic verifier: canonical APIs, source files, local metadata, exact docs matches.
2. Rule judge: authority, freshness, subject match, coverage, contradiction.
3. LLM judge: only for semantic entailment and synthesis, bound to evidence ids.

LLM judge cannot override canonical evidence; it can only explain or flag uncertainty.

## 10. Review queue

Every non-trivial item should map to a review queue:

- `Must Fix`
- `Should Fix`
- `Needs Citation`
- `Needs Local Verification`
- `Human Review`
- `Safe`
- `Ignored`

Each item has a rubric so humans can mark whether the claim extraction, evidence, verdict, and suggestion are correct.

## 11. MCP v2 tools

Keep v1 tools, then add staged tools:

- `classify_article`
- `extract_claims_v2`
- `plan_evidence`
- `gather_evidence`
- `judge_claims`
- `build_review_queue`
- `suggest_patches`
- `run_benchmark`
- `summarize_audit_artifacts`

## 12. Migration strategy

- v1 remains the stable default.
- v2 starts as `scripts/audit_v2.py` and artifact schemas.
- Do not replace `scripts/fact_check.py` until benchmark metrics show v2 improvement.
- Markdown reports remain backward-compatible for humans.
