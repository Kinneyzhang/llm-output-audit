# AGENTS.md

This repository provides `llm-output-audit`, a portable audit toolkit for long-form LLM output. It can be used as a Python CLI, stdio MCP server, or lightweight adapter for Hermes, Claude Code, Codex, OpenCode, Gemini, and generic coding agents.

## When an agent should use this repo

Use this tool when asked to:

- audit or fact-check long-form LLM-generated content
- check hallucinations or stale knowledge
- review source quality and evidence strength
- verify research reports, technical comparisons, usage guides, README/blog drafts, or durable notes
- produce actionable edit suggestions for factual issues

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

Use `--mode full` for public/high-stakes writing. Add `--use-wiki --wiki /path/to/llm-wiki` only when the user has a local wiki configured.

## Rules

1. Do not modify user-owned source files unless explicitly asked to apply fixes.
2. For agent-generated durable drafts, audit before finalizing and apply safe corrections.
3. Always inspect the generated audit report; inspect the trace log when debugging the auditor itself.
4. Source-owned structured facts such as GitHub stars, package versions, release dates, and repository status should prefer API evidence over generic web snippets.
5. Keep generated reports and trace logs near the source article when practical.

## Multi-agent installation

Install adapter files for a specific agent with:

```bash
python3 scripts/install_agent_skill.py --agent codex --scope project --dry-run
python3 scripts/install_agent_skill.py --agent claude-code --scope user
python3 scripts/install_agent_skill.py --agent hermes --scope user
```

Supported adapters: `hermes`, `claude-code`, `codex`, `opencode`, `gemini`, `generic`, `mcp`.

## MCP server

This repository can also run as a stdio MCP server:

```bash
python3 scripts/mcp_server.py
```

Tools exposed: `audit_file`, `audit_text`, `audit_file_v2`, `summarize_artifacts`, `summarize_trace`, and `install_snippet`.

Generate an MCP config snippet:

```bash
python3 scripts/install_agent_skill.py --agent mcp --scope project
```
