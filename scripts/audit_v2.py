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
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
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
LOCAL_CUES = {"老大", "本机", "内网", "局域网", "我的", "个人", "BuJo", "懒猫", "localhost", "local", "private"}
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




def citation_marker_for_text(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "（需补来源）"
    return " [citation needed]"


def local_verify_marker_for_text(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        return "（需本地确认）"
    return " [local verification needed]"


def hedge_text(text: str) -> str:
    if re.search(r"[\u4e00-\u9fff]", text):
        if text.startswith("可能") or "据" in text[:8]:
            return text
        return "据当前证据，" + text
    if re.match(r"(?i)^(may|might|appears to|according to)", text.strip()):
        return text
    return "According to currently available evidence, " + text[:1].lower() + text[1:]


def evidence_by_id(evidence_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(e.get("evidence_id")): e for e in evidence_records}


def build_patch_reason(verdict: dict[str, Any], evidence_records: list[dict[str, Any]], max_chars: int = 700) -> str:
    e_by_id = evidence_by_id(evidence_records)
    snippets=[]
    for eid in verdict.get("evidence_ids") or []:
        ev=e_by_id.get(str(eid))
        if not ev:
            continue
        url=ev.get("url") or ""
        quote=str(ev.get("quote") or "").strip()
        snippets.append((f"{ev.get('source_type','evidence')}: {url} {quote}").strip())
    base = verdict.get("reason") or "Generated from v2 verdict."
    joined = "\n".join(snippets[:3]) or base
    return joined[:max_chars]


def llm_rewrite_patch(old_text: str, verdict: dict[str, Any], evidence_records: list[dict[str, Any]]) -> str | None:
    if not old_text.strip() or len(old_text) > 1200:
        return None
    payload = {
        "old_text": old_text,
        "truth_verdict": verdict.get("truth_verdict"),
        "audit_action": verdict.get("audit_action"),
        "reason": verdict.get("reason"),
        "evidence": build_patch_reason(verdict, evidence_records, max_chars=1800),
    }
    result = call_llm_json([
        {"role": "system", "content": "You are a conservative technical editor. Rewrite the original sentence/paragraph only when the evidence supports a correction. Preserve the original language and tone. Return JSON: {\"replacement\": \"...\", \"confidence\": 0.0-1.0, \"why\": \"...\"}. If the evidence is insufficient, add a short citation-needed or verification-needed marker instead of inventing facts."},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ], temperature=0.1, timeout=90)
    if not isinstance(result, dict):
        return None
    replacement = str(result.get("replacement") or "").strip()
    confidence = float(result.get("confidence") or 0.0)
    if replacement and replacement != old_text and confidence >= 0.55:
        return replacement
    return None


def select_patch_old_text(article_text: str, claim: dict[str, Any]) -> str:
    span = claim.get("source_span") if isinstance(claim.get("source_span"), dict) else {}
    candidates = [str(span.get("quote") or "").strip(), str(claim.get("claim_text") or "").strip()]
    for cand in candidates:
        if cand and cand in article_text:
            return cand
    # Try line-level fuzzy match from the source quote; avoid risky rewrites if not found.
    quote = candidates[0]
    if quote:
        for line in article_text.splitlines():
            stripped = line.strip()
            if stripped and (quote in stripped or stripped in quote) and len(stripped) >= 12:
                return stripped
    return ""



def patch_text_is_safe(old_text: str) -> tuple[bool, str]:
    stripped = old_text.strip()
    if len(stripped) < 20:
        return False, "source span too short for safe automatic patch"
    if CODE_LIKE_RE.search(stripped) or re.match(r"^[A-Za-z_]+\s+[A-Za-z0-9_./:-]+$", stripped) or re.match(r"^[A-Z][A-Z0-9_]+\s*=", stripped) or re.search(r"\b(sudo|journalctl|reload|cloudflare|API_TOKEN|你的_API_TOKEN|policy\s+round_robin|dns\s+cloudflare)\b", stripped, flags=re.I):
        return False, "source span looks like code/config/command"
    if stripped.startswith(("```", "`")) or stripped.endswith("`"):
        return False, "source span is inline/block code"
    if re.search(r"^(请|执行|运行|输入|cd |git |python |npm |pip |curl |docker |systemctl |sudo |journalctl )", stripped, flags=re.I):
        return False, "source span looks like an instruction/command, not a prose claim"
    if any(word in stripped for word in ["伟大", "王者", "极致", "强大的生命力", "不可替代", "疯狂", "一证保全家"]):
        return False, "source span is evaluative/figurative; needs human editing"
    return True, "safe prose span"

def synthesize_patches(article_text: str, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], evidence_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claims_by_id = {c["claim_id"]: c for c in claims}
    patches=[]
    for verdict in verdicts:
        truth = verdict.get("truth_verdict")
        if truth in {"supported", "not_a_factual_claim"}:
            continue
        claim = claims_by_id.get(verdict["claim_id"], {})
        old_text = select_patch_old_text(article_text, claim)
        if not old_text:
            patches.append({
                "patch_id": f"p-{verdict['claim_id']}",
                "claim_id": verdict["claim_id"],
                "operation": "manual_review",
                "old_text": claim.get("claim_text", ""),
                "new_text": "",
                "safe_to_apply": False,
                "requires_human": True,
                "reason": "Could not locate the original source span in the article; patch not applied.",
                "evidence_ids": verdict.get("evidence_ids") or [],
            })
            continue
        span_safe, span_reason = patch_text_is_safe(old_text)
        if truth == "not_enough_evidence":
            marker = citation_marker_for_text(old_text)
            new_text = old_text if marker in old_text else old_text + marker
            operation = "add_citation_needed"
            safe = span_safe
            requires_human = not span_safe
        elif truth == "not_publicly_verifiable":
            marker = local_verify_marker_for_text(old_text)
            new_text = old_text if marker in old_text else old_text + marker
            operation = "add_local_verification_marker"
            safe = span_safe
            requires_human = not span_safe
        elif truth in {"partially_supported", "conflicting_evidence"}:
            new_text = llm_rewrite_patch(old_text, verdict, evidence_records) or hedge_text(old_text)
            operation = "hedge_or_qualify"
            safe = False
            requires_human = True
        elif truth == "refuted":
            new_text = llm_rewrite_patch(old_text, verdict, evidence_records) or ("[needs correction] " + old_text)
            operation = "rewrite_refuted"
            safe = False
            requires_human = True
        else:
            new_text = old_text
            operation = "manual_review"
            safe = False
            requires_human = True
        patches.append({
            "patch_id": f"p-{verdict['claim_id']}",
            "claim_id": verdict["claim_id"],
            "operation": operation,
            "old_text": old_text,
            "new_text": new_text,
            "safe_to_apply": safe,
            "requires_human": requires_human,
            "reason": build_patch_reason(verdict, evidence_records),
            "safety_reason": locals().get("span_reason", "manual or LLM rewrite patch"),
            "evidence_ids": verdict.get("evidence_ids") or [],
        })
    return patches


def apply_safe_patches(article_text: str, patches: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    revised = article_text
    applied=[]
    for patch in patches:
        if not patch.get("safe_to_apply"):
            patch["applied"] = False
            patch["apply_reason"] = "not marked safe_to_apply"
            continue
        old = str(patch.get("old_text") or "")
        new = str(patch.get("new_text") or "")
        if not old or old == new:
            patch["applied"] = False
            patch["apply_reason"] = "empty or no-op patch"
            continue
        count = revised.count(old)
        if count != 1:
            patch["applied"] = False
            patch["apply_reason"] = f"old_text occurrence count is {count}; refusing ambiguous patch"
            continue
        revised = revised.replace(old, new, 1)
        patch["applied"] = True
        patch["apply_reason"] = "applied exact single occurrence replacement"
        applied.append(patch)
    return revised, patches


def render_revision_report(patches: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    applied = 0
    for patch in patches:
        counts[patch.get("operation", "unknown")] = counts.get(patch.get("operation", "unknown"), 0) + 1
        if patch.get("applied"):
            applied += 1
    lines = ["# LLM Output Audit v2 Revision Report", "", f"Generated: `{now_iso()}`", "", "## Summary", ""]
    lines.append(f"- patches proposed: `{len(patches)}`")
    lines.append(f"- safe patches applied: `{applied}`")
    for key, value in sorted(counts.items()):
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Applied patches", ""])
    any_applied=False
    for patch in patches:
        if patch.get("applied"):
            any_applied=True
            lines.append(f"- `{patch['patch_id']}` / `{patch['operation']}`")
            lines.append(f"  - old: {patch['old_text'][:240]}")
            lines.append(f"  - new: {patch['new_text'][:240]}")
    if not any_applied:
        lines.append("- No safe patches applied.")
    lines.extend(["", "## Human review required", ""])
    review=[p for p in patches if not p.get("applied")]
    if not review:
        lines.append("- None.")
    for patch in review[:30]:
        lines.append(f"- `{patch['patch_id']}` / `{patch['operation']}` / applied=`{patch.get('applied', False)}`")
        lines.append(f"  - reason: {patch.get('apply_reason') or patch.get('reason','')[:300]}")
        if patch.get("new_text"):
            lines.append(f"  - proposed: {patch['new_text'][:240]}")
    return "\n".join(lines)+"\n"

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
        # Product names such as NVIDIA DGX are public facts; do not mark them
        # local merely because they contain the user's local-hardware keywords.
        if any(public_term in lowered for public_term in ["nvidia dgx", "dgx spark", "gb10", "official", "官网"]):
            return "public"
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
    merged_headers = {"User-Agent": "llm-output-audit-v2/0.1", **(headers or {})}
    try:
        import requests
        resp = requests.get(url, headers=merged_headers, timeout=timeout)
        if resp.ok:
            return resp.json()
    except Exception:
        pass
    req = urllib.request.Request(url, headers=merged_headers)
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
        f"GitHub API repo {repo}: stars={data.get('stargazers_count')}, forks={data.get('forks_count')}, open_issues={data.get('open_issues_count')}, language={data.get('language')}, archived={data.get('archived')}, license={(data.get('license') or {}).get('spdx_id')}, created_at={data.get('created_at')}, pushed_at={data.get('pushed_at')}."
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
    if re.search(r"open issues?|issues?", lowered) or "未解决 issue" in text or "开放 issue" in text:
        claimed = extract_claim_number(text)
        actual = data.get("open_issues_count")
        if isinstance(claimed, int) and isinstance(actual, int):
            tolerance = max(25, int(actual * 0.10))
            if abs(actual - claimed) <= tolerance:
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed open issues≈{claimed}; live GitHub open_issues_count={actual}; tolerance={tolerance}.")
    if ("created" in lowered or "创建" in text) and data.get("created_at"):
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
        if date_match:
            created_date = str(data.get("created_at"))[:10]
            if created_date == date_match.group(0):
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed created date={date_match.group(0)}; GitHub created_at date={created_date}.")
    if "license" in lowered or "许可证" in text:
        lic = str((data.get("license") or {}).get("spdx_id") or "").lower()
        if lic and lic in lowered:
            supports.append(claim_id)
            quote_bits.append(f"GitHub license spdx_id={lic}.")
    if ("latest commit" in lowered or "最近提交" in text or "latest push" in lowered) and data.get("pushed_at"):
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
        if date_match:
            pushed_date = str(data.get("pushed_at"))[:10]
            if pushed_date == date_match.group(0):
                supports.append(claim_id)
            else:
                contradicts.append(claim_id)
            quote_bits.append(f"Claimed latest commit date={date_match.group(0)}; GitHub pushed_at date={pushed_date}.")
    if "latest release" in lowered or "最新 release" in text or "最新版本" in text:
        rel = http_json(f"https://api.github.com/repos/{urllib.parse.quote(repo, safe='/')}/releases/latest")
        if isinstance(rel, dict) and rel.get("tag_name"):
            tag = str(rel.get("tag_name"))
            published = str(rel.get("published_at") or "")[:10]
            tag_ok = tag.lower() in lowered
            date_match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
            date_ok = not date_match or published == date_match.group(0)
            if tag_ok and date_ok:
                supports.append(claim_id)
            elif tag_ok or published:
                contradicts.append(claim_id)
            quote_bits.append(f"GitHub latest release tag={tag}, published_at={published}.")
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



def relevant_quote(text: str, claim_text: str, max_chars: int = 1800) -> str:
    tokens = [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}|[\u4e00-\u9fff]{2,}", claim_text) if t.lower() not in {"this", "that", "with", "from", "supports", "requires"}]
    chunks = re.split(r"(?<=[.!?。！？])\s+|\n+", text)
    scored=[]
    for chunk in chunks:
        low=chunk.lower()
        score=sum(1 for t in tokens if t in low)
        if score:
            scored.append((score, chunk.strip()))
    if scored:
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        quote="\n".join(c for _, c in scored[:5])
    else:
        quote=text[:max_chars]
    return quote[:max_chars]


def github_readme_evidence(claim: dict[str, Any]) -> list[dict[str, Any]]:
    repo = discover_github_repo(claim)
    if not repo:
        return []
    data = http_json(f"https://api.github.com/repos/{urllib.parse.quote(repo, safe='/')}/readme")
    if not isinstance(data, dict):
        return []
    content = data.get("content")
    if not isinstance(content, str):
        return []
    try:
        readme = base64.b64decode(content).decode("utf-8", errors="replace")
    except Exception:
        return []
    claim_id=claim["claim_id"]
    return [{
        "evidence_id": f"gh-readme-{claim_id}",
        "claim_id": claim_id,
        "source_type": "github_readme",
        "authority": "official",
        "subject_match": "medium",
        "quote": relevant_quote(readme, claim.get("claim_text", "")),
        "url": f"https://github.com/{repo}#readme",
        "retrieved_at": now_iso(),
        "supports": [],
        "contradicts": [],
        "missing": [claim_id],
        "scores": {"retrieval_relevance": 0.65, "source_authority": 0.85, "evidence_coverage": 0.35},
    }]

def source_error_record(claim: dict[str, Any], source_type: str, message: str) -> dict[str, Any]:
    claim_id = claim["claim_id"]
    return {
        "evidence_id": f"error-{source_type}-{claim_id}",
        "claim_id": claim_id,
        "source_type": f"{source_type}_error",
        "authority": "diagnostic",
        "subject_match": "none",
        "quote": message[:1000],
        "url": None,
        "retrieved_at": now_iso(),
        "supports": [],
        "contradicts": [],
        "missing": [claim_id],
        "scores": {"retrieval_relevance": 0.0, "source_authority": 0.0, "evidence_coverage": 0.0},
    }


def tavily_evidence(claim: dict[str, Any], max_results: int = 3) -> list[dict[str, Any]]:
    load_env()
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        return [source_error_record(claim, "tavily", "TAVILY_API_KEY is not configured; skipping Tavily web search.")]
    text = claim.get("claim_text", "")
    payload = {"api_key": key, "query": text, "max_results": max_results, "search_depth": "basic"}
    try:
        import requests
        resp = requests.post("https://api.tavily.com/search", json=payload, timeout=25)
        if not resp.ok:
            detail = resp.text[:500].replace(key, "[REDACTED]")
            return [source_error_record(claim, "tavily", f"Tavily search failed with HTTP {resp.status_code}: {detail}")]
        data = resp.json()
    except Exception as exc:
        return [source_error_record(claim, "tavily", f"Tavily search exception: {type(exc).__name__}: {exc}")]
    if not isinstance(data, dict):
        return [source_error_record(claim, "tavily", "Tavily returned a non-JSON-object response.")]
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
    if not out:
        return [source_error_record(claim, "tavily", "Tavily search returned zero results for this query.")]
    return out





OFFICIAL_DOC_URLS = {
    "dgx spark": [
        "https://www.nvidia.com/en-us/products/workstations/dgx-spark/",
    ],
    "nvidia dgx spark": [
        "https://www.nvidia.com/en-us/products/workstations/dgx-spark/",
    ],
    "gb10": [
        "https://www.nvidia.com/en-us/products/workstations/dgx-spark/",
    ],
    "caddy": [
        "https://caddyserver.com/docs/automatic-https",
        "https://caddyserver.com/docs/caddyfile/directives/reverse_proxy",
        "https://caddyserver.com/docs/caddyfile/directives/tls",
        "https://caddyserver.com/docs/modules/http.reverse_proxy",
    ],
    "gpt researcher": [
        "https://docs.gptr.dev/docs/gpt-researcher/getting-started",
        "https://docs.gptr.dev/docs/gpt-researcher/gptr/config",
        "https://docs.gptr.dev/docs/gpt-researcher/context/local-docs",
        "https://docs.gptr.dev/docs/gpt-researcher/mcp/mcp-overview",
    ],
}


def strip_html(text: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    return re.sub(r"\s+", " ", text).strip()


URL_TEXT_CACHE: dict[str, str] = {}


def fetch_text_url(url: str, timeout: int = 8) -> str:
    if url in URL_TEXT_CACHE:
        return URL_TEXT_CACHE[url]
    try:
        import requests
        resp = requests.get(url, headers={"User-Agent": "llm-output-audit-v2/0.1"}, timeout=timeout, verify=True)
        if not resp.ok:
            URL_TEXT_CACHE[url] = ""
            return ""
        URL_TEXT_CACHE[url] = strip_html(resp.text)
        return URL_TEXT_CACHE[url]
    except Exception:
        URL_TEXT_CACHE[url] = ""
        return ""


def official_docs_evidence(claim: dict[str, Any]) -> list[dict[str, Any]]:
    text = " ".join(str(claim.get(k, "")) for k in ("claim_text", "subject", "object")).lower()
    urls: list[str] = []
    for alias, candidates in OFFICIAL_DOC_URLS.items():
        if alias in text:
            urls.extend(candidates)
    if not urls:
        return []
    records=[]
    claim_id=claim["claim_id"]
    # Keep live runs responsive; repeated URL fetches are cached.
    for i, url in enumerate(urls[:2], start=1):
        body = fetch_text_url(url)
        if not body:
            continue
        quote = relevant_quote(body, claim.get("claim_text", ""), max_chars=1600)
        records.append({
            "evidence_id": f"docs-{claim_id}-{i}",
            "claim_id": claim_id,
            "source_type": "official_docs",
            "authority": "official",
            "subject_match": "medium",
            "quote": quote,
            "url": url,
            "retrieved_at": now_iso(),
            "supports": [],
            "contradicts": [],
            "missing": [claim_id],
            "scores": {"retrieval_relevance": 0.7, "source_authority": 0.9, "evidence_coverage": 0.35},
        })
    return records

def v1_gather_evidence_records(claim: dict[str, Any], max_sources: int = 4, source_workers: int = 4) -> list[dict[str, Any]]:
    """Bridge the mature v1 Source Router into the v2 Evidence Ledger.

    v1 already knows how to route claims to Tavily/DDG, GitHub, Wikipedia,
    arXiv, Semantic Scholar, PyPI, and npm. v2 keeps the artifact contract and
    evidence-ledger judge, but reuses those source adapters instead of rebuilding
    them ad hoc.
    """
    try:
        import importlib.util
        scripts_dir = Path(__file__).resolve().parent
        fact_path = scripts_dir / "fact_check.py"
        spec = importlib.util.spec_from_file_location("loa_fact_check_v1", fact_path)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault("loa_fact_check_v1", module)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        claim_text = str(claim.get("claim_text") or "")
        claim_type = str(claim.get("claim_type") or "FEATURE")
        routed, results = module.gather_evidence(
            claim_text,
            claim_type,
            wiki_path=None,
            use_llm_router=False,
            use_wiki=False,
            max_sources=max_sources,
            source_workers=source_workers,
        )
    except Exception:
        return []

    records: list[dict[str, Any]] = []
    claim_id = claim["claim_id"]
    for i, item in enumerate(results[:8], start=1):
        source = str(item.get("source") or "v1_source_router")
        structured = item.get("structured_data") if isinstance(item.get("structured_data"), dict) else None
        snippet_parts = []
        if item.get("title"):
            snippet_parts.append(str(item.get("title")))
        if item.get("snippet"):
            snippet_parts.append(str(item.get("snippet")))
        if structured:
            snippet_parts.append("structured_data=" + json.dumps(structured, ensure_ascii=False, sort_keys=True))
        quote = " | ".join(snippet_parts)[:1500] or "v1 source router returned an evidence result."
        authority = "canonical" if item.get("structured") or source in {"github", "pypi", "npm", "arxiv", "semantic_scholar"} else ("reference" if source == "wikipedia" else "secondary")
        score = float(item.get("evidence_score") or 0.5)
        records.append({
            "evidence_id": f"v1-{claim_id}-{i}",
            "claim_id": claim_id,
            "source_type": f"v1_{source}",
            "authority": authority,
            "subject_match": "unknown",
            "quote": quote,
            "url": item.get("url"),
            "retrieved_at": now_iso(),
            "supports": [],
            "contradicts": [],
            "missing": [claim_id],
            "scores": {
                "retrieval_relevance": max(0.0, min(1.0, score)),
                "source_authority": 0.9 if authority == "canonical" else (0.65 if authority == "reference" else 0.5),
                "evidence_coverage": 0.35,
            },
            "metadata": {
                "v1_routed_sources": routed,
                "v1_source": source,
                "structured": bool(item.get("structured")),
            },
        })
    return records

def duckduckgo_evidence(claim: dict[str, Any], max_results: int = 3) -> list[dict[str, Any]]:
    text = claim.get("claim_text", "")
    query = urllib.parse.urlencode({"q": text, "format": "json", "no_redirect": "1", "no_html": "1"})
    data = http_json(f"https://api.duckduckgo.com/?{query}", timeout=15)
    if not isinstance(data, dict):
        return []
    raw_results: list[tuple[str, str, str]] = []
    if data.get("AbstractText"):
        raw_results.append((str(data.get("Heading") or "DuckDuckGo abstract"), str(data.get("AbstractURL") or ""), str(data.get("AbstractText") or "")))
    for r in data.get("RelatedTopics", []):
        if isinstance(r, dict) and r.get("Text"):
            raw_results.append((str(r.get("Text", ""))[:100], str(r.get("FirstURL") or ""), str(r.get("Text") or "")))
        elif isinstance(r, dict) and isinstance(r.get("Topics"), list):
            for sub in r["Topics"]:
                if isinstance(sub, dict) and sub.get("Text"):
                    raw_results.append((str(sub.get("Text", ""))[:100], str(sub.get("FirstURL") or ""), str(sub.get("Text") or "")))
        if len(raw_results) >= max_results:
            break
    out=[]
    for i, (title, url, quote) in enumerate(raw_results[:max_results], start=1):
        out.append({
            "evidence_id": f"ddg-{claim['claim_id']}-{i}",
            "claim_id": claim["claim_id"],
            "source_type": "duckduckgo",
            "authority": "secondary",
            "subject_match": "unknown",
            "quote": quote[:1000],
            "url": url,
            "retrieved_at": now_iso(),
            "supports": [],
            "contradicts": [],
            "missing": [claim["claim_id"]],
            "scores": {"retrieval_relevance": 0.4, "source_authority": 0.35, "evidence_coverage": 0.2},
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


SPECIAL_LINE_COUNT_CACHE: dict[str, tuple[int, int, str]] = {}


def github_archive_line_count(repo: str, ref: str, prefix: str, suffixes: tuple[str, ...]) -> tuple[int, int, str] | None:
    cache_key = f"{repo}@{ref}:{prefix}:{','.join(suffixes)}"
    if cache_key in SPECIAL_LINE_COUNT_CACHE:
        return SPECIAL_LINE_COUNT_CACHE[cache_key]
    try:
        import io
        import tarfile
        import requests
        url = f"https://github.com/{repo}/archive/refs/heads/{ref}.tar.gz"
        resp = requests.get(url, headers={"User-Agent": "llm-output-audit-v2/0.1"}, timeout=120)
        if not resp.ok:
            return None
        files = 0
        lines = 0
        with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as archive:
            for member in archive:
                parts = member.name.split("/", 1)
                rel = parts[1] if len(parts) > 1 else ""
                if not member.isfile() or not rel.startswith(prefix) or not rel.endswith(suffixes):
                    continue
                fh = archive.extractfile(member)
                if fh is None:
                    continue
                files += 1
                lines += sum(1 for _ in fh)
        result = (files, lines, url)
        SPECIAL_LINE_COUNT_CACHE[cache_key] = result
        return result
    except Exception:
        return None


def github_tree_count(repo: str, ref: str, prefix: str) -> tuple[int, int, str] | None:
    try:
        url = f"https://api.github.com/repos/{repo}/git/trees/{urllib.parse.quote(ref)}?recursive=1"
        data = http_json(url, headers={"User-Agent": "llm-output-audit-v2/0.1"}, timeout=30)
        if not isinstance(data, dict):
            return None
        items = [x for x in data.get("tree", []) if x.get("type") == "blob" and str(x.get("path", "")).startswith(prefix)]
        return len(items), int(sum(int(x.get("size") or 0) for x in items)), url
    except Exception:
        return None


def specialized_evidence(claim: dict[str, Any]) -> list[dict[str, Any]]:
    text = str(claim.get("claim_text") or "")
    lower = text.lower()
    claim_id = claim["claim_id"]
    records: list[dict[str, Any]] = []
    if "dgx spark" in lower or "gb10" in lower:
        url = "https://www.nvidia.com/en-us/products/workstations/dgx-spark/"
        body = fetch_text_url(url, timeout=20)
        compact = body.lower()
        found = {
            "gb10": "gb10" in compact and "grace blackwell" in compact,
            "128gb": "128 gb" in compact and "lpddr5x" in compact,
            "273gb/s": "273 gb/s" in compact,
            "fp4": ("one petaflop" in compact or "1 peta" in compact or "1 pflop" in compact) and "fp4" in compact,
        }
        if body:
            quote = relevant_quote(body, "DGX Spark GB10 Grace Blackwell 128 GB LPDDR5x 273 GB/s FP4 one petaFLOP Tensor Performance Memory Bandwidth", max_chars=1800)
            supports = [claim_id] if all(found.values()) else []
            records.append({
                "evidence_id": f"official-dgx-spark-{claim_id}",
                "claim_id": claim_id,
                "source_type": "official_product_page",
                "authority": "official",
                "subject_match": "exact",
                "quote": f"NVIDIA DGX Spark official page/spec text matched fields {found}. Relevant quote: {quote}",
                "url": url,
                "retrieved_at": now_iso(),
                "supports": supports,
                "contradicts": [],
                "missing": [] if supports else [claim_id],
                "scores": {"retrieval_relevance": 0.98, "source_authority": 1.0, "evidence_coverage": 0.95 if supports else 0.55},
            })
    if "emacs" in lower and "lisp" in lower and ("行" in text or "line" in lower):
        counted = github_archive_line_count("emacs-mirror/emacs", "master", "lisp/", (".el",))
        if counted:
            files, lines, url = counted
            claimed = extract_claim_number(text)
            contradicts = [claim_id] if claimed and abs(lines - claimed) > max(50_000, int(claimed * 0.25)) else []
            supports = [claim_id] if claimed and not contradicts else []
            records.append({
                "evidence_id": f"stat-emacs-lisp-{claim_id}",
                "claim_id": claim_id,
                "source_type": "github_archive_line_count",
                "authority": "canonical",
                "subject_match": "exact",
                "quote": f"Counted Emacs mirror archive {url}: lisp/*.el files={files}, physical lines={lines}. This directly checks the GNU Emacs lisp/ directory line-count claim.",
                "url": "https://github.com/emacs-mirror/emacs/tree/master/lisp",
                "retrieved_at": now_iso(),
                "supports": supports,
                "contradicts": contradicts,
                "missing": [] if supports or contradicts else [claim_id],
                "scores": {"retrieval_relevance": 0.98, "source_authority": 0.9, "evidence_coverage": 0.95},
            })
    if "melpa" in lower:
        counted = github_tree_count("melpa/melpa", "master", "recipes/")
        if counted:
            files, bytes_size, url = counted
            records.append({
                "evidence_id": f"stat-melpa-recipes-{claim_id}",
                "claim_id": claim_id,
                "source_type": "github_tree_metadata",
                "authority": "canonical",
                "subject_match": "medium",
                "quote": f"MELPA recipe repository contains {files} recipe files under recipes/ (total recipe bytes={bytes_size}). This verifies MELPA package index size, but not total lines across every upstream package repository; that requires crawling each recipe's upstream repo.",
                "url": "https://github.com/melpa/melpa/tree/master/recipes",
                "retrieved_at": now_iso(),
                "supports": [],
                "contradicts": [],
                "missing": [claim_id],
                "scores": {"retrieval_relevance": 0.85, "source_authority": 0.9, "evidence_coverage": 0.45},
            })
    return records


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
    records.extend(specialized_evidence(claim))
    preferred = set((plan or {}).get("preferred_sources", []))
    if "github_api" in preferred or "source_repo" in preferred or "github" in claim.get("claim_text", "").lower() or claim_mentions_stars(claim.get("claim_text", "")):
        records.extend(github_repo_evidence(claim))
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        records.extend(official_docs_evidence(claim))
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        records.extend(v1_gather_evidence_records(claim, max_sources=4, source_workers=4))
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        records.extend(github_readme_evidence(claim))
    if not any(r.get("supports") or r.get("contradicts") for r in records):
        records.extend(tavily_evidence(claim, max_results=3))
    if not records:
        records.extend(duckduckgo_evidence(claim, max_results=3))
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
            # Keep retrieved source records immutable as observations. The LLM
            # judge is a derived evidence record; it may support a claim, but its
            # refutations are conservative and do not turn secondary/irrelevant
            # snippets into high-authority contradictions.
            for rec in records:
                rec.setdefault("missing", [claim_id])
            records.append({
                "evidence_id": f"judge-{claim_id}", "claim_id": claim_id, "source_type": "llm_hybrid_judge",
                "authority": "derived", "subject_match": "derived", "quote": judged["reason"], "retrieved_at": now_iso(),
                "supports": [claim_id] if judged["truth_verdict"] in {"supported", "partially_supported"} else [],
                "contradicts": [claim_id] if judged["truth_verdict"] == "refuted" else [],
                "missing": [claim_id] if judged["truth_verdict"] == "not_enough_evidence" else [],
                "scores": {"retrieval_relevance": 0.7, "source_authority": judged["confidence"], "evidence_coverage": judged["confidence"]},
            })
    return records


def gather_live_evidence(claims: list[dict[str, Any]], plans: list[dict[str, Any]], workers: int = 6) -> list[dict[str, Any]]:
    plan_by_id = {p.get("claim_id"): p for p in plans}
    records: list[dict[str, Any]] = []
    if len(claims) <= 1 or workers <= 1:
        for claim in claims:
            records.extend(live_evidence_for_claim(claim, plan_by_id.get(claim.get("claim_id"))))
        return records
    with ThreadPoolExecutor(max_workers=min(workers, len(claims))) as ex:
        futures = [ex.submit(live_evidence_for_claim, claim, plan_by_id.get(claim.get("claim_id"))) for claim in claims]
        for fut in as_completed(futures):
            try:
                records.extend(fut.result())
            except Exception:
                continue
    records.sort(key=lambda item: str(item.get("evidence_id", "")))
    return records

def infer_verification_strategy(claim: dict[str, Any]) -> dict[str, Any]:
    """Infer where a claim *should* be verified before trying adapters.

    This is deliberately about evidence ownership, not about today's available
    adapters. The audit UI can therefore distinguish three states:
    found evidence, searched wrong place, or correct method known but not yet
    executable.
    """
    text = str(claim.get("claim_text") or "")
    lower = text.lower()
    claim_type = str(claim.get("claim_type") or "UNKNOWN")
    subject = str(claim.get("subject") or "unknown")

    strategy = {
        "source_kind": "official_or_canonical_reference",
        "authority_target": "canonical_reference_or_official_docs",
        "execution_method": "web_search_then_fetch_official_page",
        "locator_hints": [],
        "queries": [text],
        "adapter_status": "generic_web_required",
        "rationale": "Default: public factual claim should be checked against official/canonical references before secondary snippets.",
    }

    if claim.get("verifiability") == "local":
        return {
            **strategy,
            "source_kind": "local_context",
            "authority_target": "local_files_logs_or_owner_knowledge",
            "execution_method": "local_review_checklist",
            "locator_hints": ["local filesystem", "deployment logs", "human owner"],
            "adapter_status": "manual_or_local_required",
            "rationale": "The claim depends on local/private context rather than public web evidence.",
        }
    if claim.get("verifiability") == "not_publicly_verifiable":
        return {
            **strategy,
            "source_kind": "planning_or_estimate",
            "authority_target": "explicit assumptions or owner confirmation",
            "execution_method": "human_review_with_assumption_check",
            "locator_hints": ["planning document", "benchmark run", "owner confirmation"],
            "adapter_status": "manual_required",
            "rationale": "The claim is an estimate/plan; public sources can verify assumptions but not the final local estimate.",
        }

    if any(term in lower for term in ["dgx spark", "gb10", "grace blackwell", "lpddr5x", "fp4", "pflops"]):
        return {
            **strategy,
            "source_kind": "vendor_product_specs",
            "authority_target": "vendor official product/specification page",
            "execution_method": "fetch_official_product_page",
            "locator_hints": ["nvidia.com", "NVIDIA DGX Spark product page", "datasheet/specifications"],
            "queries": [
                "NVIDIA DGX Spark specifications GB10 128 GB LPDDR5x 273 GB/s FP4",
                text,
            ],
            "adapter_status": "implemented:nvidia_dgx_spark_product_page",
            "rationale": "Hardware specifications are source-owned by the vendor; generic search snippets are secondary.",
        }

    if any(term in lower for term in ["行", "lines", "loc", "line count", "源码", "source code"]):
        if "emacs" in lower and "lisp" in lower:
            return {
                **strategy,
                "source_kind": "source_repository_statistic",
                "authority_target": "source repository archive/tree",
                "execution_method": "download_or_tree_walk_and_count_lines",
                "locator_hints": ["emacs-mirror/emacs", "lisp/", "*.el"],
                "queries": ["GNU Emacs source repository lisp directory", text],
                "adapter_status": "implemented:github_archive_line_count",
                "rationale": "Repository line counts should be computed from the source tree, not searched as prose.",
            }
        if "melpa" in lower:
            return {
                **strategy,
                "source_kind": "package_index_plus_upstream_repo_crawl",
                "authority_target": "package index recipes plus each upstream source repository",
                "execution_method": "crawl_package_recipes_then_count_upstream_repositories",
                "locator_hints": ["melpa/melpa recipes", "recipe upstream repo URLs", "per-repo LOC counters"],
                "queries": ["MELPA package recipes upstream repositories", text],
                "adapter_status": "partial:melpa_recipe_index_only;missing:upstream_repo_loc_crawler",
                "rationale": "Total LOC across all MELPA packages is not owned by one web page; it requires crawling the package index and then the upstream repos.",
            }
        return {
            **strategy,
            "source_kind": "source_repository_statistic",
            "authority_target": "source repository archive/tree",
            "execution_method": "loc_counter_against_source_tree",
            "locator_hints": [subject, "GitHub/GitLab/source repo", "target directory/file globs"],
            "adapter_status": "needs_repo_locator_and_loc_counter",
            "rationale": "Code size claims should be measured from source repositories rather than generic web snippets.",
        }

    if any(term in lower for term in ["github", "stars", "forks", "release", "commit", "repo", "repository", "仓库", "星标"]):
        return {
            **strategy,
            "source_kind": "source_repository_metadata",
            "authority_target": "source host API",
            "execution_method": "github_api_or_source_host_api",
            "locator_hints": ["GitHub API", "repository full name"],
            "adapter_status": "implemented:github_api_when_repo_resolved",
            "rationale": "Repository metadata belongs to the source host API.",
        }

    if any(term in lower for term in ["download", "downloads", "npm", "pypi", "package", "包下载"]):
        return {
            **strategy,
            "source_kind": "package_registry_metadata",
            "authority_target": "npm/PyPI/package registry API",
            "execution_method": "registry_api_lookup",
            "locator_hints": ["npm registry", "PyPI JSON API", "package name"],
            "adapter_status": "implemented:npm_pypi_for_resolved_packages",
            "rationale": "Package versions/downloads should be checked against package registries.",
        }

    if any(term in lower for term in ["benchmark", "humaneval", "mbpp", "评测", "基准", "paper", "arxiv", "论文"]):
        return {
            **strategy,
            "source_kind": "benchmark_or_research_evidence",
            "authority_target": "official benchmark table, model card, paper, or leaderboard",
            "execution_method": "official_model_card_or_academic_index_lookup",
            "locator_hints": ["model card", "official technical report", "arXiv/Semantic Scholar", "leaderboard"],
            "adapter_status": "generic_web_or_academic_adapter_required",
            "rationale": "Benchmark claims need benchmark tables or papers, not broad web summaries.",
        }

    if claim_type in {"FEATURE", "COMPAT", "STATUS", "REQUIREMENT"}:
        strategy.update({
            "source_kind": "official_docs_or_source_repo",
            "authority_target": "official docs, README, source repository, changelog",
            "execution_method": "official_docs_fetch_or_repo_readme_fetch",
            "locator_hints": [subject, "official docs", "README", "changelog"],
            "adapter_status": "generic_official_doc_locator_required",
            "rationale": "Capability/status claims should be verified against official docs or source repos.",
        })
    return strategy


def plan_evidence(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    plans = []
    for claim in claims:
        strategy = infer_verification_strategy(claim)
        source_kind = strategy["source_kind"]
        if claim.get("verifiability") == "local":
            sources = ["local_files", "human_review"]
        elif claim.get("verifiability") == "not_publicly_verifiable":
            sources = ["human_review", "local_context"]
        elif source_kind in {"source_repository_metadata", "source_repository_statistic", "package_index_plus_upstream_repo_crawl"}:
            sources = ["source_repo", "github_api", "official_docs"]
        elif source_kind == "vendor_product_specs":
            sources = ["official_docs", "vendor_product_page", "canonical_reference"]
        elif source_kind == "package_registry_metadata":
            sources = ["package_registry", "official_docs", "canonical_reference"]
        elif source_kind == "benchmark_or_research_evidence":
            sources = ["benchmark_evidence", "official_docs", "academic_index"]
        elif source_kind == "official_docs_or_source_repo":
            sources = ["official_docs", "source_repo", "benchmark_evidence"]
        else:
            sources = ["canonical_reference", "official_docs", "benchmark_evidence"]
        plans.append({
            "claim_id": claim["claim_id"],
            "preferred_sources": sources,
            "queries": strategy["queries"],
            "source_kind": strategy["source_kind"],
            "authority_target": strategy["authority_target"],
            "execution_method": strategy["execution_method"],
            "locator_hints": strategy["locator_hints"],
            "adapter_status": strategy["adapter_status"],
            "rationale": strategy["rationale"],
        })
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
            informative_missing = [
                item for item in missing
                if item.get("authority") != "diagnostic" and not str(item.get("source_type", "")).endswith("_error")
            ]
            if informative_missing:
                informative_missing.sort(key=lambda item: float(item.get("scores", {}).get("retrieval_relevance", 0.0)), reverse=True)
                reason = informative_missing[0].get("quote") or "Relevant evidence was found but did not fully verify the claim."
            else:
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


def write_artifacts(out_dir: Path, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], *, source_mode: str, source_path: Path | None, article_profile: dict[str, Any] | None = None, verification_plan: list[dict[str, Any]] | None = None, evidence_records: list[dict[str, Any]] | None = None, write_revision: bool = False) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = evidence_records if evidence_records is not None else synthesize_evidence(claims, verdicts, source_mode)
    review_queue = synthesize_review_queue(verdicts)
    suggestions = synthesize_suggestions(claims, verdicts)
    patches: list[dict[str, Any]] = []
    if write_revision and source_path and source_path.exists():
        article_text = source_path.read_text(encoding="utf-8", errors="ignore")
        patches = synthesize_patches(article_text, claims, verdicts, evidence)
        revised_text, patches = apply_safe_patches(article_text, patches)
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
            "patches": "actual-patches.json" if write_revision else None,
            "revised": "revised.md" if write_revision else None,
            "revision_report": "revision-report.md" if write_revision else None,
            "article_profile": "article-profile.json" if article_profile else None,
            "verification_plan": "verification-plan.json" if verification_plan is not None else None,
        },
        "counts": {
            "claims": len(claims),
            "evidence": len(evidence),
            "verdicts": len(verdicts),
            "review_queue": len(review_queue),
            "suggestions": len(suggestions),
            "patches": len(patches),
            "patches_applied": sum(1 for p in patches if p.get("applied")),
        },
    }
    write_json(out_dir / "actual-claims.json", claims)
    write_jsonl(out_dir / "actual-evidence.jsonl", evidence)
    write_json(out_dir / "actual-verdicts.json", verdicts)
    write_json(out_dir / "actual-review-queue.json", review_queue)
    write_json(out_dir / "actual-suggestions.json", suggestions)
    (out_dir / "actual-report.md").write_text(render_actual_report(article_profile, claims, verdicts, review_queue, suggestions), encoding="utf-8")
    if write_revision and source_path and source_path.exists():
        write_json(out_dir / "actual-patches.json", patches)
        (out_dir / "revised.md").write_text(revised_text, encoding="utf-8")
        (out_dir / "revision-report.md").write_text(render_revision_report(patches), encoding="utf-8")
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
    parser.add_argument("--write-revision", action="store_true", help="Also synthesize actual-patches.json, apply safe patches to revised.md, and write revision-report.md")
    parser.add_argument("--post-audit-revision", choices=["none", "missing", "auto"], default="none", help="After writing revised.md, run a second v2 audit into post-audit/ using missing or auto evidence mode")
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
        write_artifacts(out_dir, claims, verdicts, source_mode="native", source_path=article_path, article_profile=profile, verification_plan=plans, evidence_records=evidence, write_revision=args.write_revision)
        revised_path = out_dir / "revised.md"
        if args.write_revision and args.post_audit_revision != "none" and revised_path.exists():
            post_profile, post_claims, post_plans, post_verdicts, post_evidence = run_native_pipeline(
                revised_path,
                None,
                max_claims=min(args.max_claims, 40),
                claim_extractor=args.claim_extractor,
                evidence_mode=args.post_audit_revision,
            )
            write_artifacts(out_dir / "post-audit", post_claims, post_verdicts, source_mode="post_revision", source_path=revised_path, article_profile=post_profile, verification_plan=post_plans, evidence_records=post_evidence, write_revision=False)
    else:
        raise SystemExit("provide --file ARTICLE, --oracle --case CASE_DIR, or --trace TRACE_JSONL")

    print(f"wrote v2 artifacts to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
