---
name: llm-output-audit
description: Use when auditing long-form LLM-generated articles, technical reports, or research notes for factual accuracy, hallucination risk, internal consistency, source quality, and actionable edit suggestions.
version: 1.7.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [llm-output, audit, hallucination, verification, source-quality, editorial-review]
    related_skills: [tavily, llm-wiki, question-research, bilingual-article-translation]
---

# LLM Output Audit

## Overview

LLM-generated long-form outputs contain four common quality risks:

1. **Hallucinations** — confidently stated "facts" that are simply wrong (dates, numbers, names, causality)
2. **Stale knowledge** — things that were true at training cutoff but have since changed (version numbers, project status, organizational decisions)
3. **Unsupported claims** — plausible but uncited statements that need evidence or hedging
4. **Internal contradictions** — the article contradicts itself across sections, timelines, or comparisons

This skill runs a structured verification pipeline with an internal **Source Router**:

```
Article text
    ↓
⓪ Consistency risk gate — decide whether full internal consistency check is needed
    ↓
⓪b Internal consistency check — find contradictions inside the article when risk is high
    ↓
① Extract atomic verifiable claims (LLM pass)
    ↓
② Source Router — choose the best evidence channels per claim type/content
    ↓
③ Query routed sources:
   - Tavily / DuckDuckGo for general web + latest announcements
   - GitHub API for repos, stars, releases, project status
   - Wikipedia API for organizations, people, historical background
   - arXiv API for papers and academic publication metadata
   - Semantic Scholar API for citations, authors, paper influence
   - PyPI API for Python packages, versions, release dates
   - npm registry/download API for JavaScript packages and downloads
   - Optional LLM Wiki grep for local confirmed knowledge (`--use-wiki`)
    ↓
④ Fetch the best source URL and verify claim keywords appear in the actual page
    ↓
⑤ Rate each claim: ✅ confirmed / 🟡 likely / ⚠️ uncertain / ❌ wrong / 🔍 unsourced
    ↓
⑥ For ❌ claims, run adversarial second pass to reduce false positives
    ↓
⑦ Output structured report: verdict per claim + evidence + source route + edit suggestions
```

**What it catches well:**
- Specific dates ("released in April 2025")
- Specific numbers ("97M downloads", "10,000+ servers")
- Named events and their outcomes ("ACP merged into A2A")
- Attribution ("Google's A2A", "IBM's ACP")
- Causal/logical claims ("because X, therefore Y")

**What it cannot guarantee:**
- Opinions and interpretations (not verifiable by search)
- Very recent events (past few weeks, search lag)
- Niche topics with no web coverage

---

## When to Use

- Before publishing any research article, technical comparison, or usage guide
- After generating long-form content with DGX / GPT Researcher / any LLM
- Before saving durable outputs into BuJo `research/` or distilling them into LLM Wiki
- When a reader challenges a specific claim in an article
- When the user explicitly asks to audit, verify, check hallucinations, or assess accuracy
- Periodically re-checking older articles after major industry changes

**Don't use for:**
- Short conversational answers without high-risk current facts
- Purely opinion/analysis pieces with no specific factual claims
- Brainstorming or rough drafts that the user explicitly says are not for publication
- Real-time breaking news where search lag makes results unreliable unless sources are provided

---

### Audit Mode Policy — speed vs accuracy

Do not run the same depth for every task. Use modes to balance latency and reliability:

| Mode | Use for | Behavior |
|---|---|---|
| `fast` | ordinary low-risk chat | no full audit; use normal answering or spot-check only if needed |
| `spot` | high-risk short factual answer | audit up to 3 highest-importance claims, 1–2 routed sources each, no consistency/adversarial |
| `draft` | durable drafts: BuJo research, LLM Wiki, internal reports | audit up to 12 medium/high-importance claims, risk-gated consistency, conditional adversarial |
| `full` | public publishing, important reports, user-requested deep audit | audit up to 50 claims, more sources, full consistency, LLM router, adversarial enabled |
| `auto` | default | infer from claim count, article length, and consistency risk |

CLI:

```bash
--mode auto|fast|spot|draft|full
```

Default policy:

- short ordinary answer → `fast`
- high-risk short factual answer → `spot`
- generated long-form durable output → `draft`
- public/publish-critical/user-requested deep audit → `full`

### Parallelism policy

Use computer parallelism wherever it is safe:

1. **Claim-level parallelism** — multiple selected claims are verified concurrently.
2. **Source-level parallelism** — within one claim, routed sources are queried concurrently.
3. **Conditional expensive passes** — consistency, adversarial review, LLM router, and URL fetch run only when the mode/risk/evidence requires them.

CLI:

```bash
--workers 6          # parallel claim verification workers
--source-workers 4   # parallel evidence-source workers per claim
```

Keep defaults conservative to avoid API rate limits. Increase workers for local endpoints / generous APIs; reduce workers if a provider throttles.

---

## Execution Timing and Revision Policy

This skill is not only a report generator. It defines when to audit and what to do with the audit result.

### Mode A — Agent-generated durable output: auto-revise before delivery

Use this mode when the assistant is generating content that will be saved, published, or reused:

- research reports
- technical comparisons
- usage guides
- deployment writeups
- BuJo `research/` articles
- LLM Wiki distilled pages
- blog/README/docs drafts

Workflow:

```text
Draft internally
  ↓
Run llm-output-audit
  ↓
Apply safe revisions
  ↓
Re-check critical fixes if needed
  ↓
Deliver/save final version
```

Policy:

- Do **not** show the user an unreviewed draft as the final answer.
- Automatically fix all clear `❌ WRONG` issues when the audit provides a reliable correction.
- For `⚠️ UNCERTAIN`, reduce certainty, qualify the statement, specify the source, or mark it as needing verification.
- For `🔍 UNSOURCED`, either remove the claim, hedge it, or add `[citation needed]` depending on whether it is central to the article.
- For `🟡 LIKELY`, add or preserve a citation when practical.
- In the final response, summarize that an audit was run and mention major corrections; do not dump the full audit report unless the user asks.

### Mode B — User-supplied / existing text: report-first, do not mutate silently

Use this mode when the user gives an existing article, file, report, or external text and asks for review.

Workflow:

```text
Audit supplied text
  ↓
Return report + prioritized edit suggestions
  ↓
Wait for instruction before modifying the original file
```

Policy:

- Do **not** silently rewrite user-owned source files unless the user explicitly asks to apply fixes.
- If the user says "直接改 / apply fixes / 修掉", apply safe `❌` corrections and conservative `⚠️/🔍` wording changes, then report exactly what changed.
- Keep the audit report alongside the revised file when practical.

### Mode C — High-risk short answer: spot-check before final answer

For short answers containing high-risk factual claims — current project status, latest version, prices, dates, download counts, legal/medical/financial/security facts — do not run the full article pipeline. Instead, use the same source-routing principle for a targeted spot-check before answering.

```text
High-risk claim → source route → quick evidence check → answer with uncertainty/citation
```

### Mode D — User explicitly asks for audit

If the user says "审查 / 核查 / 查幻觉 / 准不准 / audit this", run the full audit. If they also say "直接改", use Mode B apply-fixes behavior.

---

## Phase 1 — Claim Extraction

Ask the LLM to extract atomic, individually-verifiable claims from the article. One claim per line. Format: `[TYPE] claim text`.

**Claim types:**
- `[DATE]` — specific dates or time periods
- `[NUMBER]` — statistics, counts, download numbers, version numbers
- `[EVENT]` — named events and their outcomes
- `[ATTR]` — attribution (who made what, who announced what)
- `[STATUS]` — current state of a project/protocol/tool
- `[CAUSAL]` — causal or logical claims ("X led to Y", "because X")

**Extraction prompt template:**

```
You are a fact-checking assistant. Extract every independently verifiable factual claim from the article below.

Rules:
- One atomic claim per line
- Format: [TYPE] claim text
- Include specific numbers, dates, names, events, project statuses
- Do NOT include opinions, recommendations, or subjective statements
- Do NOT rephrase — preserve the exact claim as stated in the article

Article:
{article_text}
```

**Target:** 10–30 claims per typical research article. If fewer than 5, the article may be too opinion-heavy for fact-checking. If more than 50, batch into groups of 20.

---

## Phase 2 — Verification

For each claim, run a targeted web search. Use the claim text as the search query, optionally adding context words.

### Search query construction

| Claim type | Query pattern |
|---|---|
| `[DATE]` | `"{subject}" release date OR announcement date` |
| `[NUMBER]` | `"{subject}" statistics OR downloads OR count site:official-source.com` |
| `[EVENT]` | `"{event name}" what happened confirmed` |
| `[ATTR]` | `"{thing}" created by OR developed by OR announced by` |
| `[STATUS]` | `"{project}" current status 2025 OR 2026` |
| `[CAUSAL]` | search the premise and conclusion separately |

### LLM Wiki cross-check

Before searching the web, check if a relevant LLM Wiki page exists:

```bash
grep -r "claim keywords" /home/geekinney/llm-wiki/entities/ \
     /home/geekinney/llm-wiki/concepts/ \
     /home/geekinney/llm-wiki/comparisons/ 2>/dev/null
```

If found: compare claim against wiki content. Wiki `status: confirmed` pages take precedence as local ground truth.

### Verification per claim

For each claim, collect:
- Up to 3 search results
- Any matching LLM Wiki content
- Your judgment: does the evidence support, contradict, or fail to address the claim?

---

## Phase 3 — Rating Each Claim

Rate every claim using this rubric:

| Rating | Symbol | Meaning |
|---|---|---|
| Confirmed | ✅ | Multiple sources agree, or official source confirms |
| Likely correct | 🟡 | One reliable source supports it, no contradiction found |
| Uncertain | ⚠️ | Conflicting sources, or claim is too vague to verify |
| Wrong | ❌ | Source explicitly contradicts the claim |
| Unsourced | 🔍 | No relevant source found either way |

**Confidence threshold rules:**
- Claim has official source (GitHub, official blog, LF announcement) → ✅ or ❌
- Claim has 2+ independent secondary sources agreeing → 🟡 minimum
- Only 1 source found → ⚠️ unless it's authoritative
- No sources found → 🔍 (not automatically ❌)

---

## Phase 4 — Audit Report Format

Output a structured audit report. Use this exact format so it's machine-parseable and human-readable:

```markdown
# LLM Output Audit Report: {article_title}
Checked: {date}
Claims extracted: {N}
Verdict summary: ✅ {n} confirmed | 🟡 {n} likely | ⚠️ {n} uncertain | ❌ {n} wrong | 🔍 {n} unsourced

---

## ✅ Confirmed Claims
- [ATTR] ACP merged into A2A under Linux Foundation
  Source: https://lfaidata.foundation/communityblog/2025/08/29/...

## 🟡 Likely Correct
...

## ⚠️ Uncertain / Needs Verification
...

## ❌ Wrong — Needs Correction
- [NUMBER] MCP has 97M+ monthly downloads
  Found: 132M+ as of late 2025 (70x growth year-over-year)
  Source: LinkedIn / npm-stat
  Fix: Replace "97M+" with "132M+"

- [NUMBER] 10,000+ MCP servers published
  Found: 17,000+ as of late 2025
  Source: digitalapplied.com MCP ecosystem guide
  Fix: Replace "10,000+" with "17,000+"

## 🔍 Unsourced — Could Not Verify
...

---

## Suggested Edits (copy-paste ready)
{list of exact old → new replacements}
```

---

## Consistency Risk Gate

Internal consistency checking is no longer triggered by article length alone. v1.5.0 uses a deterministic risk gate after claim extraction.

Signals:

- multiple `[STATUS]` claims
- dense timeline: 3+ `[DATE]` claims
- multiple `[CAUSAL]` claims
- comparison structure: `vs`, `对比`, `相比`, `优于`, etc.
- lifecycle/status keywords: deprecated, merged, active, maintained, 废弃, 并入, 迁移, 不再, 仍然活跃
- contradictory keyword pairs: deprecated/active, merged/independent, 废弃/维护, 并入/独立, etc.
- long article is only a weak +1 signal, never the sole deciding factor

Controls:

```bash
--skip-consistency    # never run internal consistency check
--force-consistency   # always run it regardless of risk score
```

The report distinguishes skipped checks from passed checks:

```markdown
## ⏭️ Internal Consistency: Skipped
```

---

## Source Router Design

v1 only used generic web search + LLM Wiki. v4 adds a deterministic source router so different kinds of claims are verified against the most authoritative source available.

**Routing principle:** don't ask one search engine everything. Ask the source that owns the fact.

Examples:

| Claim | Preferred sources |
|---|---|
| `github.com/org/repo has 10k stars` | GitHub API |
| `Package X has N monthly downloads` | npm downloads API / PyPI metadata |
| `Paper X was published by Y` | arXiv / Semantic Scholar |
| `OpenAI was founded in 2015` | Wikipedia / official web sources |
| `Project X is still active` | GitHub pushed_at / latest release / official docs |
| `DPO is more stable than RLHF` | arXiv / Semantic Scholar / web review |

The router has two modes:

1. **Rule router** — default. Fast, deterministic, zero extra tokens.
2. **LLM router** — optional via `--llm-router`. Useful for ambiguous claims, but slower and more expensive.

---

## RAG vs This Skill

This skill uses RAG-like retrieval, but it is not just RAG.

- RAG answers questions by retrieving context and generating text.
- This skill audits an existing article: extract claims → route sources → gather evidence → rate → adversarial review → suggest edits.

RAG is one component of the skill, not the whole system. The skill's advantage is editorial workflow: systematic claim extraction, source-specific routing, false-positive control, and concrete correction suggestions.

---

## Automation with Script

For articles stored as local Markdown files, use the bundled script:

```bash
python3 ~/.hermes/skills/research/llm-output-audit/scripts/fact_check.py \
    --file /path/to/article.md \
    --output /path/to/report.md \
    --mode draft \
    --workers 6 \
    --source-workers 4
```

Optional local knowledge-base enhancement:

```bash
python3 ~/.hermes/skills/research/llm-output-audit/scripts/fact_check.py \
    --file /path/to/article.md \
    --output /path/to/report.md \
    --use-wiki \
    --wiki /home/geekinney/llm-wiki
```

The script:
1. Reads the article
2. Calls DeepSeek / OpenAI-compatible LLM to extract claims
3. Uses audit mode policy (`fast` / `spot` / `draft` / `full` / `auto`) to choose how many claims and sources to audit
4. Uses the Source Router to select the best evidence channels for each selected claim
5. Queries specialized sources in parallel where appropriate: GitHub, Wikipedia, arXiv, Semantic Scholar, PyPI, npm, Tavily/DDG; optionally local LLM Wiki when `--use-wiki` is enabled
6. Uses a deterministic consistency risk gate to decide whether internal contradiction checking is needed; supports `--skip-consistency` and `--force-consistency`
7. Scores evidence by authority / directness / freshness and sorts stronger evidence first
8. Fetches generic web URLs and checks whether claim keywords appear in the actual page (`🔗✓` marker); skips fetch for structured API evidence
9. Calls the LLM to rate each claim with evidence and an actionable edit suggestion
10. Runs conditional adversarial second-pass review on ❌ claims, skipping it when high-authority structured evidence is sufficient
11. Writes the report with audit mode, selected/total claim count, `Routed sources`, `Source quality`, and `Edit Suggestions` sections
12. For agent-generated durable outputs, apply safe corrections and wording changes before final delivery; for user-supplied existing files, report first and only modify when explicitly asked

See `scripts/fact_check.py` for full implementation.

---

## Manual Workflow (no script)

1. Paste the article and say: **"Extract all verifiable claims from this article, one per line, prefixed with [DATE]/[NUMBER]/[EVENT]/[ATTR]/[STATUS]/[CAUSAL]"**
2. For each extracted claim, say: **"Search and verify: {claim}"**
3. After all claims checked, say: **"Generate the fact-check report in the standard format"**

---

## Effectiveness Expectations

Based on testing against AI/tech research articles:

| Error type | Detection rate | Notes |
|---|---|---|
| Wrong numbers/stats | ~90% | High — numbers are searchable |
| Wrong dates | ~85% | High — official announcements indexed |
| Wrong attribution | ~80% | Good — most major projects have clear docs |
| Stale status info | ~75% | Good — but requires recent search results |
| Wrong causal claims | ~40% | Harder — requires deeper reasoning |
| Opinion presented as fact | ~30% | Requires human judgment |

**Realistic outcome:** Catch 3–8 errors per typical 2000-word tech article generated purely from LLM. Zero errors possible for well-sourced articles.

---

## Common Pitfalls

1. **Extracting too many claims** — Opinion sentences slip in as "claims". Stick to TYPE prefixes; if a sentence can't be typed, skip it.

2. **Treating "unsourced" as "wrong"** — 🔍 means the web didn't help, not that the claim is false. Niche facts about internal tools, local deployments, or very recent events often come back 🔍.

3. **Single-source confirmation** — One blog post saying X doesn't confirm X. Prefer official sources (GitHub releases, official blogs, Linux Foundation announcements).

4. **Search lag** — Events from the past 2–4 weeks may not be well-indexed. Mark as ⚠️ rather than ❌.

5. **Over-correcting** — If a correction source is itself a blog post of uncertain quality, flag as ⚠️ rather than replacing the original claim.

6. **Assuming LLM Wiki exists.** LLM Wiki is an optional local knowledge source. Use `--use-wiki --wiki /path/to/wiki` only when the user has one configured; otherwise the skill should work without it.

---

## Real-World Benchmark

Tested on: `ai-agent-protocols-mcp-a2a-acp-anp.md` (~2000 words, AI/agent protocols overview)

| Metric | Manual | Skill (automated) |
|---|---|---|
| Time | ~30 min | **128 sec** |
| Claims checked | 4 (spot-check) | **27 (systematic)** |
| Confirmed correct | — | 8 |
| Issues surfaced | 4 known errors | 1 ❌ + 7 ⚠️ new issues |
| False positives | 0 | 1 (search noise) |

**What it caught beyond manual review:**
- "ANP 由社区发起" — actually founded by one person, not a community (⚠️)
- "MCP/A2A 生产可用" — no official source confirming this status (⚠️)
- "AWS/Google/MS/OpenAI 加入 MCP 治理委员会" — no direct source (🔍)
- `Next.js 153M` downloads, `MCP 17,000+ servers` — no source found (🔍)

**What it missed / got wrong:**
- Numbers already corrected by manual research → came back 🔍 (search didn't re-find sources)
- `132M downloads` → mis-rated ❌ due to unrelated search result (false positive)
- Bare date strings ("2024年", "2025年8月") extracted as claims → high-noise extractions

**Practical conclusion:**  
The skill is best used as a first-pass filter that surfaces ⚠️/🔍 items for human follow-up — not as a fully autonomous verifier. Treating ❌ as definitive requires checking the correction evidence. The efficiency gain (~93% time reduction) and coverage gain (systematic vs spot-check) are the primary values.

---

## Verification Checklist

- [ ] All claims extracted and typed ([DATE]/[NUMBER]/[EVENT]/[ATTR]/[STATUS]/[CAUSAL])
- [ ] Each claim searched with at least 1 targeted query
- [ ] Optional LLM Wiki checked only when `--use-wiki` is explicitly enabled
- [ ] Every claim has a rating (✅/🟡/⚠️/❌/🔍)
- [ ] ❌ claims have: what was found + source URL + exact suggested fix
- [ ] Report written in standard format
- [ ] Article patched with all ❌ corrections
- [ ] Report saved alongside article or in BuJo research/
