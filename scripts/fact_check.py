#!/usr/bin/env python3
"""
fact_check.py — LLM output audit pipeline for long-form articles and reports.

v6 pipeline:
  0. Internal consistency check (no network)
  1. Extract atomic verifiable claims
  2. Select audit mode: fast / spot / draft / full / auto
  3. Route each selected claim to best evidence sources (Source Router)
  4. Query routed sources in parallel: Tavily/DDG, GitHub, Wikipedia, arXiv, Semantic Scholar, PyPI, npm
     Optional: LLM Wiki only when --use-wiki is enabled and path exists
  5. Fetch generic web URLs only; skip fetch for structured API evidence
  6. Rate each claim with evidence scoring + edit suggestion
  7. For ❌ claims: conditional adversarial second pass to reduce false positives
  8. Output structured report

Usage:
    python3 fact_check.py --file article.md [--output report.md]
    python3 fact_check.py --file article.md --dry-run

Environment:
    DEEPSEEK_API_KEY or OPENAI_API_KEY   — LLM for extraction + rating
    TAVILY_API_KEY                        — high-quality web search, recommended
    FACT_CHECK_BASE_URL                   — optional OpenAI-compatible endpoint override
    FACT_CHECK_MODEL                      — optional model name override
    DGX_API_KEY                           — optional key for DGX endpoint
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


# ── Load ~/.hermes/.env ───────────────────────────────────────────────────────
def load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if env_path.exists():
        for line in env_path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()


# ── Helpers ──────────────────────────────────────────────────────────────────
def truncate(text: str, n: int = 500) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


def request_json(method: str, url: str, *, params=None, json_body=None, timeout: int = 15) -> dict | list | None:
    try:
        import requests
        headers = {"User-Agent": "Hermes-LLMOutputAudit/6.0"}
        resp = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
        if not resp.ok:
            return None
        return resp.json()
    except Exception:
        return None


STRUCTURED_SOURCES = {"github", "github_releases", "pypi", "npm", "npm_downloads", "arxiv", "semantic_scholar", "llm_wiki"}
GENERIC_WEB_SOURCES = {"tavily_web", "duckduckgo", "wikipedia"}

SOURCE_QUALITY = {
    "github": {"authority": 0.95, "directness": 0.95, "freshness": 0.90},
    "github_releases": {"authority": 0.98, "directness": 0.98, "freshness": 0.95},
    "npm": {"authority": 0.95, "directness": 0.95, "freshness": 0.90},
    "npm_downloads": {"authority": 0.95, "directness": 1.00, "freshness": 0.95},
    "pypi": {"authority": 0.95, "directness": 0.95, "freshness": 0.90},
    "arxiv": {"authority": 0.92, "directness": 0.90, "freshness": 0.85},
    "semantic_scholar": {"authority": 0.90, "directness": 0.90, "freshness": 0.85},
    "llm_wiki": {"authority": 0.88, "directness": 0.75, "freshness": 0.75},
    "wikipedia": {"authority": 0.70, "directness": 0.70, "freshness": 0.60},
    "tavily_web": {"authority": 0.55, "directness": 0.60, "freshness": 0.80},
    "duckduckgo": {"authority": 0.45, "directness": 0.55, "freshness": 0.65},
}

def source_quality(source: str) -> dict:
    q = SOURCE_QUALITY.get(source, {"authority": 0.50, "directness": 0.50, "freshness": 0.50})
    score = round(q["authority"] * q["directness"] * q["freshness"], 3)
    return {**q, "score": score, "structured": source in STRUCTURED_SOURCES}

def result(source: str, title: str, url: str, snippet: str, confidence: str = "medium", structured_data: dict | None = None) -> dict:
    q = source_quality(source)
    return {
        "source": source,
        "title": title or "",
        "url": url or "",
        "snippet": truncate(snippet, 700),
        "confidence": confidence,
        "authority": q["authority"],
        "directness": q["directness"],
        "freshness": q["freshness"],
        "evidence_score": q["score"],
        "structured": q["structured"],
        "structured_data": structured_data or {},
    }


def format_results(results: list[dict]) -> str:
    if not results:
        return "No evidence results found."
    chunks = []
    for r in results:
        chunks.append(
            f"Source: {r.get('source', '')} ({r.get('confidence', 'medium')}, "
            f"score={r.get('evidence_score', '')}, structured={r.get('structured', False)})\n"
            f"Title: {r.get('title', '')}\n"
            f"URL: {r.get('url', '')}\n"
            f"Structured data: {json.dumps(r.get('structured_data', {}), ensure_ascii=False)}\n"
            f"Snippet: {r.get('snippet', '')}"
        )
    return "\n\n".join(chunks)


# ── LLM client ────────────────────────────────────────────────────────────────
def llm_call(system: str, user: str, model: str | None = None) -> str:
    try:
        import requests
    except ImportError:
        sys.exit("pip install requests")

    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "")
    openai_key = os.environ.get("OPENAI_API_KEY", "")
    custom_base = os.environ.get("FACT_CHECK_BASE_URL") or ""

    if deepseek_key and not custom_base:
        api_key = deepseek_key
        base_url = "https://api.deepseek.com/v1"
        default_model = "deepseek-chat"
    elif custom_base:
        api_key = os.environ.get("DGX_API_KEY") or openai_key or deepseek_key or ""
        base_url = custom_base.rstrip("/")
        default_model = os.environ.get("FAST_LLM", "").replace("openai:", "") or "gpt-4o-mini"
    else:
        api_key = openai_key
        base_url = "https://api.openai.com/v1"
        default_model = "gpt-4o-mini"

    if not api_key:
        sys.exit("Missing LLM API key: set DEEPSEEK_API_KEY or OPENAI_API_KEY")

    model = model or os.environ.get("FACT_CHECK_MODEL", default_model)
    resp = requests.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()



# ── Audit modes ───────────────────────────────────────────────────────────────
AUDIT_MODES = {
    "fast": {
        "max_claims": 0,
        "max_sources": 1,
        "consistency": False,
        "adversarial": False,
        "llm_router": False,
        "description": "No full audit; use only for ordinary low-risk chat answers.",
    },
    "spot": {
        "max_claims": 3,
        "max_sources": 2,
        "consistency": False,
        "adversarial": False,
        "llm_router": False,
        "description": "High-risk short factual answer; verify only the most important claims.",
    },
    "draft": {
        "max_claims": 12,
        "max_sources": 3,
        "consistency": "risk",
        "adversarial": "conditional",
        "llm_router": False,
        "description": "Durable draft audit; balance speed and accuracy for BuJo/LLM Wiki/research drafts.",
    },
    "full": {
        "max_claims": 50,
        "max_sources": 5,
        "consistency": True,
        "adversarial": True,
        "llm_router": True,
        "description": "Publication-grade audit; deepest and slowest.",
    },
}


def infer_audit_mode(article: str, claims: list[dict]) -> str:
    """Choose a default mode when --mode=auto."""
    risk, _ = consistency_risk(article, claims)
    n = len(claims)
    if n <= 3 and risk < 3:
        return "spot"
    if n >= 18 or risk >= 6 or len(article) > 6000:
        return "full"
    return "draft"


def claim_importance(claim: dict) -> tuple[int, list[str]]:
    """Heuristic importance score for speed/accuracy tradeoff."""
    ctype = claim.get("type", "")
    text = claim.get("text", "")
    lower = text.lower()
    score = 0
    reasons = []

    if ctype in {"DATE", "NUMBER", "STATUS", "ATTR"}:
        score += 2
        reasons.append(f"type:{ctype}")
    if ctype == "CAUSAL":
        score += 1
        reasons.append("causal")
    if any(k in lower for k in ["latest", "current", "deprecated", "merged", "release", "version", "download", "stars", "citations", "no longer"]):
        score += 2
        reasons.append("high-risk current/status keyword")
    if any(k in lower for k in ["最新", "当前", "废弃", "并入", "发布", "版本", "下载", "引用", "不再", "仍然"]):
        score += 2
        reasons.append("high-risk cn keyword")
    if re.search(r"\b\d[\d,\.]*\s*(k|m|b|万|亿|%|percent|million|billion)?\b", lower):
        score += 2
        reasons.append("numeric claim")
    if "github.com/" in lower or "npm" in lower or "pypi" in lower or "arxiv" in lower:
        score += 1
        reasons.append("specialized-source claim")
    return score, reasons


def select_claims_for_mode(claims: list[dict], mode: str) -> list[dict]:
    cfg = AUDIT_MODES[mode]
    max_claims = cfg["max_claims"]
    if max_claims <= 0:
        return []
    annotated = []
    for i, claim in enumerate(claims):
        score, reasons = claim_importance(claim)
        c = dict(claim)
        c["importance_score"] = score
        c["importance_reasons"] = reasons
        c["original_index"] = i
        annotated.append(c)
    annotated.sort(key=lambda c: (c["importance_score"], -c["original_index"]), reverse=True)
    selected = annotated[:max_claims]
    selected.sort(key=lambda c: c["original_index"])
    return selected

# ── Source Registry + Router ─────────────────────────────────────────────────
SOURCE_REGISTRY = {
    "tavily_web": {
        "strengths": ["latest news", "official announcements", "general web", "status changes"],
        "claim_types": ["DATE", "NUMBER", "EVENT", "ATTR", "STATUS", "CAUSAL"],
        "keywords": ["announced", "released", "current", "status", "news", "blog", "发布", "宣布", "现状"],
    },
    "github": {
        "strengths": ["open-source repositories", "releases", "stars", "maintainers", "software dates"],
        "claim_types": ["DATE", "NUMBER", "ATTR", "STATUS"],
        "keywords": ["github", "repo", "repository", "release", "stars", "开源", "仓库", "版本"],
    },
    "wikipedia": {
        "strengths": ["organizations", "people", "historical facts", "standards background"],
        "claim_types": ["DATE", "EVENT", "ATTR", "NUMBER"],
        "keywords": ["company", "organization", "founded", "person", "standard", "protocol", "公司", "组织", "成立", "创始", "协议"],
    },
    "arxiv": {
        "strengths": ["paper metadata", "authors", "submission dates", "research claims"],
        "claim_types": ["DATE", "ATTR", "CAUSAL"],
        "keywords": ["arxiv", "paper", "论文", "研究", "发表", "preprint"],
    },
    "semantic_scholar": {
        "strengths": ["paper citations", "academic influence", "authors", "publication venue"],
        "claim_types": ["NUMBER", "ATTR", "CAUSAL"],
        "keywords": ["citation", "citations", "cited", "引用", "论文", "研究"],
    },
    "pypi": {
        "strengths": ["Python package versions", "release dates", "package metadata"],
        "claim_types": ["DATE", "NUMBER", "ATTR", "STATUS"],
        "keywords": ["python", "pip", "pypi", "package", "库", "包"],
    },
    "npm": {
        "strengths": ["JavaScript package versions", "npm downloads", "release dates"],
        "claim_types": ["DATE", "NUMBER", "STATUS"],
        "keywords": ["npm", "node", "javascript", "typescript", "downloads", "下载"],
    },
    "llm_wiki": {
        "strengths": ["local curated knowledge", "confirmed personal wiki pages", "previously verified facts"],
        "claim_types": ["DATE", "NUMBER", "EVENT", "ATTR", "STATUS", "CAUSAL"],
        "keywords": [""],
    },
}


def rule_route_sources(claim_type: str, text: str, use_wiki: bool = False) -> list[str]:
    """Fast deterministic router. LLM Wiki is optional, never assumed."""
    lower = text.lower()
    scores: dict[str, int] = {}
    for name, meta in SOURCE_REGISTRY.items():
        score = 0
        if claim_type in meta["claim_types"]:
            score += 2
        for kw in meta["keywords"]:
            if kw and kw.lower() in lower:
                score += 3
        scores[name] = score

    # package-like names / explicit URLs
    if re.search(r"github\.com/[\w.-]+/[\w.-]+", lower):
        scores["github"] += 6
    if re.search(r"(?:^|\s)(?:@?[a-z0-9_.-]+/[a-z0-9_.-]+|[a-z0-9_.-]+)(?:\s|$)", lower) and any(k in lower for k in ["npm", "package", "下载", "downloads"]):
        scores["npm"] += 4
    if any(k in lower for k in ["pip install", "pypi", "python package"]):
        scores["pypi"] += 5
    if any(k in lower for k in ["paper", "arxiv", "论文", "citation", "引用"]):
        scores["arxiv"] += 4
        scores["semantic_scholar"] += 4

    ordered = [name for name, score in sorted(scores.items(), key=lambda x: x[1], reverse=True) if score > 0]
    if not use_wiki:
        ordered = [name for name in ordered if name != "llm_wiki"]
    elif "llm_wiki" not in ordered:
        ordered.append("llm_wiki")
    if "tavily_web" not in ordered:
        ordered.append("tavily_web")
    return ordered[:4]


ROUTER_SYSTEM = textwrap.dedent("""
    You route factual claims to the best verification sources.
    Return ONLY a JSON array of 2-4 source names from this list:
    tavily_web, github, wikipedia, arxiv, semantic_scholar, pypi, npm, llm_wiki.

    Choose sources by where the primary evidence would be most authoritative.
    Prefer official/specialized sources over generic web search.
""").strip()


def llm_route_sources(claim_type: str, text: str, rule_sources: list[str], use_wiki: bool = False) -> list[str]:
    """LLM router only used when deterministic routing is weak/ambiguous."""
    prompt = f"""Claim type: {claim_type}
Claim: {text}
Rule router suggested: {rule_sources}

Source registry:
{json.dumps(SOURCE_REGISTRY, ensure_ascii=False, indent=2)}"""
    try:
        raw = llm_call(ROUTER_SYSTEM, prompt)
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        picked = json.loads(m.group()) if m else []
        valid = [s for s in picked if s in SOURCE_REGISTRY]
        if valid:
            merged = []
            allowed = set(SOURCE_REGISTRY) if use_wiki else set(SOURCE_REGISTRY) - {"llm_wiki"}
            for s in valid + rule_sources + (["llm_wiki"] if use_wiki else []) + ["tavily_web"]:
                if s in allowed and s not in merged:
                    merged.append(s)
            return merged[:4]
    except Exception:
        pass
    return rule_sources


def route_sources(claim_type: str, text: str, use_llm_router: bool = False, use_wiki: bool = False, max_sources: int = 4) -> list[str]:
    rule_sources = rule_route_sources(claim_type, text, use_wiki=use_wiki)
    # Rule router is usually enough; LLM router is optional for ambiguous claims.
    if use_llm_router and len(rule_sources) < 3:
        return llm_route_sources(claim_type, text, rule_sources, use_wiki=use_wiki)[:max_sources]
    return rule_sources[:max_sources]


# ── Evidence source implementations ──────────────────────────────────────────
def web_search(query: str, n: int = 3) -> list[dict]:
    """Tavily → DuckDuckGo fallback."""
    import requests
    tavily_key = os.environ.get("TAVILY_API_KEY", "")
    if tavily_key:
        try:
            resp = requests.post(
                "https://api.tavily.com/search",
                json={"api_key": tavily_key, "query": query, "max_results": n},
                timeout=15,
            )
            if resp.ok:
                return [
                    result("tavily_web", r.get("title", ""), r.get("url", ""), r.get("content", ""), "medium")
                    for r in resp.json().get("results", [])
                ]
        except Exception:
            pass

    data = request_json("GET", "https://api.duckduckgo.com/", params={"q": query, "format": "json", "no_redirect": 1, "no_html": 1}, timeout=10)
    if isinstance(data, dict):
        results = []
        if data.get("AbstractText"):
            results.append(result("duckduckgo", data.get("Heading", ""), data.get("AbstractURL", ""), data["AbstractText"], "low"))
        for r in data.get("RelatedTopics", [])[:n]:
            if isinstance(r, dict) and r.get("Text"):
                results.append(result("duckduckgo", r["Text"][:80], r.get("FirstURL", ""), r["Text"], "low"))
        return results[:n]
    return []


def source_tavily_web(text: str, claim_type: str) -> list[dict]:
    queries = {
        "DATE": f"{text} official announcement date release date",
        "NUMBER": f"{text} official statistics downloads count",
        "EVENT": f"{text} confirmed official announcement",
        "ATTR": f"{text} created by announced by developed by official",
        "STATUS": f"{text} current status 2026 official",
        "CAUSAL": text,
    }
    return web_search(queries.get(claim_type, text), n=3)


def source_github(text: str, claim_type: str) -> list[dict]:
    # Direct repo URL is best.
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)", text, re.I)
    repos = []
    if m:
        repos.append(m.group(1).strip("/"))
    else:
        q = re.sub(r"[^\w\s./-]", " ", text)[:160]
        data = request_json("GET", "https://api.github.com/search/repositories", params={"q": q, "per_page": 3}, timeout=15)
        if isinstance(data, dict):
            repos.extend([item.get("full_name") for item in data.get("items", []) if item.get("full_name")])

    out = []
    for full_name in repos[:3]:
        repo = request_json("GET", f"https://api.github.com/repos/{full_name}", timeout=15)
        if isinstance(repo, dict) and repo.get("full_name"):
            snippet = (
                f"Repo {repo.get('full_name')} has {repo.get('stargazers_count')} stars, "
                f"created_at={repo.get('created_at')}, pushed_at={repo.get('pushed_at')}, "
                f"description={repo.get('description')}"
            )
            out.append(result("github", repo.get("full_name", ""), repo.get("html_url", ""), snippet, "high", {"repo": repo.get("full_name"), "stars": repo.get("stargazers_count"), "created_at": repo.get("created_at"), "pushed_at": repo.get("pushed_at"), "license": (repo.get("license") or {}).get("spdx_id")}))
            rel = request_json("GET", f"https://api.github.com/repos/{full_name}/releases/latest", timeout=15)
            if isinstance(rel, dict) and rel.get("html_url"):
                rs = f"Latest release {rel.get('tag_name')} published_at={rel.get('published_at')} name={rel.get('name')}"
                out.append(result("github_releases", f"{full_name} latest release", rel.get("html_url", ""), rs, "high", {"repo": full_name, "latest_release": rel.get("tag_name"), "published_at": rel.get("published_at")}))
    return out[:4]


def entity_candidates(text: str) -> list[str]:
    """Extract likely entity/package/query anchors from a claim."""
    aliases = {
        "next.js": "Next.js",
        "nextjs": "Next.js",
        "react": "React",
        "vue": "Vue.js",
        "typescript": "TypeScript",
        "pytorch": "PyTorch",
        "tensorflow": "TensorFlow",
        "openai": "OpenAI",
        "anthropic": "Anthropic",
    }
    lower = text.lower()
    out = []
    for k, v in aliases.items():
        if k in lower:
            out.append(v)
    # Acronyms and capitalized multi-word entities.
    out.extend(re.findall(r"\b[A-Z][A-Za-z0-9.+-]*(?:\s+[A-Z][A-Za-z0-9.+-]*){0,3}\b", text))
    # GitHub repo path.
    out.extend(re.findall(r"github\.com/([\w.-]+/[\w.-]+)", text, re.I))
    seen = []
    stop = {"The", "This", "That", "A", "An", "In", "On", "As"}
    for x in out:
        x = x.strip(" .,;:()[]{}")
        if x and x not in stop and x not in seen:
            seen.append(x)
    return seen[:5]


def source_wikipedia(text: str, claim_type: str) -> list[dict]:
    queries = entity_candidates(text) or [text[:80]]
    out = []
    for q in queries[:3]:
        data = request_json("GET", "https://en.wikipedia.org/w/api.php", params={
            "action": "opensearch", "search": q, "limit": 2, "namespace": 0, "format": "json"
        }, timeout=10)
        if isinstance(data, list) and len(data) >= 4:
            titles = data[1]
            snippets = data[2]
            urls = data[3]
            for title, snip, url in zip(titles, snippets, urls):
                out.append(result("wikipedia", title, url, snip, "medium"))
    return out[:4]


def source_arxiv(text: str, claim_type: str) -> list[dict]:
    try:
        import requests
        query = urllib.parse.quote(text[:160])
        url = f"http://export.arxiv.org/api/query?search_query=all:{query}&start=0&max_results=3"
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Hermes-LLMOutputAudit/6.0"})
        if not resp.ok:
            return []
        entries = re.findall(r"<entry>(.*?)</entry>", resp.text, flags=re.S)
        out = []
        for e in entries:
            title = re.sub(r"\s+", " ", re.search(r"<title>(.*?)</title>", e, re.S).group(1)).strip() if re.search(r"<title>(.*?)</title>", e, re.S) else ""
            link = re.search(r'<id>(.*?)</id>', e, re.S).group(1).strip() if re.search(r'<id>(.*?)</id>', e, re.S) else ""
            published = re.search(r"<published>(.*?)</published>", e, re.S).group(1).strip() if re.search(r"<published>(.*?)</published>", e, re.S) else ""
            summary = re.sub(r"\s+", " ", re.search(r"<summary>(.*?)</summary>", e, re.S).group(1)).strip() if re.search(r"<summary>(.*?)</summary>", e, re.S) else ""
            out.append(result("arxiv", title, link, f"published={published}. {summary}", "high", {"published": published, "title": title}))
        return out
    except Exception:
        return []


def source_semantic_scholar(text: str, claim_type: str) -> list[dict]:
    data = request_json("GET", "https://api.semanticscholar.org/graph/v1/paper/search", params={
        "query": text[:180],
        "limit": 3,
        "fields": "title,url,year,citationCount,authors,venue,abstract",
    }, timeout=15)
    out = []
    if isinstance(data, dict):
        for p in data.get("data", [])[:3]:
            authors = ", ".join(a.get("name", "") for a in p.get("authors", [])[:3])
            snippet = f"year={p.get('year')}, citations={p.get('citationCount')}, venue={p.get('venue')}, authors={authors}. {p.get('abstract') or ''}"
            out.append(result("semantic_scholar", p.get("title", ""), p.get("url", ""), snippet, "high", {"year": p.get("year"), "citation_count": p.get("citationCount"), "venue": p.get("venue")}))
    return out


def package_candidates(text: str) -> list[str]:
    cands = []
    lower = text.lower()
    aliases = {
        "next.js": "next",
        "nextjs": "next",
        "react": "react",
        "vue.js": "vue",
        "typescript": "typescript",
        "tailwind": "tailwindcss",
        "vite": "vite",
        "webpack": "webpack",
        "pytorch": "torch",
        "tensorflow": "tensorflow",
        "fastapi": "fastapi",
        "django": "django",
    }
    for k, v in aliases.items():
        if k in lower:
            cands.append(v)
    for m in re.findall(r"`([^`]+)`", text):
        cands.append(m.strip())
    for m in re.findall(r"(?:npm install|pnpm add|yarn add|pip install|pypi)\s+(@?[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)?)", text):
        cands.append(m.strip())
    for m in re.findall(r"@?[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+|[a-zA-Z][a-zA-Z0-9_.-]*-[a-zA-Z0-9_.-]+", text):
        cands.append(m.strip())
    seen = []
    bad = {"downloads", "download", "package", "packages", "monthly", "weekly", "million", "version"}
    for c in cands:
        c = c.strip(" .,;:()[]{}")
        if 2 <= len(c) <= 80 and c.lower() not in bad and c not in seen:
            seen.append(c)
    return seen[:4]


def source_pypi(text: str, claim_type: str) -> list[dict]:
    out = []
    for pkg in package_candidates(text):
        data = request_json("GET", f"https://pypi.org/pypi/{urllib.parse.quote(pkg)}/json", timeout=10)
        if isinstance(data, dict) and data.get("info"):
            info = data["info"]
            releases = data.get("releases", {})
            latest = info.get("version")
            release_dates = []
            for file in releases.get(latest, [])[:2]:
                if file.get("upload_time_iso_8601"):
                    release_dates.append(file["upload_time_iso_8601"])
            snippet = f"package={pkg}, latest_version={latest}, latest_upload={release_dates[:1]}, summary={info.get('summary')}, author={info.get('author')}"
            out.append(result("pypi", pkg, info.get("package_url", f"https://pypi.org/project/{pkg}/"), snippet, "high", {"package": pkg, "latest_version": latest, "latest_upload": release_dates[:1], "author": info.get("author")}))
    return out


def source_npm(text: str, claim_type: str) -> list[dict]:
    out = []
    for pkg in package_candidates(text):
        encoded = urllib.parse.quote(pkg, safe="")
        meta = request_json("GET", f"https://registry.npmjs.org/{encoded}", timeout=10)
        if isinstance(meta, dict) and meta.get("name"):
            latest = meta.get("dist-tags", {}).get("latest", "")
            time = meta.get("time", {}).get(latest, "")
            snippet = f"package={pkg}, latest_version={latest}, latest_time={time}, description={meta.get('description')}"
            out.append(result("npm", pkg, f"https://www.npmjs.com/package/{pkg}", snippet, "high", {"package": pkg, "latest_version": latest, "latest_time": time}))
            downloads = request_json("GET", f"https://api.npmjs.org/downloads/point/last-month/{encoded}", timeout=10)
            if isinstance(downloads, dict) and downloads.get("downloads") is not None:
                out.append(result("npm_downloads", f"{pkg} downloads", f"https://www.npmjs.com/package/{pkg}", f"last-month downloads={downloads.get('downloads')}, period={downloads.get('start')}..{downloads.get('end')}", "high", {"package": pkg, "last_month_downloads": downloads.get("downloads"), "period": f"{downloads.get('start')}..{downloads.get('end')}"}))
    return out[:4]


# ── Fetch source URL and verify keywords ─────────────────────────────────────
def fetch_and_verify(url: str, claim_keywords: list[str], timeout: int = 10) -> dict:
    if not url or not url.startswith("http"):
        return {"fetched": False, "keyword_found": False, "matched_keywords": [], "snippet": ""}
    try:
        import requests
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if not resp.ok:
            return {"fetched": False, "keyword_found": False, "matched_keywords": [], "snippet": ""}
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text)
        matched, snippet = [], ""
        for kw in claim_keywords:
            if len(kw) < 3:
                continue
            idx = text.lower().find(kw.lower())
            if idx != -1:
                matched.append(kw)
                if not snippet:
                    snippet = text[max(0, idx - 100): min(len(text), idx + 220)].strip()
        return {"fetched": True, "keyword_found": bool(matched), "matched_keywords": matched, "snippet": snippet[:350]}
    except Exception:
        return {"fetched": False, "keyword_found": False, "matched_keywords": [], "snippet": ""}


# ── LLM Wiki grep ─────────────────────────────────────────────────────────────
def wiki_grep(keywords: list[str], wiki_path: Path) -> str:
    if not wiki_path.exists():
        return ""
    pattern = "|".join(re.escape(k) for k in keywords if k)
    if not pattern:
        return ""
    curated = [wiki_path / d for d in ["entities", "concepts", "comparisons", "queries"] if (wiki_path / d).exists()]
    results = []
    for d in curated:
        for f in d.rglob("*.md"):
            try:
                text = f.read_text(errors="ignore")
                status_match = re.search(r"^status:\s*(\w+)", text, re.MULTILINE)
                status = status_match.group(1) if status_match else "unknown"
                hits = [ln.strip() for ln in text.splitlines() if re.search(pattern, ln, re.IGNORECASE)]
                if hits:
                    label = f"[{f.relative_to(wiki_path)}] (status:{status})"
                    results.append(label + "\n" + "\n".join(hits[:3]))
            except Exception:
                pass
    return "\n\n".join(results[:5])


def source_llm_wiki(text: str, claim_type: str, wiki_path: Path) -> list[dict]:
    keywords = [w for w in re.split(r"\W+", text) if len(w) > 3][:6]
    wiki_text = wiki_grep(keywords, wiki_path)
    if not wiki_text:
        return []
    return [result("llm_wiki", "Local LLM Wiki matches", "file://" + str(wiki_path), wiki_text, "high")]


SOURCE_FUNCTIONS = {
    "tavily_web": source_tavily_web,
    "github": source_github,
    "wikipedia": source_wikipedia,
    "arxiv": source_arxiv,
    "semantic_scholar": source_semantic_scholar,
    "pypi": source_pypi,
    "npm": source_npm,
}


def gather_evidence(text: str, claim_type: str, wiki_path: Path | None = None, use_llm_router: bool = False, use_wiki: bool = False, max_sources: int = 4, source_workers: int = 4) -> tuple[list[str], list[dict]]:
    routed = route_sources(claim_type, text, use_llm_router=use_llm_router, use_wiki=use_wiki, max_sources=max_sources)
    def call_source(source_name: str) -> list[dict]:
        try:
            if source_name == "llm_wiki":
                if use_wiki and wiki_path and wiki_path.exists():
                    return source_llm_wiki(text, claim_type, wiki_path)
                return []
            fn = SOURCE_FUNCTIONS.get(source_name)
            return fn(text, claim_type) if fn else []
        except Exception as exc:
            return [result(source_name, f"{source_name} error", "", f"Source failed: {type(exc).__name__}", "low")]

    all_results: list[dict] = []
    if len(routed) <= 1 or source_workers <= 1:
        for source_name in routed:
            all_results.extend(call_source(source_name))
    else:
        with ThreadPoolExecutor(max_workers=min(source_workers, len(routed))) as ex:
            futures = {ex.submit(call_source, source_name): source_name for source_name in routed}
            for fut in as_completed(futures):
                all_results.extend(fut.result())
    # Deduplicate by URL/title.
    deduped, seen = [], set()
    for r in all_results:
        key = r.get("url") or (r.get("source"), r.get("title"), r.get("snippet")[:80])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    deduped.sort(key=lambda r: r.get("evidence_score", 0), reverse=True)
    return routed, deduped[:10]


# ── Phase 0: Internal consistency check ──────────────────────────────────────
CONSISTENCY_SYSTEM = textwrap.dedent("""
    You are a meticulous editor. Read the article and find any internal
    contradictions — places where the article says two things that cannot
    both be true.

    For each contradiction found, output exactly:
    CONTRADICTION: <quote claim A> | <quote claim B> | <brief explanation>

    If no contradictions found, output: NO_CONTRADICTIONS
    Only report genuine logical contradictions, not stylistic inconsistencies.
""").strip()


def check_internal_consistency(article: str) -> list[dict]:
    raw = llm_call(CONSISTENCY_SYSTEM, f"Article:\n\n{article}")
    if "NO_CONTRADICTIONS" in raw:
        return []
    contradictions = []
    for line in raw.splitlines():
        m = re.match(r"CONTRADICTION:\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+)", line)
        if m:
            contradictions.append({"claim_a": m.group(1).strip(), "claim_b": m.group(2).strip(), "explanation": m.group(3).strip()})
    return contradictions


# ── Phase 1: Extract claims ───────────────────────────────────────────────────
EXTRACT_SYSTEM = textwrap.dedent("""
    You are a rigorous fact-checking assistant. Extract every independently
    verifiable factual claim from the article. One atomic claim per line.

    Format: [TYPE] claim text

    Types:
    [DATE]    specific date or time period tied to an event
    [NUMBER]  statistics, counts, version numbers, percentages
    [EVENT]   named events and their outcomes
    [ATTR]    attribution — who created/announced/released something
    [STATUS]  current state of a project, protocol, or tool
    [CAUSAL]  causal claims ("X led to Y", "because of X, Y happened")

    Rules:
    - Atomic: one fact per line, no compound claims
    - Exact: preserve the wording from the article, do not paraphrase
    - No opinions, recommendations, or predictions
    - No trivial background facts
    - No bare year strings unless tied to a specific claim
    - Target: 10–25 claims for a 1500–3000 word article
""").strip()


def extract_claims(article: str) -> list[dict]:
    raw = llm_call(EXTRACT_SYSTEM, f"Article:\n\n{article}")
    claims = []
    for line in raw.splitlines():
        line = line.strip()
        m = re.match(r"\[(DATE|NUMBER|EVENT|ATTR|STATUS|CAUSAL)\]\s+(.*)", line)
        if m:
            claims.append({"type": m.group(1), "text": m.group(2).strip()})
    return claims


# ── Phase 0a: Consistency risk gate ───────────────────────────────────────────
def consistency_risk(article: str, claims: list[dict]) -> tuple[int, list[str]]:
    """Cheap deterministic gate: decide whether full internal consistency check is worth running."""
    text = article.lower()
    type_counts = {t: 0 for t in ["DATE", "NUMBER", "EVENT", "ATTR", "STATUS", "CAUSAL"]}
    for c in claims:
        type_counts[c.get("type", "")] = type_counts.get(c.get("type", ""), 0) + 1

    risk = 0
    reasons = []

    if type_counts.get("STATUS", 0) >= 2:
        risk += 2
        reasons.append(f"multiple STATUS claims ({type_counts['STATUS']})")
    if type_counts.get("DATE", 0) >= 3:
        risk += 1
        reasons.append(f"dense timeline ({type_counts['DATE']} DATE claims)")
    if type_counts.get("CAUSAL", 0) >= 2:
        risk += 1
        reasons.append(f"multiple CAUSAL claims ({type_counts['CAUSAL']})")

    comparison_markers = [" vs ", " versus ", "compare", "comparison", "对比", "相比", "相较", "区别", "优于", "劣于"]
    has_comparison = any(m in text for m in comparison_markers)
    if has_comparison:
        risk += 2
        reasons.append("comparison structure")
    if has_comparison and (type_counts.get("CAUSAL", 0) >= 1 or type_counts.get("STATUS", 0) >= 1):
        risk += 1
        reasons.append("comparison with status/causal conclusion")

    lifecycle_keywords = [
        "deprecated", "obsolete", "merged", "renamed", "replaced", "migrated", "archived",
        "active", "maintained", "unmaintained", "current status", "no longer",
        "废弃", "过时", "合并", "并入", "迁移", "替代", "取代", "归档", "维护", "不再", "仍然活跃",
    ]
    hits = [k for k in lifecycle_keywords if k in text]
    if hits:
        risk += 2
        reasons.append("lifecycle/status keywords: " + ", ".join(hits[:5]))

    contradictory_pairs = [
        ("deprecated", "active"), ("deprecated", "maintained"), ("obsolete", "current"),
        ("merged", "independent"), ("archived", "active"), ("local-only", "cloud"),
        ("open-source", "proprietary"), ("free", "paid-only"),
        ("废弃", "活跃"), ("废弃", "维护"), ("并入", "独立"), ("本地", "云端必需"),
        ("开源", "闭源"), ("免费", "付费"),
    ]
    pair_hits = [f"{a}/{b}" for a, b in contradictory_pairs if a in text and b in text]
    if pair_hits:
        risk += 3
        reasons.append("potential contradictory keyword pairs: " + ", ".join(pair_hits[:4]))

    if len(article) > 3000:
        risk += 1
        reasons.append("long article")

    return risk, reasons


def should_run_consistency(article: str, claims: list[dict], *, skip: bool = False, force: bool = False) -> tuple[bool, int, list[str]]:
    if skip:
        return False, 0, ["--skip-consistency"]
    if force:
        return True, 999, ["--force-consistency"]
    risk, reasons = consistency_risk(article, claims)
    return risk >= 3, risk, reasons


# ── Rate + suggestion ─────────────────────────────────────────────────────────
RATE_SYSTEM = textwrap.dedent("""
    You are a fact-checking assistant. Given a claim and evidence gathered from
    routed sources, rate the claim and propose an edit if needed.

    Ratings:
    ✅ CONFIRMED    — official or multiple sources explicitly confirm this
    🟡 LIKELY       — one reliable source supports it, no contradiction
    ⚠️ UNCERTAIN    — conflicting sources, source page doesn't mention it, or too vague
    ❌ WRONG        — a source explicitly and clearly contradicts this claim
    🔍 UNSOURCED    — no relevant evidence found either way

    Output ONLY valid JSON:
    {
      "rating": "✅ CONFIRMED",
      "evidence": "one-sentence summary of what the sources say",
      "source_url": "most relevant URL or empty string",
      "correction": "corrected text if WRONG, else empty string",
      "suggestion": "actionable edit suggestion"
    }

    Suggestion rules:
    - ✅ CONFIRMED: empty string
    - 🟡 LIKELY: suggest adding citation to the best source
    - ⚠️ UNCERTAIN: suggest hedging, specifying source, or marking [citation needed]
    - ❌ WRONG: corrected replacement sentence, same semantic scope as original
    - 🔍 UNSOURCED: suggest delete / hedge / research terms

    Important:
    - Prefer ⚠️ over ❌ unless a source explicitly contradicts the claim
    - Evidence from source-specific APIs (GitHub, npm, PyPI, arXiv, Semantic Scholar) is stronger than generic snippets
    - Official sources outweigh blogs
    - If fetched source page doesn't contain claim keywords, treat the search result as weak/noisy
""").strip()


ADVERSARIAL_SYSTEM = textwrap.dedent("""
    A claim has been rated ❌ WRONG by a first reviewer.
    Play devil's advocate: find any credible evidence that supports the claim.

    Output ONLY valid JSON:
    {
      "supporting_evidence": "what supports the claim, or empty string",
      "revised_rating": "❌ WRONG or ⚠️ UNCERTAIN",
      "reasoning": "why"
    }

    If credible supporting evidence exists, revise to ⚠️ UNCERTAIN.
    If nothing supports it, keep ❌ WRONG.
""").strip()


def verify_claim(claim: dict, wiki_path: Path | None = None, use_llm_router: bool = False, use_wiki: bool = False, max_sources: int = 4, source_workers: int = 4, adversarial_policy=True) -> dict:
    text = claim["text"]
    ctype = claim["type"]

    routed_sources, evidence_results = gather_evidence(text, ctype, wiki_path, use_llm_router=use_llm_router, use_wiki=use_wiki, max_sources=max_sources, source_workers=source_workers)
    evidence_text = format_results(evidence_results)

    keywords = [w for w in re.split(r"\W+", text) if len(w) > 3][:6]
    best_evidence = next((r for r in evidence_results if r.get("url", "").startswith("http")), {})
    best_url = best_evidence.get("url", "")
    # v5: only fetch generic web pages. Structured APIs already returned canonical data.
    should_fetch = bool(best_url) and not best_evidence.get("structured", False)
    fetch_info = fetch_and_verify(best_url, keywords[:4]) if should_fetch else {"fetched": False, "keyword_found": False, "matched_keywords": [], "snippet": ""}

    if best_evidence.get("structured", False):
        fetch_note = f"Fetch skipped: best evidence is structured API data from {best_evidence.get('source')}."
    elif fetch_info["fetched"] and fetch_info["keyword_found"]:
        fetch_note = f"Source page fetched ✓ — keywords {fetch_info['matched_keywords']} found. Snippet: {fetch_info['snippet']}"
    elif fetch_info["fetched"]:
        fetch_note = "Source page fetched but claim keywords NOT found; URL may be noisy."
    else:
        fetch_note = "Source page could not be fetched, or no URL available."

    user_prompt = f"""Claim ({ctype}): {text}

Routed sources:
{', '.join(routed_sources)}

Evidence results:
{evidence_text}

Evidence quality note:
High authority/directness/freshness structured API evidence should outweigh generic snippets.

Source page verification:
{fetch_note}"""

    raw = llm_call(RATE_SYSTEM, user_prompt)
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        rated = json.loads(m.group()) if m else {}
    except Exception:
        rated = {}

    rating = rated.get("rating", "🔍 UNSOURCED")
    evidence = rated.get("evidence", "")
    source_url = rated.get("source_url", best_url)
    correction = rated.get("correction", "")
    suggestion = rated.get("suggestion", "")

    adversarial_note = ""
    top_score = max((r.get("evidence_score", 0) for r in evidence_results), default=0)
    strong_structured_evidence = any(r.get("structured") and r.get("evidence_score", 0) >= 0.80 for r in evidence_results)
    should_adversarial = bool(adversarial_policy) and "❌" in rating and not (adversarial_policy == "conditional" and strong_structured_evidence and ctype in {"DATE", "NUMBER", "STATUS", "ATTR"})
    if "❌" in rating and not should_adversarial:
        adversarial_note = "Skipped: high-authority structured evidence is sufficient for this factual mismatch."
    if should_adversarial:
        adv_prompt = f"""Claim: {text}

Evidence used by first reviewer:
{evidence_text}

First reviewer rating: {rating}
First reviewer correction: {correction}

Now find evidence FOR this claim."""
        adv_raw = llm_call(ADVERSARIAL_SYSTEM, adv_prompt)
        try:
            adv_m = re.search(r"\{.*\}", adv_raw, re.DOTALL)
            adv = json.loads(adv_m.group()) if adv_m else {}
        except Exception:
            adv = {}
        if adv.get("revised_rating", "").startswith("⚠️"):
            rating = "⚠️ UNCERTAIN"
            adversarial_note = f"Adversarial pass: {adv.get('reasoning', '')} Supporting: {adv.get('supporting_evidence', '')}"
            correction = ""
            suggestion = suggestion or adv.get("supporting_evidence", "")
        else:
            adversarial_note = f"Adversarial pass confirmed ❌: {adv.get('reasoning', '')}"

    return {
        "type": ctype,
        "text": text,
        "rating": rating,
        "evidence": evidence,
        "source_url": source_url,
        "correction": correction,
        "suggestion": suggestion,
        "fetch_verified": fetch_info.get("keyword_found", False),
        "routed_sources": routed_sources,
        "top_evidence_score": top_score,
        "structured_evidence": strong_structured_evidence,
        "adversarial_note": adversarial_note,
    }


# ── Report ───────────────────────────────────────────────────────────────────
RATING_ORDER = ["✅ CONFIRMED", "🟡 LIKELY", "⚠️ UNCERTAIN", "❌ WRONG", "🔍 UNSOURCED"]
RATING_HEADERS = {
    "✅ CONFIRMED": "✅ Confirmed",
    "🟡 LIKELY": "🟡 Likely Correct",
    "⚠️ UNCERTAIN": "⚠️ Uncertain / Needs Human Review",
    "❌ WRONG": "❌ Wrong — Needs Correction",
    "🔍 UNSOURCED": "🔍 Unsourced — Could Not Verify",
}


def rating_key(rating: str) -> str:
    return next((r for r in RATING_ORDER if r in rating), "🔍 UNSOURCED")


def generate_report(article_path: Path, results: list[dict], contradictions: list[dict] | None, mode: str = "draft", total_claims: int | None = None) -> str:
    counts = {r: 0 for r in RATING_ORDER}
    for res in results:
        counts[rating_key(res["rating"])] += 1
    summary = " | ".join(f"{k} {v}" for k, v in counts.items() if v > 0)

    lines = [
        f"# LLM Output Audit Report: {article_path.name}",
        f"Checked: {datetime.now().strftime('%Y-%m-%d')}",
        f"Claims audited: {len(results)}" + (f" / {total_claims} extracted" if total_claims is not None else ""),
        f"Audit mode: {mode}",
        f"Verdict summary: {summary}",
        "",
        "---",
    ]

    if contradictions is None:
        lines.append("\n## ⏭️ Internal Consistency: Skipped\n")
    elif contradictions:
        lines.append(f"\n## 🔴 Internal Contradictions ({len(contradictions)} found)\n")
        for c in contradictions:
            lines.append(f"- **Claim A:** {c['claim_a']}")
            lines.append(f"  **Claim B:** {c['claim_b']}")
            lines.append(f"  **Why:** {c['explanation']}\n")
    else:
        lines.append("\n## 🟢 Internal Consistency: No contradictions found\n")

    lines.append("---")
    grouped = {r: [] for r in RATING_ORDER}
    for res in results:
        grouped[rating_key(res["rating"])].append(res)

    for rating in RATING_ORDER:
        group = grouped[rating]
        if not group:
            continue
        lines.append(f"\n## {RATING_HEADERS[rating]}\n")
        for res in group:
            fetch_badge = " 🔗✓" if res.get("fetch_verified") else ""
            lines.append(f"- **[{res['type']}]** {res['text']}{fetch_badge}")
            lines.append(f"  - Routed sources: {', '.join(res.get('routed_sources', []))}")
            lines.append(f"  - Source quality: score={res.get('top_evidence_score', 0)} structured={res.get('structured_evidence', False)}")
            if res.get("evidence"):
                lines.append(f"  - Evidence: {res['evidence']}")
            if res.get("source_url"):
                lines.append(f"  - Source: {res['source_url']}")
            if res.get("adversarial_note"):
                lines.append(f"  - Adversarial check: {res['adversarial_note']}")
            if res.get("correction"):
                lines.append(f"  - **Fix:** {res['correction']}")
            if res.get("suggestion") and not res.get("correction"):
                lines.append(f"  - **Suggestion:** {res['suggestion']}")
            lines.append("")

    actionable = [r for r in results if r.get("suggestion") or r.get("correction")]
    wrong_only = [r for r in actionable if "❌" in r["rating"]]
    needs_work = [r for r in actionable if "❌" not in r["rating"] and "✅" not in r["rating"]]
    if wrong_only or needs_work:
        lines.append("\n---\n\n## 📝 Edit Suggestions\n")
        if wrong_only:
            lines.append("### ❌ Corrections (factually wrong — must fix)\n")
            for res in wrong_only:
                lines.append(f"**Claim:** {res['text']}")
                lines.append(f"**Fix:**   {res.get('correction') or res.get('suggestion', '')}\n")
        if needs_work:
            lines.append("### ⚠️🔍🟡 Improvements (uncertain / unsourced / likely)\n")
            for res in needs_work:
                badge = res["rating"].split()[0]
                lines.append(f"{badge} **{res['text']}**")
                lines.append(f"   → {res.get('suggestion', '')}\n")

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Audit long-form LLM output (v6 — modes + parallel evidence checks)")
    parser.add_argument("--file", required=True, help="Path to the Markdown article")
    parser.add_argument("--output", help="Report path (default: <file>-audit.md)")
    parser.add_argument("--wiki", default="", help="Optional LLM Wiki root; only used with --use-wiki")
    parser.add_argument("--use-wiki", action="store_true", help="Enable optional local LLM Wiki evidence source")
    parser.add_argument("--skip-consistency", action="store_true", help="Skip internal consistency check")
    parser.add_argument("--force-consistency", action="store_true", help="Force internal consistency check regardless of risk gate")
    parser.add_argument("--dry-run", action="store_true", help="Extract claims only")
    parser.add_argument("--no-fetch", action="store_true", help="Skip source page fetching")
    parser.add_argument("--llm-router", action="store_true", help="Use LLM to refine source routing when ambiguous")
    parser.add_argument("--mode", choices=["auto", "fast", "spot", "draft", "full"], default="auto", help="Audit speed/depth mode")
    parser.add_argument("--workers", type=int, default=min(6, (os.cpu_count() or 4)), help="Parallel claim verification workers")
    parser.add_argument("--source-workers", type=int, default=4, help="Parallel evidence-source workers per claim")
    args = parser.parse_args()

    article_path = Path(args.file)
    if not article_path.exists():
        sys.exit(f"File not found: {article_path}")

    if args.no_fetch:
        global fetch_and_verify
        fetch_and_verify = lambda url, kw, **kw2: {"fetched": False, "keyword_found": False, "matched_keywords": [], "snippet": ""}

    wiki_path = Path(args.wiki).expanduser() if args.wiki else None
    use_wiki = bool(args.use_wiki and wiki_path and wiki_path.exists())
    if args.use_wiki and not use_wiki:
        print(f"⚠️  --use-wiki requested but wiki path is missing: {args.wiki or '(empty)'}; continuing without LLM Wiki")
    output_path = Path(args.output) if args.output else article_path.with_name(article_path.stem + "-audit.md")
    article = article_path.read_text(errors="ignore")

    print(f"📄 Article: {article_path.name} ({len(article)} chars)")

    print("🔍 Phase 1: Extracting verifiable claims...")
    claims = extract_claims(article)
    print(f"   Found {len(claims)} claims")

    if args.dry_run:
        print("\nClaims:")
        for c in claims:
            print(f"  [{c['type']}] {c['text']}")
        return

    mode = infer_audit_mode(article, claims) if args.mode == "auto" else args.mode
    cfg = AUDIT_MODES[mode]
    selected_claims = select_claims_for_mode(claims, mode)
    print(f"⚙️  Audit mode: {mode} — {cfg['description']}")
    print(f"   Selected {len(selected_claims)} / {len(claims)} claims for audit")
    if not selected_claims:
        print("   No full audit work in fast mode. Use --mode spot/draft/full for scripted audits.")

    contradictions = None
    consistency_setting = cfg["consistency"]
    force_consistency = args.force_consistency or consistency_setting is True
    skip_consistency = args.skip_consistency or consistency_setting is False
    run_consistency, risk_score, risk_reasons = should_run_consistency(
        article,
        claims,
        skip=skip_consistency,
        force=force_consistency,
    )
    if run_consistency:
        print(f"🔄 Phase 0: Checking internal consistency... risk={risk_score} ({'; '.join(risk_reasons) or 'forced'})")
        contradictions = check_internal_consistency(article)
        if contradictions:
            print(f"   ⚠️  {len(contradictions)} internal contradiction(s) found")
        else:
            print("   ✅ No internal contradictions")
    else:
        print(f"⏭️  Phase 0: Skipping internal consistency check... risk={risk_score} ({'; '.join(risk_reasons) or 'low risk'})")

    print("🧭 Phases 2–4: Routing sources + verifying claims...")
    results = [None] * len(selected_claims)
    use_llm_router = args.llm_router or bool(cfg["llm_router"])
    max_sources = int(cfg["max_sources"])
    adversarial_policy = cfg["adversarial"]

    def run_one(pos_claim: tuple[int, dict]) -> tuple[int, dict]:
        pos, claim = pos_claim
        routed_preview = route_sources(claim["type"], claim["text"], use_llm_router=use_llm_router, use_wiki=use_wiki, max_sources=max_sources)
        print(f"  [{pos+1:02d}/{len(selected_claims)}] [{claim['type']}] {claim['text'][:65]}...")
        print(f"          sources: {', '.join(routed_preview)}")
        res = verify_claim(
            claim,
            wiki_path,
            use_llm_router=use_llm_router,
            use_wiki=use_wiki,
            max_sources=max_sources,
            source_workers=args.source_workers,
            adversarial_policy=adversarial_policy,
        )
        fetch_badge = "🔗✓" if res.get("fetch_verified") else "  "
        adv_badge = " (adv)" if res.get("adversarial_note") else ""
        print(f"          → {res['rating'].split()[0]} {fetch_badge}{adv_badge}")
        return pos, res

    if selected_claims:
        if args.workers <= 1 or len(selected_claims) <= 1:
            for item in enumerate(selected_claims):
                pos, res = run_one(item)
                results[pos] = res
        else:
            with ThreadPoolExecutor(max_workers=min(args.workers, len(selected_claims))) as ex:
                futures = [ex.submit(run_one, item) for item in enumerate(selected_claims)]
                for fut in as_completed(futures):
                    pos, res = fut.result()
                    results[pos] = res
    results = [r for r in results if r is not None]

    report = generate_report(article_path, results, contradictions, mode=mode, total_claims=len(claims))
    output_path.write_text(report)

    wrong = sum(1 for r in results if "❌" in r["rating"])
    uncertain = sum(1 for r in results if "⚠️" in r["rating"])
    confirmed = sum(1 for r in results if "✅" in r["rating"])
    unsourced = sum(1 for r in results if "🔍" in r["rating"])
    likely = len(results) - confirmed - uncertain - wrong - unsourced

    print(f"\n✅ Report saved: {output_path}")
    print(f"   ✅ {confirmed} confirmed | 🟡 {likely} likely | ⚠️ {uncertain} uncertain | ❌ {wrong} wrong | 🔍 {unsourced} unsourced")
    if contradictions:
        print(f"   🔴 {len(contradictions)} internal contradiction(s) — fix before publishing")
    if wrong:
        print(f"   ❌ {wrong} error(s) found — see Edit Suggestions")


if __name__ == "__main__":
    main()
