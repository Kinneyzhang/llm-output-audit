# v2 Claim Extraction Strategy

## Why not verify whole articles directly?

LLMs are good at global reading, but fact checking needs traceability. A whole-article verdict like "mostly accurate" is useful for a quick impression, but it is hard to debug, evaluate, reproduce, or patch.

v2 therefore uses a hybrid design:

```text
whole-article understanding
  -> select important claims
  -> atomic claim graph
  -> evidence per claim
  -> verdict per claim
  -> article-level summary/report
```

The key is that the claim extraction step should **not** mechanically split every sentence. Sentence splitting is only a deterministic fallback. The preferred path is article-aware claim graph extraction.

## Extractor modes

`scripts/audit_v2.py` supports:

```bash
--claim-extractor rule|llm|hybrid
```

- `rule`: deterministic fallback, CI-safe, no secrets, useful for smoke tests.
- `llm`: use an LLM to read the article globally and extract structured claims.
- `hybrid`: default. Use LLM when available for real articles; keep deterministic behavior for source-pack benchmark cases; fall back to rules when no LLM is configured.

Public benchmark cases with `source-pack.json` intentionally stay deterministic so CI remains reproducible.

## LLM extractor contract

The LLM returns JSON only:

```json
{
  "claims": [
    {
      "claim_text": "...",
      "claim_type": "DATE|NUMBER|EVENT|ATTR|STATUS|FEATURE|REQUIREMENT|COMPAT|WORKFLOW|EVAL|CAUSAL|ASSUMPTION",
      "subject": "...",
      "predicate": "...",
      "object": "...",
      "scope": "public|local|planning|article",
      "time_context": "current|historical|future|unspecified",
      "verifiability": "public|local|mixed|not_publicly_verifiable|not_factual",
      "importance": "high|medium|low",
      "risk_level": "high|medium|low",
      "source_quote": "..."
    }
  ]
}
```

The extractor is instructed to read the whole article first and select at most `--max-claims` important claims. It should prefer high-impact factual claims and skip headings, code blocks, pure advice, and filler.

## Why atomic claims are still needed

Atomic claims are not the user experience; they are the internal ledger. They make these things possible:

- Evidence can support or refute a specific statement.
- A false positive can be debugged at claim level.
- Patch suggestions can point to exact text.
- Regression tests can compute precision/recall/verdict accuracy.
- Local/private claims can be routed away from generic web search.

The final user-facing output is `actual-report.md`, which summarizes the claim-level ledger into a readable report.

## Efficiency policy

Use a staged budget:

1. Rule prefilter removes obvious noise.
2. LLM extracts at most `--max-claims` important claims.
3. Evidence routing only runs for selected claims.
4. Local/planning claims become `Needs Local Verification` instead of expensive web searches.
5. Public smoke benchmark cases use source packs and deterministic judging.

This balances accuracy and cost: global LLM judgment is used where it helps most, but evidence and evaluation remain structured.

## Current empirical signal

On three private BuJo validation articles, the rule-only sentence splitter previously selected about `203` claims after filtering. LLM-assisted extraction with `--max-claims 40` selected:

- GPT Researcher usage guide: `40` claims
- Emacs Lisp finetune plan: `38` claims
- Caddy architecture analysis: `40` claims

The selected claims are more article-aware: project stats, architecture/capability claims, causal claims, public-vs-planning distinctions, and local/private verifiability labels. This is not the final quality bar, but it is much closer to a usable audit experience than sentence-level splitting.
