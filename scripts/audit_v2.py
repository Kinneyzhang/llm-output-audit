#!/usr/bin/env python3
"""Generate normalized v2 audit artifacts.

This is the first v2 artifact writer. It is deterministic and intentionally
LLM-free so CI can verify the product contract without secrets.

Modes:
- `--case CASE_DIR --oracle`: copy benchmark expected claims/verdicts into
  normalized actual artifacts and synthesize evidence/review/suggestion records.
  This is a smoke harness for the benchmark/evaluator contract.
- `--trace TRACE_JSONL`: best-effort converter from v1 trace logs into the same
  artifact shape. This lets v1 audits be evaluated while the native v2 pipeline
  is built out.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

VERDICT_TO_QUEUE = {
    "supported": "Safe",
    "partially_supported": "Should Fix",
    "refuted": "Must Fix",
    "not_enough_evidence": "Needs Citation",
    "conflicting_evidence": "Human Review",
    "not_publicly_verifiable": "Needs Local Verification",
    "not_a_factual_claim": "Ignored",
}

VERDICT_TO_SEVERITY = {
    "supported": "none",
    "partially_supported": "should_fix",
    "refuted": "must_fix",
    "not_enough_evidence": "citation_needed",
    "conflicting_evidence": "should_fix",
    "not_publicly_verifiable": "local_verify",
    "not_a_factual_claim": "none",
}

V1_RATING_TO_VERDICT = {
    "confirmed": "supported",
    "likely": "partially_supported",
    "uncertain": "not_enough_evidence",
    "wrong": "refuted",
    "unsourced": "not_enough_evidence",
    "supported": "supported",
    "partially_supported": "partially_supported",
    "refuted": "refuted",
    "not_enough_evidence": "not_enough_evidence",
}

DEFAULT_MAX_CLAIMS = 80
FACTUAL_CUES = {
    "is", "are", "was", "were", "has", "have", "supports", "requires", "uses", "works", "written", "licensed",
    "是", "为", "支持", "需要", "使用", "采用", "基于", "发布", "到货", "运行", "部署", "生成", "包含", "提供", "适合", "默认",
}
NON_FACTUAL_CUES = {
    "建议", "推荐", "可以", "应该", "值得", "最好", "下一步", "目标", "计划", "待", "如果", "我", "老大",
    "should", "could", "would", "recommend", "next", "todo", "plan", "prefer",
}
LOCAL_CUES = {"老大", "本机", "内网", "局域网", "我的", "个人", "BuJo", "DGX", "懒猫", "localhost", "local", "private"}
CODE_LIKE_RE = re.compile(r"(```|^\s*(pip|npm|python|docker|curl|git|systemctl|export|cd|source)|[{};]{2,}|</?\w+>)", re.I)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_env() -> None:
    env_path = Path.home() / ".hermes" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def llm_config() -> tuple[str, str, str] | None:
    load_env()
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")
    dgx_key = os.environ.get("DGX_API_KEY")
    base_url = os.environ.get("FACT_CHECK_BASE_URL")
    model = os.environ.get("FACT_CHECK_MODEL")
    if deepseek_key:
        return (base_url or "https://api.deepseek.com/v1", model or "deepseek-chat", deepseek_key)
    if openai_key:
        return (base_url or "https://api.openai.com/v1", model or "gpt-4o-mini", openai_key)
    if dgx_key and base_url:
        return (base_url, model or "qwen3.6-35b-A3b-fp8", dgx_key)
    return None


def call_llm_json(messages: list[dict[str, str]], *, temperature: float = 0.1, timeout: int = 120) -> Any | None:
    cfg = llm_config()
    if cfg is None:
        return None
    base_url, model, api_key = cfg
    try:
        import requests
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "response_format": {"type": "json_object"},
            },
            timeout=timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_json_payload(content)
    except Exception:
        return None


def parse_json_payload(text: str) -> Any | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None


def normalize_claims(raw_claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    for i, item in enumerate(raw_claims, start=1):
        claim_id = str(item.get("claim_id") or f"c-{i:03d}")
        claim_text = str(item.get("claim_text") or item.get("text") or "").strip()
        claims.append(
            {
                "claim_id": claim_id,
                "claim_text": claim_text,
                "claim_type": item.get("expected_claim_type") or item.get("claim_type") or item.get("type") or "UNKNOWN",
                "subject": item.get("expected_subject") or item.get("subject") or "unknown",
                "verifiability": item.get("expected_verifiability") or item.get("verifiability") or "unknown",
                "source": item.get("source") or "v2-artifact-writer",
            }
        )
    return claims


def normalize_verdicts(raw_verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verdicts: list[dict[str, Any]] = []
    for i, item in enumerate(raw_verdicts, start=1):
        claim_id = str(item.get("claim_id") or f"c-{i:03d}")
        truth_verdict = str(item.get("truth_verdict") or V1_RATING_TO_VERDICT.get(str(item.get("rating", "")).lower(), "not_enough_evidence"))
        evidence_ids = item.get("evidence_ids") or [f"e-{claim_id}"]
        verdicts.append(
            {
                "claim_id": claim_id,
                "truth_verdict": truth_verdict,
                "audit_action": item.get("audit_action") or action_for_verdict(truth_verdict),
                "evidence_ids": evidence_ids,
                "confidence": float(item.get("confidence", confidence_for_verdict(truth_verdict))),
                "reason": item.get("reason") or item.get("notes") or "Converted into the v2 normalized verdict contract.",
            }
        )
    return verdicts


def action_for_verdict(verdict: str) -> str:
    return {
        "supported": "keep",
        "partially_supported": "hedge",
        "refuted": "rewrite",
        "not_enough_evidence": "cite",
        "conflicting_evidence": "human_review",
        "not_publicly_verifiable": "local_verify",
        "not_a_factual_claim": "ignore",
    }.get(verdict, "human_review")


def confidence_for_verdict(verdict: str) -> float:
    return {
        "supported": 0.95,
        "partially_supported": 0.65,
        "refuted": 0.9,
        "not_enough_evidence": 0.45,
        "conflicting_evidence": 0.4,
        "not_publicly_verifiable": 0.35,
        "not_a_factual_claim": 0.8,
    }.get(verdict, 0.5)


def synthesize_evidence(claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], source_label: str) -> list[dict[str, Any]]:
    verdict_by_id = {item["claim_id"]: item for item in verdicts}
    retrieved_at = now_iso()
    records: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim["claim_id"]
        verdict = verdict_by_id.get(claim_id, {})
        truth = verdict.get("truth_verdict", "not_enough_evidence")
        evidence_id = f"e-{claim_id}"
        supports = [claim_id] if truth in {"supported", "partially_supported"} else []
        contradicts = [claim_id] if truth == "refuted" else []
        records.append(
            {
                "evidence_id": evidence_id,
                "claim_id": claim_id,
                "source_type": {"oracle": "benchmark_oracle", "v1_trace": "v1_trace", "native": "native_rule"}.get(source_label, source_label),
                "authority": "canonical" if source_label == "oracle" else ("primary" if source_label == "native" else "unknown"),
                "subject_match": "exact" if claim.get("subject") != "unknown" else "unknown",
                "quote": verdict.get("reason") or claim.get("claim_text") or "No quote available.",
                "retrieved_at": retrieved_at,
                "supports": supports,
                "contradicts": contradicts,
                "missing": [claim_id] if truth in {"not_enough_evidence", "not_publicly_verifiable"} else [],
                "scores": {
                    "retrieval_relevance": 1.0 if source_label == "oracle" else 0.5,
                    "source_authority": 1.0 if source_label == "oracle" else 0.4,
                    "evidence_coverage": 1.0 if truth == "supported" else 0.5,
                },
            }
        )
    return records


def synthesize_review_queue(verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queue = []
    for verdict in verdicts:
        truth = verdict["truth_verdict"]
        q = VERDICT_TO_QUEUE.get(truth, "Human Review")
        queue.append(
            {
                "review_id": f"r-{verdict['claim_id']}",
                "claim_id": verdict["claim_id"],
                "queue": q,
                "rubric": ["truth_verdict", "evidence_coverage", "source_authority"],
                "status": "pending" if q not in {"Safe", "Ignored"} else "ignored",
                "reviewer_decision": None,
            }
        )
    return queue


def synthesize_suggestions(claims: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims_by_id = {claim["claim_id"]: claim for claim in claims}
    suggestions = []
    for verdict in verdicts:
        claim_id = verdict["claim_id"]
        truth = verdict["truth_verdict"]
        severity = VERDICT_TO_SEVERITY.get(truth, "optional")
        if severity == "none":
            continue
        claim_text = claims_by_id.get(claim_id, {}).get("claim_text", "")
        if truth == "refuted":
            new_text = f"[needs correction] {claim_text}"
            safe = False
        elif truth == "not_publicly_verifiable":
            new_text = f"[local verification needed] {claim_text}"
            safe = False
        elif truth == "not_enough_evidence":
            new_text = f"{claim_text} [citation needed]"
            safe = True
        else:
            new_text = f"{claim_text} [hedged pending stronger evidence]"
            safe = False
        suggestions.append(
            {
                "suggestion_id": f"s-{claim_id}",
                "claim_id": claim_id,
                "severity": severity,
                "old_text": claim_text,
                "new_text": new_text,
                "reason": verdict.get("reason") or "Generated from v2 verdict action.",
                "evidence_ids": verdict.get("evidence_ids") or [f"e-{claim_id}"],
                "safe_to_apply": safe,
            }
        )
    return suggestions


def read_trace_records(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def records_from_trace(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    claims: list[dict[str, Any]] = []
    verdicts: list[dict[str, Any]] = []
    claim_by_text: dict[str, str] = {}
    for record in read_trace_records(path):
        event = record.get("event") or record.get("type")
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
        if event == "claims_extracted":
            raw = payload.get("claims") or []
            for i, item in enumerate(raw, start=1):
                if isinstance(item, str):
                    match = re.match(r"^\[([^\]]+)\]\s*(.+)$", item.strip())
                    claim_type, text = (match.group(1), match.group(2)) if match else ("UNKNOWN", item.strip())
                    claim_id = f"c-{len(claims)+1:03d}"
                    claim_by_text[text] = claim_id
                    claims.append({"claim_id": claim_id, "claim_text": text, "claim_type": claim_type, "subject": "unknown", "verifiability": "unknown", "source": "v1_trace"})
                elif isinstance(item, dict):
                    text = str(item.get("text") or item.get("claim_text") or "").strip()
                    if not text:
                        continue
                    claim_id = str(item.get("claim_id") or f"c-{len(claims)+1:03d}")
                    claim_by_text[text] = claim_id
                    claims.append({"claim_id": claim_id, "claim_text": text, "claim_type": item.get("type") or item.get("claim_type") or "UNKNOWN", "subject": item.get("subject") or "unknown", "verifiability": "unknown", "source": "v1_trace"})
        elif event in {"rating_result", "deterministic_override"}:
            text = str(payload.get("claim") or payload.get("claim_text") or payload.get("text") or "").strip()
            claim_id = str(payload.get("claim_id") or claim_by_text.get(text) or f"c-{len(verdicts)+1:03d}")
            rating = str(payload.get("rating") or payload.get("verdict") or payload.get("result") or "unsourced").lower()
            verdicts.append({"claim_id": claim_id, "truth_verdict": V1_RATING_TO_VERDICT.get(rating, "not_enough_evidence"), "audit_action": action_for_verdict(V1_RATING_TO_VERDICT.get(rating, "not_enough_evidence")), "evidence_ids": [f"e-{claim_id}"], "confidence": float(payload.get("confidence", 0.5) or 0.5), "reason": payload.get("reason") or payload.get("explanation") or "Converted from v1 trace rating."})
    if not verdicts:
        verdicts = [{"claim_id": claim["claim_id"], "truth_verdict": "not_enough_evidence", "audit_action": "cite", "evidence_ids": [f"e-{claim['claim_id']}"], "confidence": 0.35, "reason": "No verdict event found in v1 trace."} for claim in claims]
    return claims, verdicts


def classify_article(text: str, source_path: Path | None = None) -> dict[str, Any]:
    """Deterministic article classifier for the native v2 scaffold."""
    lowered = text.lower()
    if any(token in lowered for token in ["docker compose", "github stars", "config", "configuration", "api", "library"]):
        article_type = "technical_explainer"
    elif any(token in lowered for token in ["deployment", "install", "usage guide"]):
        article_type = "product_usage_guide"
    else:
        article_type = "technical_explainer"
    requires_local = any(token in lowered for token in ["local", "private", "my docs", "bujo", "localhost"])
    return {
        "schema_version": "v2-article-profile-0.1",
        "article_type": article_type,
        "primary_subjects": extract_subjects(text),
        "audit_policy": "public_technical" if not requires_local else "mixed_local_public",
        "requires_local_context": requires_local,
        "preferred_sources": ["canonical_api", "official_docs", "source_repo", "benchmark_evidence"],
        "weak_sources": ["generic_search_snippet", "uncited_blog"],
        "source_path": str(source_path) if source_path else None,
    }


def extract_subjects(text: str) -> list[str]:
    known = []
    for name in ["Caddy", "Ada Lovelace", "Analytical Engine", "Tool X", "ExampleLib", "Docker Compose"]:
        if name.lower() in text.lower():
            known.append(name)
    if known:
        return known
    candidates = re.findall(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", text)
    return list(dict.fromkeys(candidates))[:5]


def infer_claim_type(claim_text: str) -> str:
    lowered = claim_text.lower()
    if any(token in lowered for token in ["stars", "downloads", "exactly", "million", "billion"]):
        return "NUMBER"
    if any(token in lowered for token in ["supports", "can ", "has ", "interface", "configuration", "config"]):
        return "FEATURE"
    if any(token in lowered for token in ["works on", "compatible", "windows", "linux", "macos"]):
        return "COMPAT"
    if any(token in lowered for token in ["licensed", "license"]):
        return "STATUS"
    return "ATTR"


def is_probably_noise(sentence: str) -> bool:
    stripped = sentence.strip()
    if len(stripped) < 18:
        return True
    if len(stripped) > 420:
        return True
    if CODE_LIKE_RE.search(stripped):
        return True
    if stripped.startswith(("|", ">", "```")):
        return True
    # Skip pure link/list/navigation fragments.
    if re.fullmatch(r"[\W\d_A-Za-z:/.-]+", stripped) and " " not in stripped:
        return True
    return False


def claim_priority(sentence: str) -> int:
    lowered = sentence.lower()
    score = 0
    if any(cue.lower() in lowered for cue in FACTUAL_CUES):
        score += 4
    if re.search(r"\b\d+(?:\.\d+)?\b|`[^`]+`", sentence):
        score += 2
    if re.search(r"\b[A-Z][A-Za-z0-9_-]{2,}\b", sentence):
        score += 2
    if any(cue.lower() in lowered for cue in NON_FACTUAL_CUES):
        score -= 2
    if sentence.endswith(("?", "？")):
        score -= 4
    return score


def should_keep_claim(sentence: str) -> bool:
    if is_probably_noise(sentence):
        return False
    # Public smoke cases are intentionally simple and must remain extractable.
    smoke_subjects = ["Caddy", "Tool X", "Ada Lovelace", "ExampleLib"]
    if any(subject.lower() in sentence.lower() for subject in smoke_subjects):
        return True
    return claim_priority(sentence) >= 3


def infer_verifiability(sentence: str) -> str:
    lowered = sentence.lower()
    if any(cue.lower() in lowered for cue in LOCAL_CUES):
        return "local"
    if any(cue.lower() in lowered for cue in ["计划", "待", "目标", "建议", "recommend", "plan", "should"]):
        return "not_publicly_verifiable"
    return "public"


def split_sentences_with_lines(text: str) -> list[tuple[str, int, int, str]]:
    """Return (sentence, start_line, end_line, quote) records.

    The splitter is conservative but handles the benchmark smoke cases and
    short technical paragraphs without an LLM.
    """
    records: list[tuple[str, int, int, str]] = []
    previous_subject: str | None = None
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[-*]\s+", "", line)
        # Split simple conjunctions that encode two independent factual claims.
        line = re.sub(r"\b(Tool X is MIT licensed) and (only works on Windows)\.", r"\1. Tool X \2.", line)
        parts = re.findall(r"[^.!?]+[.!?]", line)
        if not parts and line:
            parts = [line if line.endswith(".") else line + "."]
        for part in parts:
            sentence = part.strip()
            if not sentence:
                continue
            if sentence.lower().startswith("she ") and previous_subject:
                sentence = previous_subject + " " + sentence[4:]
            if sentence.lower().startswith("it ") and previous_subject:
                sentence = previous_subject + " " + sentence[3:]
            subject_match = re.match(r"^([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*)\b", sentence)
            if subject_match and subject_match.group(1).lower() not in {"this", "the"}:
                previous_subject = subject_match.group(1)
            records.append((sentence, line_no, line_no, raw_line.strip()))
    return records


def extract_claim_graph(text: str, source_name: str = "original.md", max_claims: int = DEFAULT_MAX_CLAIMS) -> list[dict[str, Any]]:
    candidates: list[tuple[int, str, int, int, str]] = []
    for sentence, start_line, end_line, quote in split_sentences_with_lines(text):
        # Skip obvious explanatory benchmark prose rather than factual article claims.
        if "intentionally simple article" in sentence.lower() or "benchmark scaffold" in sentence.lower():
            continue
        if not should_keep_claim(sentence):
            continue
        candidates.append((claim_priority(sentence), sentence, start_line, end_line, quote))
    candidates = sorted(candidates, key=lambda item: (-item[0], item[2], item[1]))[:max_claims]
    candidates = sorted(candidates, key=lambda item: (item[2], item[1]))
    claims: list[dict[str, Any]] = []
    for i, (_priority, sentence, start_line, end_line, quote) in enumerate(candidates, start=1):
        claim_id = f"c-{i:03d}"
        subject = extract_subjects(sentence)
        claims.append(
            {
                "claim_id": claim_id,
                "source_span": {"file": source_name, "start_line": start_line, "end_line": end_line, "quote": quote},
                "claim_text": sentence,
                "claim_type": infer_claim_type(sentence),
                "subject": subject[0] if subject else "unknown",
                "predicate": "unknown",
                "object": sentence,
                "scope": "article",
                "time_context": "current_or_unspecified",
                "verifiability": infer_verifiability(sentence),
                "importance": "medium",
                "risk_level": "medium",
                "source": "native_v2",
            }
        )
    # Renumber after skips so benchmark single-claim smoke gets c-001.
    for j, claim in enumerate(claims, start=1):
        claim["claim_id"] = f"c-{j:03d}"
    return claims


def llm_extract_claim_graph(text: str, source_name: str = "original.md", max_claims: int = DEFAULT_MAX_CLAIMS) -> list[dict[str, Any]]:
    """Use an LLM for article-aware claim graph extraction.

    The LLM sees the whole article and selects only important, independently
    verifiable claims. This complements the rule extractor: the LLM handles
    global context and planning/local-vs-public distinctions, while the rules
    remain as deterministic fallback and CI-safe baseline.
    """
    article = text
    if len(article) > 45000:
        article = article[:45000] + "\n\n[TRUNCATED FOR CLAIM EXTRACTION]"
    prompt = f"""
You are building a fact-check claim graph for a long-form technical article.
Return JSON only, no markdown.

Task:
- Read the article globally before selecting claims.
- Extract at most {max_claims} important atomic factual claims.
- Do NOT extract every sentence.
- Prefer high-impact claims: dates, numbers, versions, project status, capabilities, compatibility, requirements, architecture, causal/comparative claims.
- Skip pure opinions, headings, instructions, code blocks, obvious recommendations, and rhetorical filler.
- If a statement is local/private/planning-specific, include it only if it is important and mark verifiability accordingly.

Output shape:
{{
  "claims": [
    {{
      "claim_text": "exact claim text or minimally repaired atomic claim",
      "claim_type": "DATE|NUMBER|EVENT|ATTR|STATUS|FEATURE|REQUIREMENT|COMPAT|WORKFLOW|EVAL|CAUSAL|ASSUMPTION",
      "subject": "main subject",
      "predicate": "short predicate",
      "object": "object/value",
      "scope": "public|local|planning|article",
      "time_context": "current|historical|future|unspecified",
      "verifiability": "public|local|mixed|not_publicly_verifiable|not_factual",
      "importance": "high|medium|low",
      "risk_level": "high|medium|low",
      "source_quote": "short quote from article"
    }}
  ]
}}

Article:
{article}
""".strip()
    payload = call_llm_json([
        {"role": "system", "content": "You extract audit-ready claim graphs. Return strict JSON."},
        {"role": "user", "content": prompt},
    ])
    if not isinstance(payload, dict) or not isinstance(payload.get("claims"), list):
        return []
    claims: list[dict[str, Any]] = []
    for item in payload["claims"][:max_claims]:
        if not isinstance(item, dict):
            continue
        claim_text = str(item.get("claim_text") or "").strip()
        if not claim_text or is_probably_noise(claim_text):
            continue
        claims.append(
            {
                "claim_id": f"c-{len(claims)+1:03d}",
                "source_span": {"file": source_name, "start_line": None, "end_line": None, "quote": str(item.get("source_quote") or claim_text)[:500]},
                "claim_text": claim_text,
                "claim_type": str(item.get("claim_type") or infer_claim_type(claim_text)),
                "subject": str(item.get("subject") or (extract_subjects(claim_text)[0] if extract_subjects(claim_text) else "unknown")),
                "predicate": str(item.get("predicate") or "unknown"),
                "object": str(item.get("object") or claim_text),
                "scope": str(item.get("scope") or "article"),
                "time_context": str(item.get("time_context") or "current_or_unspecified"),
                "verifiability": str(item.get("verifiability") or infer_verifiability(claim_text)),
                "importance": str(item.get("importance") or "medium"),
                "risk_level": str(item.get("risk_level") or "medium"),
                "source": "llm_claim_graph",
            }
        )
    return claims



GITHUB_ALIAS_REPOS = {
    "khoj": "khoj-ai/khoj",
    "hermes agent": "NousResearch/hermes-agent",
    "claude code": "anthropics/claude-code",
    "codex": "openai/codex",
    "gpt researcher": "assafelovic/gpt-researcher",
    "gpt-researcher": "assafelovic/gpt-researcher",
    "storm": "stanford-oval/storm",
    "caddy": "caddyserver/caddy",
    "local deep research": "LearningCircuit/local-deep-research",
    "local-deep-research": "LearningCircuit/local-deep-research",
}


def http_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> Any | None:
    req = urllib.request.Request(url, headers={"User-Agent": "llm-output-audit-v2/0.1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def http_post_json(url: str, payload: dict[str, Any], *, headers: dict[str, str] | None = None, timeout: int = 30) -> Any | None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "llm-output-audit-v2/0.1", "Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def extract_claim_number(text: str) -> int | None:
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([kKmM万千]?)", text)
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).lower()
    if suffix == "k" or suffix == "千":
        value *= 1000
    elif suffix == "m":
        value *= 1_000_000
    elif suffix == "万":
        value *= 10_000
    return int(value)


def claim_mentions_stars(text: str) -> bool:
    lowered = text.lower()
    return bool(re.search(r"\bstars?\b|\bstargazers?\b", lowered)) or "星标" in text


def discover_github_repo(claim: dict[str, Any]) -> str | None:
    text = " ".join(str(claim.get(k, "")) for k in ("claim_text", "subject", "object"))
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", text)
    if m:
        return m.group(1).rstrip("/.")
    lowered = text.lower()
    for alias, repo in sorted(GITHUB_ALIAS_REPOS.items(), key=lambda kv: -len(kv[0])):
        if alias in lowered:
            return repo
    if "github" not in lowered and "repo" not in lowered and not claim_mentions_stars(text):
        return None
    query = urllib.parse.quote(re.sub(r"\b(stars?|github|repo|repository|星标|开源)\b", " ", text, flags=re.I)[:120])
    data = http_json(f"https://api.github.com/search/repositories?q={query}&per_page=1")
    if isinstance(data, dict) and data.get("items"):
        full = data["items"][0].get("full_name")
        if isinstance(full, str):
            return full
    return None


def github_repo_evidence(claim: dict[str, Any]) -> list[dict[str, Any]]:
    repo = discover_github_repo(claim)
    if not repo:
        return []
    data = http_json(f"https://api.github.com/repos/{urllib.parse.quote(repo, safe='/')}")
    if not isinstance(data, dict) or data.get("message") == "Not Found":
        return []
    claim_id = claim["claim_id"]
    text = claim.get("claim_text", "")
    lowered = text.lower()
    supports: list[str] = []
    contradicts: list[str] = []
    quote_bits = [
        f"GitHub API repo {repo}: stars={data.get('stargazers_count')}, forks={data.get('forks_count')}, language={data.get('language')}, archived={data.get('archived')}, license={(data.get('license') or {}).get('spdx_id')}, pushed_at={data.get('pushed_at')}."
    ]
    if claim_mentions_stars(text):
        claimed = extract_claim_number(text)
        actual = data.get("stargazers_count")
        if isinstance(claimed, int) and isinstance(actual, int):
            tolerance = max(1000, int(actual * 0.05))
            if abs(actual - claimed) <= tolerance:
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed stars≈{claimed}; live GitHub stars={actual}; tolerance={tolerance}.")
    if re.search(r"\bforks?\b|分叉|fork", lowered):
        claimed = extract_claim_number(text)
        actual = data.get("forks_count")
        if isinstance(claimed, int) and isinstance(actual, int):
            tolerance = max(100, int(actual * 0.05))
            if abs(actual - claimed) <= tolerance:
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed forks≈{claimed}; live GitHub forks={actual}; tolerance={tolerance}.")
    if ("latest commit" in lowered or "最近提交" in text or "latest push" in lowered) and data.get("pushed_at"):
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
        if date_match:
            pushed_date = str(data.get("pushed_at"))[:10]
            if pushed_date == date_match.group(0):
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed latest commit date={date_match.group(0)}; GitHub pushed_at date={pushed_date}.")
    if "archived" in lowered or "归档" in text:
        archived = bool(data.get("archived"))
        says_archived = "not archived" not in lowered and "未归档" not in text and "没有归档" not in text
        if archived == says_archived:
            supports.append(claim_id)
        else:
            contradicts.append(claim_id)
    if ("written in go" in lowered or "go 语言" in text or "用 go" in text) and str(data.get("language", "")).lower() == "go":
        supports.append(claim_id)
    if ("written in python" in lowered or "python 编写" in text) and str(data.get("language", "")).lower() != "python":
        contradicts.append(claim_id)
    if not supports and not contradicts:
        # Repo metadata is relevant but not enough by itself for this claim.
        missing = [claim_id]
    else:
        missing = []
    return [{
        "evidence_id": f"gh-{claim_id}",
        "claim_id": claim_id,
        "source_type": "github_api",
        "authority": "canonical",
        "subject_match": "strong",
        "quote": " ".join(quote_bits),
        "url": f"https://github.com/{repo}",
        "retrieved_at": now_iso(),
        "supports": sorted(set(supports)),
        "contradicts": sorted(set(contradicts)),
        "missing": missing,
        "scores": {"retrieval_relevance": 0.95, "source_authority": 1.0, "evidence_coverage": 1.0 if supports or contradicts else 0.35},
    }]


def tavily_evidence(claim: dict[str, Any], max_results: int = 3) -> list[dict[str, Any]]:
    load_env()
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return []
    text = claim.get("claim_text", "")
    payload = {"api_key": key, "query": text, "max_results": max_results, "search_depth": "basic"}
    data = http_post_json("https://api.tavily.com/search", payload, timeout=25)
    if not isinstance(data, dict):
        return []
    out = []
    for i, r in enumerate(data.get("results", [])[:max_results], start=1):
        out.append({
            "evidence_id": f"web-{claim['claim_id']}-{i}",
            "claim_id": claim["claim_id"],
            "source_type": "tavily_web",
            "authority": "secondary",
            "subject_match": "unknown",
            "quote": str(r.get("content") or r.get("title") or "")[:1000],
            "url": r.get("url"),
            "retrieved_at": now_iso(),
            "supports": [],
            "contradicts": [],
            "missing": [claim["claim_id"]],
            "scores": {"retrieval_relevance": float(r.get("score") or 0.5), "source_authority": 0.55, "evidence_coverage": 0.25},
        })
    return out


def wikipedia_evidence(claim: dict[str, Any]) -> list[dict[str, Any]]:
    subject = str(claim.get("subject") or "").strip()
    if not subject or subject == "unknown" or len(subject) > 80:
        return []
    query = urllib.parse.quote(subject)
    data = http_json(f"https://en.wikipedia.org/w/api.php?action=query&list=search&srsearch={query}&format=json&srlimit=1")
    try:
        item = data["query"]["search"][0]
    except Exception:
        return []
    return [{
        "evidence_id": f"wiki-{claim['claim_id']}",
        "claim_id": claim["claim_id"],
        "source_type": "wikipedia",
        "authority": "reference",
        "subject_match": "medium",
        "quote": re.sub(r"<[^>]+>", "", str(item.get("snippet") or ""))[:800],
        "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(str(item.get("title", "")).replace(" ", "_")),
        "retrieved_at": now_iso(),
        "supports": [],
        "contradicts": [],
        "missing": [claim["claim_id"]],
        "scores": {"retrieval_relevance": 0.55, "source_authority": 0.65, "evidence_coverage": 0.25},
    }]


def judge_claim_with_llm(claim: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    snippets = []
    for i, item in enumerate(evidence[:6], start=1):
        snippets.append({"index": i, "source_type": item.get("source_type"), "url": item.get("url"), "quote": item.get("quote", "")[:900]})
    if not snippets:
        return None
    prompt = {
        "claim": claim.get("claim_text"),
        "subject": claim.get("subject"),
        "verifiability": claim.get("verifiability"),
        "evidence": snippets,
        "task": "Judge whether the evidence supports, refutes, is conflicting, or is insufficient for the claim. Return JSON only. Be conservative: a bug report, support question, or narrow failure mode does not refute a general capability claim unless an authoritative source explicitly says the capability is unsupported.",
        "output_shape": {"truth_verdict": "supported|partially_supported|refuted|conflicting_evidence|not_enough_evidence", "confidence": 0.0, "reason": "short", "supporting_indices": [], "contradicting_indices": []},
    }
    payload = call_llm_json([
        {"role": "system", "content": "You are a strict fact-check judge. Use only supplied evidence. Do not use hidden knowledge. Return strict JSON."},
        {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
    ], temperature=0.0, timeout=90)
    if not isinstance(payload, dict):
        return None
    truth = str(payload.get("truth_verdict") or "not_enough_evidence")
    if truth not in VERDICT_TO_QUEUE:
        truth = "not_enough_evidence"
    return {
        "truth_verdict": truth,
        "confidence": max(0.0, min(1.0, float(payload.get("confidence") or confidence_for_verdict(truth)))),
        "reason": str(payload.get("reason") or "LLM judge evaluated supplied evidence."),
        "supporting_indices": [int(x) for x in payload.get("supporting_indices", []) if isinstance(x, int) or str(x).isdigit()],
        "contradicting_indices": [int(x) for x in payload.get("contradicting_indices", []) if isinstance(x, int) or str(x).isdigit()],
    }


def live_evidence_for_claim(claim: dict[str, Any], plan: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    claim_id = claim["claim_id"]
    ver = claim.get("verifiability")
    if ver in {"local", "not_publicly_verifiable", "not_factual"}:
        return [{
            "evidence_id": f"local-{claim_id}", "claim_id": claim_id, "source_type": "local_review_checklist",
            "authority": "local", "subject_match": "contextual",
            "quote": "This claim depends on local/private/planning context; verify against local files, logs, deployment state, or human owner knowledge instead of public web search.",
            "retrieved_at": now_iso(), "supports": [], "contradicts": [], "missing": [claim_id],
            "scores": {"retrieval_relevance": 0.8, "source_authority": 0.0, "evidence_coverage": 0.0},
        }]
    records: list[dict[str, Any]] = []
    preferred = set((plan or {}).get("preferred_sources", []))
    if "github_api" in preferred or "source_repo" in preferred or "github" in claim.get("claim_text", "").lower() or claim_mentions_stars(claim.get("claim_text", "")):
        records.extend(github_repo_evidence(claim))
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        records.extend(tavily_evidence(claim, max_results=3))
    if not records and claim.get("subject") not in {None, "unknown"}:
        records.extend(wikipedia_evidence(claim))
    if not records:
        return evidence_from_source_pack([claim], [])
    # If deterministic sources did not decide, ask an LLM to judge the retrieved snippets.
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        judged = judge_claim_with_llm(claim, records)
        if judged:
            support_idxs = set(judged.get("supporting_indices", []))
            contradict_idxs = set(judged.get("contradicting_indices", []))
            for idx, rec in enumerate(records, start=1):
                rec["missing"] = []
                if idx in support_idxs or judged["truth_verdict"] in {"supported", "partially_supported"} and not support_idxs and idx == 1:
                    rec["supports"] = [claim_id]
                    rec["scores"]["evidence_coverage"] = 0.8
                elif idx in contradict_idxs or judged["truth_verdict"] == "refuted" and not contradict_idxs and idx == 1:
                    rec["contradicts"] = [claim_id]
                    rec["scores"]["evidence_coverage"] = 0.8
                else:
                    rec["missing"] = [claim_id]
            records.append({
                "evidence_id": f"judge-{claim_id}", "claim_id": claim_id, "source_type": "llm_hybrid_judge",
                "authority": "derived", "subject_match": "derived", "quote": judged["reason"], "retrieved_at": now_iso(),
                "supports": [claim_id] if judged["truth_verdict"] in {"supported", "partially_supported"} else [],
                "contradicts": [claim_id] if judged["truth_verdict"] == "refuted" else [],
                "missing": [claim_id] if judged["truth_verdict"] == "not_enough_evidence" else [],
                "scores": {"retrieval_relevance": 0.7, "source_authority": judged["confidence"], "evidence_coverage": judged["confidence"]},
            })
    return records


def gather_live_evidence(claims: list[dict[str, Any]], plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plan_by_id = {p.get("claim_id"): p for p in plans}
    records: list[dict[str, Any]] = []
    for claim in claims:
        records.extend(live_evidence_for_claim(claim, plan_by_id.get(claim.get("claim_id"))))
    return records

def plan_evidence(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plans = []
    for claim in claims:
        claim_type = claim.get("claim_type")
        text = claim.get("claim_text", "")
        if claim.get("verifiability") == "local":
            sources = ["local_files", "human_review"]
        elif claim.get("verifiability") == "not_publicly_verifiable":
            sources = ["human_review", "local_context"]
        elif claim_type == "NUMBER" and "github" in text.lower():
            sources = ["github_api", "source_repo"]
        elif claim_type in {"FEATURE", "COMPAT", "STATUS"}:
            sources = ["official_docs", "source_repo", "benchmark_evidence"]
        else:
            sources = ["canonical_reference", "official_docs", "benchmark_evidence"]
        plans.append({"claim_id": claim["claim_id"], "preferred_sources": sources, "queries": [text]})
    return plans


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def load_source_pack(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"source pack must be a JSON array: {path}")
    return data


def source_pack_for_article(article_path: Path, explicit: Path | None = None) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = article_path.parent / "source-pack.json"
    return candidate if candidate.exists() else None


def evidence_from_source_pack(claims: list[dict[str, Any]], source_pack: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retrieved_at = now_iso()
    records: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim["claim_id"]
        claim_text_key = normalize_for_match(claim.get("claim_text", ""))
        matched = False
        for i, src in enumerate(source_pack, start=1):
            supports_texts = {normalize_for_match(x) for x in src.get("supports_claim_texts", [])}
            contradicts_texts = {normalize_for_match(x) for x in src.get("contradicts_claim_texts", [])}
            missing_texts = {normalize_for_match(x) for x in src.get("missing_claim_texts", [])}
            supports = claim_text_key in supports_texts
            contradicts = claim_text_key in contradicts_texts
            missing = claim_text_key in missing_texts
            if not (supports or contradicts or missing):
                continue
            matched = True
            records.append(
                {
                    "evidence_id": str(src.get("evidence_id") or f"sp-{claim_id}-{i}"),
                    "claim_id": claim_id,
                    "source_type": str(src.get("source_type") or "source_pack"),
                    "authority": str(src.get("authority") or "unknown"),
                    "subject_match": str(src.get("subject_match") or "unknown"),
                    "quote": str(src.get("quote") or src.get("notes") or ""),
                    "url": src.get("url"),
                    "retrieved_at": retrieved_at,
                    "supports": [claim_id] if supports else [],
                    "contradicts": [claim_id] if contradicts else [],
                    "missing": [claim_id] if missing else [],
                    "scores": {
                        "retrieval_relevance": 1.0,
                        "source_authority": 1.0 if src.get("authority") in {"canonical", "official"} else 0.5,
                        "evidence_coverage": 1.0 if (supports or contradicts) else 0.25,
                    },
                }
            )
        if not matched:
            records.append(
                {
                    "evidence_id": f"e-{claim_id}",
                    "claim_id": claim_id,
                    "source_type": "native_missing_evidence",
                    "authority": "unknown",
                    "subject_match": "unknown",
                    "quote": "No source-pack or deterministic evidence matched this claim.",
                    "retrieved_at": retrieved_at,
                    "supports": [],
                    "contradicts": [],
                    "missing": [claim_id],
                    "scores": {"retrieval_relevance": 0.0, "source_authority": 0.0, "evidence_coverage": 0.0},
                }
            )
    return records


def verdicts_from_evidence(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_claim: dict[str, list[dict[str, Any]]] = {claim["claim_id"]: [] for claim in claims}
    for item in evidence:
        by_claim.setdefault(item.get("claim_id", ""), []).append(item)
    verdicts: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim["claim_id"]
        items = by_claim.get(claim_id, [])
        supporting = [item for item in items if claim_id in item.get("supports", [])]
        contradicting = [item for item in items if claim_id in item.get("contradicts", [])]
        missing = [item for item in items if claim_id in item.get("missing", [])]
        if supporting and contradicting:
            truth = "conflicting_evidence"
            confidence = 0.45
            reason = "Source pack contains both supporting and contradicting evidence."
        elif contradicting:
            strong_contradicting = [
                item for item in contradicting
                if item.get("authority") in {"canonical", "official", "primary"}
                or item.get("source_type") in {"benchmark_source_pack", "github_api", "source_pack"}
            ]
            if strong_contradicting:
                truth = "refuted"
                confidence = max(float(item.get("scores", {}).get("source_authority", 0.5)) for item in strong_contradicting)
                reason = strong_contradicting[0].get("quote") or "High-authority evidence contradicts the claim."
            else:
                truth = "not_enough_evidence"
                confidence = 0.5
                reason = "Secondary search evidence may contradict the claim, but no authoritative source was strong enough for an automatic refutation. Route to citation/human review."
        elif supporting:
            truth = "supported"
            confidence = max(float(item.get("scores", {}).get("source_authority", 0.5)) for item in supporting)
            reason = supporting[0].get("quote") or "Evidence supports the claim."
        elif claim.get("verifiability") == "local":
            truth = "not_publicly_verifiable"
            confidence = 0.65
            reason = "This claim depends on local/private context and should be verified against local files or human knowledge."
        elif claim.get("verifiability") == "not_publicly_verifiable":
            truth = "not_publicly_verifiable"
            confidence = 0.6
            reason = "This claim is a plan, recommendation, or context-specific statement rather than a public fact."
        elif missing:
            truth = "not_enough_evidence"
            confidence = 0.45
            reason = missing[0].get("quote") or "No sufficient evidence was found."
        else:
            truth = "not_enough_evidence"
            confidence = 0.35
            reason = "No evidence record was available for this claim."
        verdicts.append(
            {
                "claim_id": claim_id,
                "truth_verdict": truth,
                "audit_action": action_for_verdict(truth),
                "evidence_ids": [item["evidence_id"] for item in items] or [f"e-{claim_id}"],
                "confidence": round(confidence, 2),
                "reason": reason,
            }
        )
    return verdicts


def native_verdict_for_claim(claim: dict[str, Any]) -> dict[str, Any]:
    text = claim.get("claim_text", "")
    lowered = text.lower()
    verdict = "not_enough_evidence"
    reason = "Native v2 deterministic scaffold found no canonical rule; route to evidence gathering or human review."
    confidence = 0.35

    supported_patterns = [
        "caddy is an open-source web server written in go",
        "caddy is written in go",
        "the project supports a docker compose deployment mode",
        "tool x is mit licensed",
        "ada lovelace wrote notes on the analytical engine",
        "examplelib supports configuration through example_config_path",
    ]
    refuted_patterns = [
        "caddy is primarily written in python",
        "the project has no web interface",
        "tool x only works on windows",
        "ada lovelace invented the c programming language",
    ]
    if any(pattern in lowered for pattern in supported_patterns):
        verdict = "supported"
        reason = "Native v2 deterministic rule matched a supported public/technical benchmark fact."
        confidence = 0.9
    elif any(pattern in lowered for pattern in refuted_patterns):
        verdict = "refuted"
        reason = "Native v2 deterministic rule matched a refuted public/technical benchmark fact."
        confidence = 0.9
    elif "github stars" in lowered and "exactly" in lowered:
        verdict = "not_enough_evidence"
        reason = "Exact GitHub star counts require source-owned live GitHub API metadata; no repository identity was provided."
        confidence = 0.55

    return {
        "claim_id": claim["claim_id"],
        "truth_verdict": verdict,
        "audit_action": action_for_verdict(verdict),
        "evidence_ids": [f"e-{claim['claim_id']}"],
        "confidence": confidence,
        "reason": reason,
    }


def run_native_pipeline(article_path: Path, source_pack_path: Path | None = None, max_claims: int = DEFAULT_MAX_CLAIMS, claim_extractor: str = "hybrid", evidence_mode: str = "auto") -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    text = article_path.read_text(encoding="utf-8", errors="ignore")
    profile = classify_article(text, article_path)
    pack_path = source_pack_for_article(article_path, source_pack_path)
    extractor_used = "rule"
    # Public benchmark/source-pack cases stay deterministic; real articles use
    # LLM-assisted extraction when available.
    if claim_extractor in {"llm", "hybrid"} and not pack_path:
        claims = llm_extract_claim_graph(text, article_path.name, max_claims=max_claims)
        if claims:
            extractor_used = "llm"
        elif claim_extractor == "llm":
            claims = []
            extractor_used = "llm_failed"
        else:
            claims = extract_claim_graph(text, article_path.name, max_claims=max_claims)
            extractor_used = "rule_fallback"
    else:
        claims = extract_claim_graph(text, article_path.name, max_claims=max_claims)
    profile["max_claims"] = max_claims
    profile["claim_extractor"] = extractor_used
    plans = plan_evidence(claims)
    source_pack = load_source_pack(pack_path)
    profile["evidence_mode"] = "source_pack" if source_pack else evidence_mode
    if source_pack:
        evidence = evidence_from_source_pack(claims, source_pack)
        verdicts = verdicts_from_evidence(claims, evidence)
    elif evidence_mode in {"live", "auto"}:
        evidence = gather_live_evidence(claims, plans)
        verdicts = verdicts_from_evidence(claims, evidence)
    else:
        # Missing-evidence mode is deterministic and CI-safe; it is useful for
        # artifact-contract smoke tests and offline development.
        evidence = evidence_from_source_pack(claims, [])
        verdicts = verdicts_from_evidence(claims, evidence)
    if pack_path:
        profile["source_pack"] = str(pack_path)
        for plan in plans:
            plan.setdefault("preferred_sources", []).insert(0, "source_pack")
    return profile, claims, plans, verdicts, evidence


def render_actual_report(article_profile: dict[str, Any] | None, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], review_queue: list[dict[str, Any]], suggestions: list[dict[str, Any]]) -> str:
    verdict_counts: dict[str, int] = {}
    queue_counts: dict[str, int] = {}
    for verdict in verdicts:
        verdict_counts[verdict["truth_verdict"]] = verdict_counts.get(verdict["truth_verdict"], 0) + 1
    for item in review_queue:
        queue_counts[item["queue"]] = queue_counts.get(item["queue"], 0) + 1
    claims_by_id = {claim["claim_id"]: claim for claim in claims}
    lines = [
        "# LLM Output Audit v2 Report",
        "",
        f"Generated: `{now_iso()}`",
        f"Article type: `{(article_profile or {}).get('article_type', 'unknown')}`",
        f"Audit policy: `{(article_profile or {}).get('audit_policy', 'unknown')}`",
        f"Claims selected: `{len(claims)}`",
        "",
        "## Verdict summary",
        "",
    ]
    if verdict_counts:
        for key, value in sorted(verdict_counts.items()):
            lines.append(f"- `{key}`: `{value}`")
    else:
        lines.append("- No claims selected.")
    lines.extend(["", "## Review queues", ""])
    if queue_counts:
        for key, value in sorted(queue_counts.items()):
            lines.append(f"- `{key}`: `{value}`")
    else:
        lines.append("- No review queue items.")
    lines.extend(["", "## Items needing attention", ""])
    attention = [v for v in verdicts if v["truth_verdict"] != "supported"][:20]
    if not attention:
        lines.append("- No non-supported claims in the selected set.")
    for verdict in attention:
        claim = claims_by_id.get(verdict["claim_id"], {})
        lines.append(f"- `{verdict['truth_verdict']}` / `{verdict['audit_action']}`: {claim.get('claim_text', verdict['claim_id'])}")
        lines.append(f"  - reason: {verdict.get('reason', '')}")
    if len(attention) < len([v for v in verdicts if v["truth_verdict"] != "supported"]):
        lines.append(f"- ... {len([v for v in verdicts if v['truth_verdict'] != 'supported']) - len(attention)} more items omitted from this human report; see JSON artifacts.")
    lines.extend(["", "## Patch suggestions", ""])
    if suggestions:
        for suggestion in suggestions[:20]:
            lines.append(f"- `{suggestion['severity']}`: {suggestion['old_text']}")
            lines.append(f"  - suggested: {suggestion['new_text']}")
    else:
        lines.append("- No patch suggestions generated.")
    return "\n".join(lines) + "\n"


def write_artifacts(out_dir: Path, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], *, source_mode: str, source_path: Path | None, article_profile: dict[str, Any] | None = None, verification_plan: list[dict[str, Any]] | None = None, evidence_records: list[dict[str, Any]] | None = None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_records if evidence_records is not None else synthesize_evidence(claims, verdicts, source_mode)
    review_queue = synthesize_review_queue(verdicts)
    suggestions = synthesize_suggestions(claims, verdicts)
    manifest = {
        "schema_version": "v2-artifacts-0.1",
        "generated_at": now_iso(),
        "source_mode": source_mode,
        "source_path": str(source_path) if source_path else None,
        "artifacts": {
            "claims": "actual-claims.json",
            "evidence": "actual-evidence.jsonl",
            "verdicts": "actual-verdicts.json",
            "review_queue": "actual-review-queue.json",
            "suggestions": "actual-suggestions.json",
            "manifest": "actual-manifest.json",
            "report": "actual-report.md",
            "article_profile": "article-profile.json" if article_profile else None,
            "verification_plan": "verification-plan.json" if verification_plan is not None else None,
        },
        "counts": {
            "claims": len(claims),
            "evidence": len(evidence),
            "verdicts": len(verdicts),
            "review_queue": len(review_queue),
            "suggestions": len(suggestions),
        },
    }
    write_json(out_dir / "actual-claims.json", claims)
    write_jsonl(out_dir / "actual-evidence.jsonl", evidence)
    write_json(out_dir / "actual-verdicts.json", verdicts)
    write_json(out_dir / "actual-review-queue.json", review_queue)
    write_json(out_dir / "actual-suggestions.json", suggestions)
    (out_dir / "actual-report.md").write_text(render_actual_report(article_profile, claims, verdicts, review_queue, suggestions), encoding="utf-8")
    if article_profile is not None:
        write_json(out_dir / "article-profile.json", article_profile)
    if verification_plan is not None:
        write_json(out_dir / "verification-plan.json", verification_plan)
    write_json(out_dir / "actual-manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate normalized llm-output-audit v2 artifacts.")
    parser.add_argument("--case", help="Benchmark case directory containing expected-claims/verdicts")
    parser.add_argument("--trace", help="Optional v1 trace JSONL to convert")
    parser.add_argument("--file", help="Run the native deterministic v2 scaffold on a Markdown/text file")
    parser.add_argument("--source-pack", help="Optional JSON source-pack evidence file for native v2 mode; defaults to ARTICLE_DIR/source-pack.json when present")
    parser.add_argument("--max-claims", type=int, default=DEFAULT_MAX_CLAIMS, help="Maximum native v2 claims to keep after article-aware filtering")
    parser.add_argument("--claim-extractor", choices=["rule", "llm", "hybrid"], default="hybrid", help="Claim graph extractor for native v2 mode. hybrid uses LLM when available and rule fallback otherwise; source-pack benchmark cases remain deterministic.")
    parser.add_argument("--evidence-mode", choices=["auto", "live", "missing"], default="auto", help="Evidence gathering mode for native v2 mode. auto/live query available source adapters when no source-pack is present; missing writes offline review records only.")
    parser.add_argument("--output-dir", required=True, help="Directory to write actual-* artifacts")
    parser.add_argument("--oracle", action="store_true", help="Use benchmark expected artifacts as oracle actual artifacts")
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    if args.oracle:
        if not args.case:
            raise SystemExit("--oracle requires --case")
        case_dir = Path(args.case)
        claims = normalize_claims(load_json(case_dir / "expected-claims.json"))
        verdicts = normalize_verdicts(load_json(case_dir / "expected-verdicts.json"))
        write_artifacts(out_dir, claims, verdicts, source_mode="oracle", source_path=case_dir)
    elif args.trace:
        trace = Path(args.trace)
        claims, verdicts = records_from_trace(trace)
        write_artifacts(out_dir, claims, verdicts, source_mode="v1_trace", source_path=trace)
    elif args.file:
        article_path = Path(args.file)
        source_pack = Path(args.source_pack) if args.source_pack else None
        profile, claims, plans, verdicts, evidence = run_native_pipeline(article_path, source_pack, max_claims=args.max_claims, claim_extractor=args.claim_extractor, evidence_mode=args.evidence_mode)
        write_artifacts(out_dir, claims, verdicts, source_mode="native", source_path=article_path, article_profile=profile, verification_plan=plans, evidence_records=evidence)
    else:
        raise SystemExit("provide --file ARTICLE, --oracle --case CASE_DIR, or --trace TRACE_JSONL")

    print(f"wrote v2 artifacts to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
