#!/usr/bin/env python3
"""Standalone web UI for llm-output-audit.

The server is intentionally dependency-free: stdlib HTTP + Server-Sent Events.
It lets a user paste text, starts the v2 audit pipeline in a background process,
streams artifact/progress events, and renders the final revised text.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT / "web"
RUNS_DIR = ROOT / ".audit-web-runs"
PYTHON = sys.executable
AUDIT_SCRIPT = ROOT / "scripts" / "audit_v2.py"

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

ARTIFACT_STEPS = [
    ("article-profile.json", "profile", "分析文章结构"),
    ("actual-claims.json", "claims", "抽取可核查声明"),
    ("verification-plan.json", "plan", "生成证据检索计划"),
    ("actual-evidence.jsonl", "evidence", "收集证据"),
    ("actual-verdicts.json", "verdicts", "判定声明可信度"),
    ("actual-review-queue.json", "review", "生成人工复核队列"),
    ("actual-suggestions.json", "suggestions", "生成修改建议"),
    ("actual-patches.json", "patches", "生成保守修订补丁"),
    ("revised.md", "revised", "输出修订文本"),
    ("actual-report.md", "report", "生成审查报告"),
    ("actual-manifest.json", "manifest", "写入 artifact manifest"),
]


def safe_json_load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_jsonl(path: Path, limit: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[:limit]:
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def sanitize_text(value: Any, max_len: int = 1_000_000) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\x00", "")
    return text[:max_len]


def summarize_job(job: dict[str, Any]) -> dict[str, Any]:
    out_dir = Path(job["out_dir"])
    data: dict[str, Any] = {
        "job_id": job["job_id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "returncode": job.get("returncode"),
        "error": job.get("error"),
        "stderr_tail": job.get("stderr_tail", ""),
        "config": job.get("config", {}),
        "artifacts": {},
    }
    for file_name, key, _label in ARTIFACT_STEPS:
        path = out_dir / file_name
        if not path.exists():
            continue
        if path.suffix == ".json":
            data["artifacts"][key] = safe_json_load(path)
        elif path.suffix == ".jsonl":
            data["artifacts"][key] = read_jsonl(path)
        else:
            data["artifacts"][key] = path.read_text(encoding="utf-8", errors="ignore")
    return data


def run_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["started_at"] = time.time()
    run_dir = Path(job["run_dir"])
    out_dir = Path(job["out_dir"])
    article_path = run_dir / "input.md"
    article_path.write_text(job["text"], encoding="utf-8")
    cmd = [
        PYTHON,
        str(AUDIT_SCRIPT),
        "--file",
        str(article_path),
        "--output-dir",
        str(out_dir),
        "--claim-extractor",
        job["config"].get("claim_extractor", "hybrid"),
        "--evidence-mode",
        job["config"].get("evidence_mode", "missing"),
        "--max-claims",
        str(job["config"].get("max_claims", 16)),
        "--write-revision",
    ]
    if job["config"].get("post_audit_revision", "none") != "none":
        cmd.extend(["--post-audit-revision", job["config"].get("post_audit_revision", "none")])
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=int(job["config"].get("timeout", 900)))
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["returncode"] = proc.returncode
            job["stdout_tail"] = proc.stdout[-4000:]
            job["stderr_tail"] = proc.stderr[-4000:]
            job["finished_at"] = time.time()
            job["status"] = "done" if proc.returncode == 0 else "failed"
            if proc.returncode != 0:
                job["error"] = proc.stderr[-1000:] or proc.stdout[-1000:] or f"audit process exited {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["returncode"] = -1
            job["error"] = f"audit timed out after {exc.timeout}s"
            job["stderr_tail"] = sanitize_text(exc.stderr, 2000)
    except Exception as exc:
        with JOBS_LOCK:
            job = JOBS[job_id]
            job["status"] = "failed"
            job["finished_at"] = time.time()
            job["returncode"] = -1
            job["error"] = f"{type(exc).__name__}: {exc}"


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMOutputAuditWeb/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.serve_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path.startswith("/assets/"):
            rel = parsed.path.removeprefix("/assets/")
            path = (WEB_DIR / "assets" / rel).resolve()
            if not str(path).startswith(str((WEB_DIR / "assets").resolve())):
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            content_type = "text/plain"
            if path.suffix == ".css":
                content_type = "text/css; charset=utf-8"
            elif path.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
            self.serve_file(path, content_type)
            return
        if parsed.path.startswith("/api/jobs/") and parsed.path.endswith("/events"):
            job_id = parsed.path.split("/")[3]
            self.stream_events(job_id)
            return
        if parsed.path.startswith("/api/jobs/"):
            job_id = parsed.path.split("/")[3]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            self.send_json(summarize_job(job))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def serve_file(self, path: Path, content_type: str) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/audit":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self.send_json({"error": "invalid JSON body"}, 400)
            return
        text = sanitize_text(payload.get("text"), 500_000).strip()
        if len(text) < 20:
            self.send_json({"error": "请输入至少 20 个字符的待审查文本"}, 400)
            return
        job_id = uuid.uuid4().hex[:12]
        run_dir = RUNS_DIR / job_id
        out_dir = run_dir / "artifacts"
        run_dir.mkdir(parents=True, exist_ok=True)
        config = {
            "claim_extractor": payload.get("claim_extractor") if payload.get("claim_extractor") in {"rule", "llm", "hybrid"} else "hybrid",
            "evidence_mode": payload.get("evidence_mode") if payload.get("evidence_mode") in {"auto", "live", "missing"} else "missing",
            "max_claims": max(1, min(int(payload.get("max_claims") or 16), 80)),
            "post_audit_revision": payload.get("post_audit_revision") if payload.get("post_audit_revision") in {"none", "missing", "auto"} else "none",
            "timeout": max(60, min(int(payload.get("timeout") or 900), 1800)),
        }
        job = {
            "job_id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "run_dir": str(run_dir),
            "out_dir": str(out_dir),
            "text": text,
            "config": config,
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
        thread = threading.Thread(target=run_job, args=(job_id,), daemon=True)
        thread.start()
        self.send_json({"job_id": job_id, "status": "queued"}, 202)

    def stream_events(self, job_id: str) -> None:
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        seen: set[str] = set()
        last_status = None
        start = time.time()
        while True:
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                break
            status = job["status"]
            if status != last_status:
                self.write_sse("status", {"status": status, "job_id": job_id})
                last_status = status
            out_dir = Path(job["out_dir"])
            for file_name, key, label in ARTIFACT_STEPS:
                path = out_dir / file_name
                if path.exists() and file_name not in seen:
                    seen.add(file_name)
                    payload: dict[str, Any] = {"step": key, "label": label, "file": file_name}
                    if path.suffix == ".json":
                        payload["data"] = safe_json_load(path)
                    elif path.suffix == ".jsonl":
                        payload["data"] = read_jsonl(path, limit=30)
                    else:
                        payload["data"] = path.read_text(encoding="utf-8", errors="ignore")
                    self.write_sse("artifact", payload)
            if status in {"done", "failed"}:
                self.write_sse("complete", summarize_job(job))
                break
            if time.time() - start > 1900:
                self.write_sse("error", {"error": "event stream timeout"})
                break
            time.sleep(0.6)

    def write_sse(self, event: str, payload: Any) -> None:
        raw = f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
        self.wfile.write(raw)
        self.wfile.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the standalone llm-output-audit web UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--clean", action="store_true", help="Remove previous .audit-web-runs before starting")
    args = parser.parse_args()
    if args.clean and RUNS_DIR.exists():
        shutil.rmtree(RUNS_DIR)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"llm-output-audit web UI: http://{args.host}:{args.port}/", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
