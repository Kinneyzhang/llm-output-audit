# LLM Output Audit Demo Report: smoke.md

Checked: 2026-05-10
Claims audited: 5 / 5 extracted
Audit mode: spot
Verdict summary: ✅ CONFIRMED 2 | 🟡 LIKELY 1 | ⚠️ UNCERTAIN 1 | 🔍 UNSOURCED 1

---

## ✅ Confirmed

- **[ATTR]** OpenAI was founded in 2015 by Sam Altman and others.
  - Routed sources: wikipedia, tavily_web
  - Source quality: score=0.88 structured=False
  - Evidence: Multiple public sources describe OpenAI as founded in 2015 by Sam Altman, Greg Brockman, Ilya Sutskever, Wojciech Zaremba, John Schulman, and others.
  - Source: https://en.wikipedia.org/wiki/OpenAI

- **[STATUS]** github.com/assafelovic/gpt-researcher is an open-source project.
  - Routed sources: github, tavily_web
  - Source quality: score=0.91 structured=True
  - Evidence: The GitHub repository is public and includes an open-source license.
  - Source: https://github.com/assafelovic/gpt-researcher

## 🟡 Likely Correct

- **[FEATURE]** The audit tool can route different claims to different evidence sources.
  - Routed sources: github, docs
  - Source quality: score=0.82 structured=True
  - Evidence: The implementation contains a rule-based Source Router and source adapters for GitHub, package registries, papers, web search, and optional local knowledge.
  - Source: scripts/fact_check.py
  - Suggestion: Keep this claim with a link to the Source Router section or the implementation.

## ⚠️ Uncertain / Needs Human Review

- **[EVAL]** Source routing is always more accurate than generic web search.
  - Routed sources: tavily_web, arxiv, semantic_scholar
  - Source quality: score=0.45 structured=False
  - Evidence: Source routing is usually better for structured claims, but “always more accurate” is too broad without a benchmark.
  - Suggestion: Rewrite as “Source routing can improve evidence quality for structured claims by querying more appropriate sources.”

## 🔍 Unsourced — Could Not Verify

- **[NUMBER]** A package has a specific monthly npm download count.
  - Routed sources: npm, tavily_web
  - Source quality: score=0.61 structured=True
  - Evidence: Package metadata can confirm identity and version, but exact monthly download counts require a reliable download-statistics endpoint or citation.
  - Suggestion: Add a direct npm downloads API citation or hedge the number.

---

## 📝 Edit Suggestions

### High priority

- Replace “Source routing is always more accurate than generic web search” with:
  - “Source routing can improve evidence quality for structured claims by querying more appropriate sources.”

### Medium priority

- Add implementation links for feature claims about the audit pipeline.
- Add direct statistical citations before publishing exact download counts.

---

## Notes

This demo report is illustrative. It shows the report structure and expected tone; it is not a benchmark result.
