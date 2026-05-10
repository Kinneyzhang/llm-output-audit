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
import re
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
                "source_type": "benchmark_oracle" if source_label == "oracle" else "v1_trace",
                "authority": "canonical" if source_label == "oracle" else "unknown",
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


def write_artifacts(out_dir: Path, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], *, source_mode: str, source_path: Path | None) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    evidence = synthesize_evidence(claims, verdicts, source_mode)
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
    write_json(out_dir / "actual-manifest.json", manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate normalized llm-output-audit v2 artifacts.")
    parser.add_argument("--case", help="Benchmark case directory containing expected-claims/verdicts")
    parser.add_argument("--trace", help="Optional v1 trace JSONL to convert")
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
    else:
        raise SystemExit("provide either --oracle --case CASE_DIR or --trace TRACE_JSONL")

    print(f"wrote v2 artifacts to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
