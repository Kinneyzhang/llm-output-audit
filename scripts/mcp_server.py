#!/usr/bin/env python3
"""MCP stdio server for llm-output-audit.

This implementation intentionally avoids an SDK dependency. It speaks the MCP
JSON-RPC stdio framing protocol directly and exposes the audit CLI as tools.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

SERVER_NAME = "llm-output-audit"
SERVER_VERSION = "1.11.0"
PROTOCOL_VERSION = "2024-11-05"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def fact_check_script() -> Path:
    return repo_root() / "scripts" / "fact_check.py"


def audit_v2_script() -> Path:
    return repo_root() / "scripts" / "audit_v2.py"


def read_message() -> dict[str, Any] | None:
    """Read one MCP framed JSON-RPC message from stdin."""
    headers: dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("utf-8", errors="replace").partition(":")
        headers[key.strip().lower()] = value.strip()

    length_text = headers.get("content-length")
    if not length_text:
        return None
    body = sys.stdin.buffer.read(int(length_text))
    return json.loads(body.decode("utf-8"))


def write_message(payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("ascii"))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def json_text(data: Any) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(data, ensure_ascii=False, indent=2)}


def text_result(data: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [json_text(data)], "isError": is_error}


def tail(text: str, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def build_audit_command(args: dict[str, Any], file_path: Path, output_path: Path | None, trace_path: Path | None) -> list[str]:
    cmd = [sys.executable, str(fact_check_script()), "--file", str(file_path)]
    if output_path:
        cmd += ["--output", str(output_path)]
    mode = args.get("mode") or "draft"
    cmd += ["--mode", str(mode)]
    if args.get("workers") is not None:
        cmd += ["--workers", str(int(args["workers"]))]
    if args.get("source_workers") is not None:
        cmd += ["--source-workers", str(int(args["source_workers"]))]
    if coerce_bool(args.get("use_wiki")):
        cmd.append("--use-wiki")
        if args.get("wiki"):
            cmd += ["--wiki", str(args["wiki"])]
    if coerce_bool(args.get("dry_run")):
        cmd.append("--dry-run")
    if coerce_bool(args.get("skip_consistency")):
        cmd.append("--skip-consistency")
    if coerce_bool(args.get("force_consistency")):
        cmd.append("--force-consistency")
    if coerce_bool(args.get("no_fetch")):
        cmd.append("--no-fetch")
    if coerce_bool(args.get("llm_router")):
        cmd.append("--llm-router")
    if trace_path:
        cmd += ["--trace-log", str(trace_path)]
    return cmd


def run_audit(args: dict[str, Any], file_path: Path, output_path: Path | None, trace_path: Path | None) -> dict[str, Any]:
    timeout = int(args.get("timeout") or 600)
    cmd = build_audit_command(args, file_path, output_path, trace_path)
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    report_content = None
    if output_path and output_path.exists() and not coerce_bool(args.get("dry_run")):
        report_content = output_path.read_text(encoding="utf-8", errors="ignore")
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": cmd,
        "file": str(file_path),
        "output": str(output_path) if output_path else None,
        "trace_log": str(trace_path) if trace_path else None,
        "stdout_tail": tail(proc.stdout),
        "stderr_tail": tail(proc.stderr),
        "report_excerpt": report_content[:6000] if report_content else None,
    }


def tool_audit_file(args: dict[str, Any]) -> dict[str, Any]:
    file_value = args.get("file")
    if not file_value:
        return text_result({"ok": False, "error": "Missing required argument: file"}, is_error=True)
    file_path = Path(str(file_value)).expanduser().resolve()
    if not file_path.exists():
        return text_result({"ok": False, "error": f"File not found: {file_path}"}, is_error=True)
    output_path = Path(str(args["output"])).expanduser().resolve() if args.get("output") else file_path.with_name(file_path.stem + "-audit.md")
    trace_path = Path(str(args["trace_log"])).expanduser().resolve() if args.get("trace_log") else output_path.with_suffix(".trace.jsonl")
    try:
        data = run_audit(args, file_path, output_path, trace_path)
        return text_result(data, is_error=not data["ok"])
    except subprocess.TimeoutExpired as exc:
        return text_result({"ok": False, "error": f"Audit timed out after {exc.timeout}s", "file": str(file_path)}, is_error=True)
    except Exception as exc:  # noqa: BLE001 - tool boundary should return structured errors
        return text_result({"ok": False, "error": f"{type(exc).__name__}: {exc}", "file": str(file_path)}, is_error=True)


def tool_audit_text(args: dict[str, Any]) -> dict[str, Any]:
    text = str(args.get("text") or "")
    if not text.strip():
        return text_result({"ok": False, "error": "Missing required argument: text"}, is_error=True)
    title = str(args.get("title") or "mcp-audit-input").strip() or "mcp-audit-input"
    safe_title = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in title.lower()).strip("-") or "mcp-audit-input"
    output_dir = Path(str(args["output_dir"])).expanduser().resolve() if args.get("output_dir") else Path(tempfile.mkdtemp(prefix="llm-output-audit-mcp-"))
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{safe_title}.md"
    output_path = output_dir / f"{safe_title}-audit.md"
    trace_path = output_dir / f"{safe_title}-audit.trace.jsonl"
    file_path.write_text(text, encoding="utf-8")
    try:
        data = run_audit(args, file_path, output_path, trace_path)
        data["input_created"] = str(file_path)
        return text_result(data, is_error=not data["ok"])
    except subprocess.TimeoutExpired as exc:
        return text_result({"ok": False, "error": f"Audit timed out after {exc.timeout}s", "file": str(file_path)}, is_error=True)
    except Exception as exc:  # noqa: BLE001
        return text_result({"ok": False, "error": f"{type(exc).__name__}: {exc}", "file": str(file_path)}, is_error=True)


def tool_summarize_trace(args: dict[str, Any]) -> dict[str, Any]:
    trace_value = args.get("trace_log")
    if not trace_value:
        return text_result({"ok": False, "error": "Missing required argument: trace_log"}, is_error=True)
    trace_path = Path(str(trace_value)).expanduser().resolve()
    if not trace_path.exists():
        return text_result({"ok": False, "error": f"Trace log not found: {trace_path}"}, is_error=True)
    max_events = int(args.get("max_events") or 20)
    counter: Counter[str] = Counter()
    last_events: list[dict[str, Any]] = []
    deterministic = []
    ratings = []
    with trace_path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("event", "unknown")
            counter[name] += 1
            if name == "deterministic_override":
                deterministic.append(event)
            if name == "rating_result":
                ratings.append(event)
            last_events.append(event)
            if len(last_events) > max_events:
                last_events.pop(0)
    data = {
        "ok": True,
        "trace_log": str(trace_path),
        "event_counts": dict(counter),
        "deterministic_override_count": len(deterministic),
        "rating_result_count": len(ratings),
        "last_events": last_events,
    }
    return text_result(data)


def tool_install_snippet(args: dict[str, Any]) -> dict[str, Any]:
    command = f"python3 {repo_root() / 'scripts' / 'mcp_server.py'}"
    server_name = str(args.get("server_name") or SERVER_NAME)
    data = {
        "ok": True,
        "hermes_config_yaml": {
            "mcp_servers": {
                server_name: {
                    "command": "python3",
                    "args": [str(repo_root() / "scripts" / "mcp_server.py")],
                    "timeout": 600,
                    "connect_timeout": 30,
                }
            }
        },
        "claude_mcp_command": f"claude mcp add {server_name} -- {command}",
        "stdio_command": command,
    }
    return text_result(data)


def run_audit_v2_file(args: dict[str, Any], file_path: Path, output_dir: Path) -> dict[str, Any]:
    timeout = int(args.get("timeout") or 600)
    cmd = [sys.executable, str(audit_v2_script()), "--file", str(file_path), "--output-dir", str(output_dir)]
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    manifest_path = output_dir / "actual-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None
    return {
        "ok": proc.returncode == 0,
        "exit_code": proc.returncode,
        "command": cmd,
        "file": str(file_path),
        "output_dir": str(output_dir),
        "manifest": manifest,
        "stdout_tail": tail(proc.stdout),
        "stderr_tail": tail(proc.stderr),
    }


def tool_audit_file_v2(args: dict[str, Any]) -> dict[str, Any]:
    file_value = args.get("file")
    if not file_value:
        return text_result({"ok": False, "error": "Missing required argument: file"}, is_error=True)
    file_path = Path(str(file_value)).expanduser().resolve()
    if not file_path.exists():
        return text_result({"ok": False, "error": f"File not found: {file_path}"}, is_error=True)
    output_dir = Path(str(args["output_dir"])).expanduser().resolve() if args.get("output_dir") else file_path.with_name(file_path.stem + "-v2-artifacts")
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        data = run_audit_v2_file(args, file_path, output_dir)
        return text_result(data, is_error=not data["ok"])
    except subprocess.TimeoutExpired as exc:
        return text_result({"ok": False, "error": f"v2 audit timed out after {exc.timeout}s", "file": str(file_path)}, is_error=True)
    except Exception as exc:  # noqa: BLE001
        return text_result({"ok": False, "error": f"{type(exc).__name__}: {exc}", "file": str(file_path)}, is_error=True)


def tool_summarize_artifacts(args: dict[str, Any]) -> dict[str, Any]:
    dir_value = args.get("artifact_dir")
    if not dir_value:
        return text_result({"ok": False, "error": "Missing required argument: artifact_dir"}, is_error=True)
    artifact_dir = Path(str(dir_value)).expanduser().resolve()
    if not artifact_dir.exists():
        return text_result({"ok": False, "error": f"Artifact dir not found: {artifact_dir}"}, is_error=True)
    files = {
        "claims": artifact_dir / "actual-claims.json",
        "evidence": artifact_dir / "actual-evidence.jsonl",
        "verdicts": artifact_dir / "actual-verdicts.json",
        "review_queue": artifact_dir / "actual-review-queue.json",
        "suggestions": artifact_dir / "actual-suggestions.json",
        "manifest": artifact_dir / "actual-manifest.json",
    }
    counts = {}
    for name, path in files.items():
        if not path.exists():
            counts[name] = None
        elif path.suffix == ".jsonl":
            counts[name] = sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
        elif name == "manifest":
            counts[name] = 1
        else:
            data = json.loads(path.read_text(encoding="utf-8"))
            counts[name] = len(data) if isinstance(data, list) else 1
    verdict_counts = {}
    if files["verdicts"].exists():
        verdict_counts = dict(Counter(item.get("truth_verdict", "unknown") for item in json.loads(files["verdicts"].read_text(encoding="utf-8"))))
    queue_counts = {}
    if files["review_queue"].exists():
        queue_counts = dict(Counter(item.get("queue", "unknown") for item in json.loads(files["review_queue"].read_text(encoding="utf-8"))))
    return text_result({"ok": True, "artifact_dir": str(artifact_dir), "counts": counts, "verdict_counts": verdict_counts, "queue_counts": queue_counts})


def tools_list() -> list[dict[str, Any]]:
    common = {
        "mode": {"type": "string", "enum": ["auto", "fast", "spot", "draft", "full"], "default": "draft"},
        "workers": {"type": "integer", "minimum": 1, "maximum": 32, "default": 6},
        "source_workers": {"type": "integer", "minimum": 1, "maximum": 16, "default": 4},
        "use_wiki": {"type": "boolean", "default": False},
        "wiki": {"type": "string"},
        "trace_log": {"type": "string"},
        "dry_run": {"type": "boolean", "default": False},
        "skip_consistency": {"type": "boolean", "default": False},
        "force_consistency": {"type": "boolean", "default": False},
        "no_fetch": {"type": "boolean", "default": False},
        "llm_router": {"type": "boolean", "default": False},
        "timeout": {"type": "integer", "minimum": 10, "maximum": 7200, "default": 600},
    }
    return [
        {
            "name": "audit_file",
            "description": "Audit a local Markdown/text file and write an audit report plus JSONL trace log.",
            "inputSchema": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "output": {"type": "string"}, **common},
                "required": ["file"],
            },
        },
        {
            "name": "audit_text",
            "description": "Audit provided text by writing it to a temporary Markdown file, then running the audit pipeline.",
            "inputSchema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "title": {"type": "string"}, "output_dir": {"type": "string"}, **common},
                "required": ["text"],
            },
        },
        {
            "name": "audit_file_v2",
            "description": "Run the deterministic v2 artifact pipeline on a local Markdown/text file and write normalized actual-* artifacts.",
            "inputSchema": {
                "type": "object",
                "properties": {"file": {"type": "string"}, "output_dir": {"type": "string"}, "timeout": {"type": "integer", "minimum": 10, "maximum": 7200, "default": 600}},
                "required": ["file"],
            },
        },
        {
            "name": "summarize_artifacts",
            "description": "Summarize a v2 artifact directory containing actual-claims/evidence/verdicts/review/suggestions files.",
            "inputSchema": {
                "type": "object",
                "properties": {"artifact_dir": {"type": "string"}},
                "required": ["artifact_dir"],
            },
        },
        {
            "name": "summarize_trace",
            "description": "Summarize a llm-output-audit JSONL trace log for debugging the audit process.",
            "inputSchema": {
                "type": "object",
                "properties": {"trace_log": {"type": "string"}, "max_events": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}},
                "required": ["trace_log"],
            },
        },
        {
            "name": "install_snippet",
            "description": "Return MCP client configuration snippets for connecting to this server.",
            "inputSchema": {
                "type": "object",
                "properties": {"server_name": {"type": "string", "default": SERVER_NAME}},
            },
        },
    ]


def handle_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "audit_file":
        return tool_audit_file(args)
    if name == "audit_text":
        return tool_audit_text(args)
    if name == "audit_file_v2":
        return tool_audit_file_v2(args)
    if name == "summarize_artifacts":
        return tool_summarize_artifacts(args)
    if name == "summarize_trace":
        return tool_summarize_trace(args)
    if name == "install_snippet":
        return tool_install_snippet(args)
    return text_result({"ok": False, "error": f"Unknown tool: {name}"}, is_error=True)


def handle_request(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    msg_id = message.get("id")
    params = message.get("params") or {}

    if method == "notifications/initialized":
        return None
    if msg_id is None:
        return None

    try:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            }
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list()}}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            return {"jsonrpc": "2.0", "id": msg_id, "result": handle_call(name, arguments)}
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}
    except Exception as exc:  # noqa: BLE001 - MCP server boundary
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}


def serve() -> None:
    while True:
        message = read_message()
        if message is None:
            break
        response = handle_request(message)
        if response is not None:
            write_message(response)


def main() -> int:
    parser = argparse.ArgumentParser(description="MCP stdio server for llm-output-audit")
    parser.add_argument("--version", action="store_true", help="Print server version and exit")
    args = parser.parse_args()
    if args.version:
        print(f"{SERVER_NAME} {SERVER_VERSION}")
        return 0
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
