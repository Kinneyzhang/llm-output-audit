#!/usr/bin/env python3
"""Common public benchmark adapter helpers for v2 scaffold.

Adapters are deterministic: raw dataset-shaped JSON/JSONL -> normalized benchmark cases.
They do not download datasets and do not call LLMs.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

LABEL_MAP = {
    "SUPPORTS": "supported",
    "SUPPORTED": "supported",
    "TRUE": "supported",
    "REFUTES": "refuted",
    "REFUTED": "refuted",
    "FALSE": "refuted",
    "NOT ENOUGH INFO": "not_enough_evidence",
    "NOT_ENOUGH_INFO": "not_enough_evidence",
    "NOT ENOUGH EVIDENCE": "not_enough_evidence",
    "NEI": "not_enough_evidence",
    "CONFLICTING EVIDENCE/CHERRY-PICKING": "conflicting_evidence",
    "CONFLICTING_EVIDENCE": "conflicting_evidence",
    "PARTIALLY SUPPORTED": "partially_supported",
    "NOT_PUBLICLY_VERIFIABLE": "not_publicly_verifiable",
}

DATASET_INFO = {
    "fever": {"layer": "public_core", "input_type": "claim"},
    "averitec": {"layer": "public_core", "input_type": "qa_evidence_case"},
    "factcheck-bench": {"layer": "public_core", "input_type": "article"},
    "factscore": {"layer": "public_core", "input_type": "atomic_fact"},
    "technical-domain": {"layer": "technical_domain", "input_type": "article"},
}


def load_records(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    payload = json.loads(text)
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return payload["records"]
    raise ValueError(f"unsupported input shape: {path}")


def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "case"


def map_label(label: str | None) -> str:
    return LABEL_MAP.get((label or "").strip().upper(), "not_enough_evidence")


def claim_obj(claim_id: str, text: str, source_file: str = "original.md", claim_type: str = "OTHER") -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "source_span": {"file": source_file, "start_line": 1, "end_line": 1, "quote": text},
        "claim_text": text,
        "claim_type": claim_type,
        "subject": "unknown",
        "predicate": "unknown",
        "object": text,
        "scope": "dataset-provided",
        "time_context": "dataset-provided",
        "verifiability": "public",
        "importance": "medium",
        "risk_level": "medium",
    }


def verdict_obj(claim_id: str, label: str, evidence_ids: list[str] | None = None, reason: str = "Dataset-provided label.") -> dict[str, Any]:
    truth = map_label(label)
    action = "keep" if truth == "supported" else "rewrite" if truth == "refuted" else "human_review"
    return {
        "claim_id": claim_id,
        "truth_verdict": truth,
        "audit_action": action,
        "evidence_ids": evidence_ids or [],
        "confidence": 1.0,
        "reason": reason,
    }


def write_case(out_root: Path, case_id: str, dataset: str, original: str, claims: list[dict[str, Any]], verdicts: list[dict[str, Any]], notes: str, metadata_extra: dict[str, Any] | None = None) -> None:
    info = DATASET_INFO[dataset]
    case_dir = out_root / case_id
    case_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "case_id": case_id,
        "layer": info["layer"],
        "source_dataset": dataset,
        "input_type": info["input_type"],
        "visibility": "public-synthetic-or-sample",
        "article_type": "public_benchmark_case" if info["layer"] == "public_core" else "technical_domain_case",
        "primary_subjects": [dataset],
    }
    if metadata_extra:
        metadata.update(metadata_extra)
    (case_dir / "original.md").write_text(original.rstrip() + "\n", encoding="utf-8")
    (case_dir / "metadata.json").write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (case_dir / "expected-claims.json").write_text(json.dumps(claims, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (case_dir / "expected-verdicts.json").write_text(json.dumps(verdicts, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (case_dir / "human-review.md").write_text("""# Human Review

Reviewer: dataset adapter

## Case summary

- Article type: public benchmark converted case
- Primary subjects: dataset-provided
- Expected risk areas: dataset-provided

## Scorecard

- claim_extraction_quality: unknown
- evidence_routing_quality: unknown
- verdict_quality: unknown
- suggestion_usefulness: unknown
- primary_failure_mode: unknown
- product_decision: public benchmark case pending auditor run

## Claim review

Dataset-provided expected labels. Human review pending for auditor outputs.
""", encoding="utf-8")
    (case_dir / "notes.md").write_text(notes.rstrip() + "\n", encoding="utf-8")


def convert_fever(record: dict[str, Any], index: int, out_root: Path) -> None:
    claim = record["claim"]
    cid = f"public-fever-{slugify(str(record.get('id', index)))}"
    evidence_ids = [f"e{n+1}" for n, _ in enumerate(record.get("evidence", []))]
    write_case(out_root, cid, "fever", claim, [claim_obj("c1", claim, claim_type="ATTR")], [verdict_obj("c1", record.get("label"), evidence_ids)], "# Notes\n\nConverted from FEVER-shaped sample record.")


def convert_averitec(record: dict[str, Any], index: int, out_root: Path) -> None:
    claim = record["claim"]
    cid = f"public-averitec-{slugify(str(record.get('claim_id', record.get('id', index))))}"
    questions = record.get("questions", [])
    notes = "# Notes\n\nConverted from AVeriTeC-shaped sample record.\n\n## Verification questions\n" + "\n".join(f"- {q.get('question')} -> {q.get('answer')}" for q in questions)
    write_case(out_root, cid, "averitec", claim, [claim_obj("c1", claim, claim_type="OTHER")], [verdict_obj("c1", record.get("label"), [f"qa{n+1}" for n,_ in enumerate(questions)], record.get("justification", "Dataset-provided justification."))], notes)


def convert_factcheck_bench(record: dict[str, Any], index: int, out_root: Path) -> None:
    cid = f"public-factcheck-bench-{slugify(str(record.get('id', index)))}"
    original = record.get("response") or record.get("text") or ""
    claims = []
    verdicts = []
    for n, item in enumerate(record.get("claims", []), start=1):
        claim_id = f"c{n}"
        claims.append(claim_obj(claim_id, item["claim"], claim_type=item.get("claim_type", "OTHER")))
        verdicts.append(verdict_obj(claim_id, item.get("label"), [f"e{n}"], item.get("reason", "Dataset-provided label.")))
    write_case(out_root, cid, "factcheck-bench", original, claims, verdicts, "# Notes\n\nConverted from Factcheck-Bench-shaped sample record.")


def convert_factscore(record: dict[str, Any], index: int, out_root: Path) -> None:
    cid = f"public-factscore-{slugify(str(record.get('id', index)))}"
    original = record.get("text", "")
    claims = []
    verdicts = []
    for n, item in enumerate(record.get("atomic_facts", []), start=1):
        claim_id = f"c{n}"
        fact = item["fact"]
        claims.append(claim_obj(claim_id, fact, claim_type="ATTR"))
        verdicts.append(verdict_obj(claim_id, item.get("label"), [f"e{n}"], item.get("source", "Atomic fact dataset label.")))
    write_case(out_root, cid, "factscore", original, claims, verdicts, "# Notes\n\nConverted from FActScore-shaped sample record.")


def convert_technical_domain(record: dict[str, Any], index: int, out_root: Path) -> None:
    cid = f"public-technical-domain-{slugify(str(record.get('id', index)))}"
    original = record.get("article", "")
    claims = []
    verdicts = []
    for n, item in enumerate(record.get("claims", []), start=1):
        claim_id = f"c{n}"
        claims.append(claim_obj(claim_id, item["claim"], claim_type=item.get("claim_type", "FEATURE")))
        verdicts.append(verdict_obj(claim_id, item.get("label"), item.get("evidence_ids", [f"e{n}"]), item.get("reason", "Technical-domain expected label.")))
    write_case(out_root, cid, "technical-domain", original, claims, verdicts, "# Notes\n\nConverted from technical-domain synthetic sample.", {"article_type": "technical_domain_case"})


CONVERTERS = {
    "fever": convert_fever,
    "averitec": convert_averitec,
    "factcheck-bench": convert_factcheck_bench,
    "factscore": convert_factscore,
    "technical-domain": convert_technical_domain,
}


def run_adapter(dataset: str) -> int:
    parser = argparse.ArgumentParser(description=f"Convert {dataset} records into normalized benchmark cases.")
    parser.add_argument("--input", required=True, help="Input JSON/JSONL sample path")
    parser.add_argument("--output", required=True, help="Output benchmark cases directory")
    parser.add_argument("--sample", type=int, default=10, help="Maximum records to convert")
    args = parser.parse_args()
    records = load_records(Path(args.input))[: args.sample]
    out_root = Path(args.output)
    for index, record in enumerate(records, start=1):
        CONVERTERS[dataset](record, index, out_root)
    print(f"converted {len(records)} {dataset} records -> {out_root}")
    return 0
