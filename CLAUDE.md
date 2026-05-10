# CLAUDE.md

This repository contains `llm-output-audit`, a CLI-backed skill for auditing long-form LLM output.

## Use this for

- factual accuracy audits
- hallucination checks
- source quality review
- stale knowledge review
- internal consistency review
- actionable edit suggestions for research reports, technical comparisons, usage guides, and durable docs

## Canonical command

```bash
python3 scripts/fact_check.py \
  --file ARTICLE.md \
  --output ARTICLE-audit.md \
  --mode draft \
  --workers 6 \
  --source-workers 4 \
  --trace-log ARTICLE-audit-trace.jsonl
```

Use `--mode full` for public or high-stakes writing.

## Rules for Claude Code

1. Existing user-owned files are report-first; do not rewrite them unless explicitly asked.
2. Agent-generated durable drafts should be audited before final delivery.
3. Keep `--trace-log` enabled for non-trivial audits.
4. Prefer structured API evidence for source-owned facts such as GitHub stars and package versions.
5. If asked to install this skill into Claude Code, run `python3 scripts/install_agent_skill.py --agent claude-code --scope user` or use `--scope project` for project-local installation.
