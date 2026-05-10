#!/usr/bin/env python3
"""Install llm-output-audit adapters for different AI agents.

The repository itself remains the canonical source of truth. This installer adds
small agent-specific entrypoints (symlinks or marker-managed instruction blocks)
so Hermes, Claude Code, Codex, and generic coding agents can discover and use the
same CLI without copying the core implementation.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Iterable

SKILL_NAME = "llm-output-audit"
MARKER_NAME = "llm-output-audit"
START_MARKER = f"<!-- {MARKER_NAME}:start -->"
END_MARKER = f"<!-- {MARKER_NAME}:end -->"
SUPPORTED_AGENTS = {"hermes", "claude-code", "codex", "opencode", "gemini", "generic", "mcp"}
AGENTS_MD_AGENTS = {"codex", "opencode", "gemini", "generic"}


@dataclass
class PlannedAction:
    kind: str
    path: Path
    detail: str


class InstallError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_version(root: Path) -> str:
    skill = root / "SKILL.md"
    if not skill.exists():
        return "unknown"
    match = re.search(r"^version:\s*([^\n]+)", skill.read_text(encoding="utf-8"), re.M)
    return match.group(1).strip() if match else "unknown"


def template_vars(root: Path, agent: str, scope: str) -> dict[str, str]:
    script_path = root / "scripts" / "fact_check.py"
    return {
        "skill_name": SKILL_NAME,
        "version": read_version(root),
        "repo_path": str(root),
        "script_path": str(script_path),
        "agent": agent,
        "scope": scope,
        "default_command": (
            f"python3 {script_path} --file ARTICLE.md --output ARTICLE-audit.md "
            f"--mode draft --trace-log ARTICLE-audit-trace.jsonl"
        ),
    }


def render_template(root: Path, template_name: str, values: dict[str, str]) -> str:
    path = root / "templates" / template_name
    if not path.exists():
        raise InstallError(f"Missing template: {path}")
    return Template(path.read_text(encoding="utf-8")).safe_substitute(values)


def marker_block(content: str) -> str:
    content = content.strip() + "\n"
    return f"{START_MARKER}\n{content}{END_MARKER}\n"


def merge_marker_block(existing: str, content: str) -> str:
    block = marker_block(content)
    pattern = re.compile(
        rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}\n?",
        re.S,
    )
    if pattern.search(existing):
        return pattern.sub(block, existing)
    if existing and not existing.endswith("\n"):
        existing += "\n"
    sep = "\n" if existing.strip() else ""
    return f"{existing}{sep}{block}"


def write_marker_file(path: Path, content: str, dry_run: bool, force: bool, actions: list[PlannedAction]) -> None:
    path = path.expanduser()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text = merge_marker_block(existing, content)
    if path.exists() and existing == new_text:
        actions.append(PlannedAction("unchanged", path, "marker block already up to date"))
        return
    actions.append(PlannedAction("write", path, "insert/update marker-managed block"))
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_text, encoding="utf-8")


def write_file(path: Path, content: str, dry_run: bool, force: bool, actions: list[PlannedAction]) -> None:
    path = path.expanduser()
    if path.exists() and not force:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if existing == content:
            actions.append(PlannedAction("unchanged", path, "already up to date"))
            return
        raise InstallError(f"Refusing to overwrite existing file without --force: {path}")
    actions.append(PlannedAction("write", path, "write generated file"))
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def ensure_symlink_or_copy(src: Path, dst: Path, mode: str, dry_run: bool, force: bool, actions: list[PlannedAction]) -> None:
    src = src.expanduser().resolve()
    dst = dst.expanduser()
    if mode == "symlink":
        if dst.is_symlink() and dst.resolve() == src:
            actions.append(PlannedAction("unchanged", dst, f"symlink already points to {src}"))
            return
        if dst.exists() or dst.is_symlink():
            if not force:
                raise InstallError(f"Target exists; use --force to replace it: {dst}")
            actions.append(PlannedAction("replace", dst, "replace existing target with symlink"))
            if not dry_run:
                if dst.is_dir() and not dst.is_symlink():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
        actions.append(PlannedAction("symlink", dst, f"-> {src}"))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(src, target_is_directory=src.is_dir())
        return

    if mode == "copy":
        if dst.exists():
            if not force:
                raise InstallError(f"Target exists; use --force to replace copied directory: {dst}")
            actions.append(PlannedAction("replace", dst, "replace existing copied directory"))
            if not dry_run:
                if dst.is_dir():
                    shutil.rmtree(dst)
                else:
                    dst.unlink()
        actions.append(PlannedAction("copy", dst, f"copy from {src}"))
        if not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            ignore = shutil.ignore_patterns(".git", "__pycache__", "*.pyc", "*-trace.jsonl", "README-audit.md")
            shutil.copytree(src, dst, ignore=ignore)
        return

    raise InstallError(f"Unknown install mode: {mode}")


def default_target(agent: str, scope: str, root: Path) -> Path:
    home = Path.home()
    cwd = Path.cwd()
    if agent == "hermes":
        if scope == "project":
            return cwd / ".hermes" / "skills" / "research" / SKILL_NAME
        return home / ".hermes" / "skills" / "research" / SKILL_NAME
    if agent == "claude-code":
        if scope == "project":
            return cwd / ".claude" / "skills" / f"{SKILL_NAME}.md"
        return home / ".claude" / "skills" / f"{SKILL_NAME}.md"
    if agent in AGENTS_MD_AGENTS:
        if scope == "project":
            return cwd / "AGENTS.md"
        if agent == "codex":
            return home / ".codex" / "AGENTS.md"
        if agent == "gemini":
            return home / ".gemini" / "AGENTS.md"
        if agent == "opencode":
            return home / ".opencode" / "AGENTS.md"
        return home / ".config" / "agents" / "AGENTS.md"
    if agent == "mcp":
        if scope == "project":
            return cwd / "mcp" / "llm-output-audit.mcp.json"
        return home / ".config" / "llm-output-audit" / "mcp-server.json"
    raise InstallError(f"Unsupported agent: {agent}")


def install_hermes(args: argparse.Namespace, root: Path, actions: list[PlannedAction]) -> None:
    target = Path(args.target).expanduser() if args.target else default_target("hermes", args.scope, root)
    ensure_symlink_or_copy(root, target, args.mode, args.dry_run, args.force, actions)


def install_claude_code(args: argparse.Namespace, root: Path, actions: list[PlannedAction]) -> None:
    values = template_vars(root, "claude-code", args.scope)
    target = Path(args.target).expanduser() if args.target else default_target("claude-code", args.scope, root)
    skill_content = render_template(root, "claude-code-skill.md.tmpl", values)
    write_file(target, skill_content, args.dry_run, args.force, actions)

    if not args.no_project_instructions:
        claude_md = (Path.cwd() / "CLAUDE.md") if args.scope == "project" else (Path.home() / ".claude" / "CLAUDE.md")
        block = render_template(root, "CLAUDE.block.md.tmpl", values)
        write_marker_file(claude_md, block, args.dry_run, args.force, actions)


def install_agents_md(args: argparse.Namespace, root: Path, actions: list[PlannedAction]) -> None:
    agent = args.agent
    values = template_vars(root, agent, args.scope)
    target = Path(args.target).expanduser() if args.target else default_target(agent, args.scope, root)
    block = render_template(root, "AGENTS.block.md.tmpl", values)
    write_marker_file(target, block, args.dry_run, args.force, actions)


def install_mcp(args: argparse.Namespace, root: Path, actions: list[PlannedAction]) -> None:
    values = template_vars(root, "mcp", args.scope)
    target = Path(args.target).expanduser() if args.target else default_target("mcp", args.scope, root)
    content = render_template(root, "mcp-config.json.tmpl", values)
    write_file(target, content, args.dry_run, args.force, actions)


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Install llm-output-audit adapters for Hermes, Claude Code, Codex, MCP, and generic agents."
    )
    p.add_argument("--agent", required=True, choices=sorted(SUPPORTED_AGENTS), help="Target agent adapter to install")
    p.add_argument("--scope", choices=["user", "project"], default="user", help="Install globally for the user or into the current project")
    p.add_argument("--target", help="Override the default target path")
    p.add_argument("--mode", choices=["symlink", "copy"], default="symlink", help="Hermes install mode; other adapters write marker-managed files")
    p.add_argument("--force", action="store_true", help="Replace existing non-marker-managed files/targets when needed")
    p.add_argument("--dry-run", action="store_true", help="Print planned actions without writing")
    p.add_argument("--no-project-instructions", action="store_true", help="For claude-code, install only the skill file and skip CLAUDE.md marker block")
    return p.parse_args(list(argv))


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = repo_root()
    actions: list[PlannedAction] = []
    try:
        if args.agent == "hermes":
            install_hermes(args, root, actions)
        elif args.agent == "claude-code":
            install_claude_code(args, root, actions)
        elif args.agent == "mcp":
            install_mcp(args, root, actions)
        else:
            install_agents_md(args, root, actions)
    except InstallError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    prefix = "DRY-RUN " if args.dry_run else ""
    print(f"{prefix}install complete for agent={args.agent} scope={args.scope}")
    for action in actions:
        print(f"- {action.kind}: {action.path} ({action.detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
