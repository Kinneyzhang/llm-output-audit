#!/usr/bin/env python3
"""Minimal v2 benchmark scaffold evaluator.

This script intentionally does not call an LLM. It validates benchmark case
structure and summarizes expected labels so v2 development can become
benchmark-first without requiring secrets in CI.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REQUIRED_CASE_FILES = [
    "original.md",
    "metadata.json",
    "expected-claims.json",
    "expected-verdicts.json",
    "human-review.md",
    "notes.md",
]

QUALITY_ORDER = {
    "unknown": 0,
    "poor": 1,
    "poor_to_medium": 2,
    "low": 2,
    "low_to_medium": 2,
    "medium": 3,
    "medium_to_high": 4,
    "high": 5,
}

QUALITY_FIELDS = [
    "claim_extraction_quality",
    "evidence_routing_quality",
    "verdict_quality",
    "suggestion_usefulness",
]

RISKY_QUALITY_VALUES = {"poor", "poor_to_medium", "low", "low_to_medium"}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_scorecard(review_text: str) -> dict[str, str]:
    """Extract simple `key: value` lines from a human-review Scorecard section."""
    match = re.search(r"^## Scorecard\s*(.*?)(?=^## |\Z)", review_text, flags=re.M | re.S)
    if not match:
        return {}
    scorecard: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = re.match(r"^-\s*([A-Za-z0-9_-]+):\s*(.+?)\s*$", line.strip())
        if item:
            scorecard[item.group(1)] = item.group(2)
    return scorecard


def quality_score(value: str | None) -> int:
    return QUALITY_ORDER.get((value or "unknown").strip(), 0)


def risky_dimensions(scorecard: dict[str, str]) -> list[str]:
    risks: list[str] = []
    for field in QUALITY_FIELDS:
        value = scorecard.get(field, "unknown")
        if value in RISKY_QUALITY_VALUES:
            risks.append(field)
    return risks


def summarize_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate scorecards into coarse product-risk signals."""
    article_types: Counter[str] = Counter()
    layers: Counter[str] = Counter()
    source_datasets: Counter[str] = Counter()
    quality_counts: dict[str, Counter[str]] = {field: Counter() for field in QUALITY_FIELDS}
    risky_cases: list[dict[str, Any]] = []
    product_decisions: Counter[str] = Counter()
    actual_available_count = 0
    actual_metric_sums: Counter[str] = Counter()
    actual_metric_denominators: Counter[str] = Counter()
    actual_failure_cases: list[dict[str, Any]] = []
    actual_artifact_counts: Counter[str] = Counter()
    actual_queue_counts: Counter[str] = Counter()
    actual_suggestion_severity_counts: Counter[str] = Counter()
    by_article_type: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for result in results:
        article_type = result.get("article_type") or "unknown"
        layer = result.get("layer") or "unknown"
        source_dataset = result.get("source_dataset") or "unknown"
        article_types[article_type] += 1
        layers[layer] += 1
        source_datasets[source_dataset] += 1
        by_article_type[article_type].append(result)
        scorecard = result.get("scorecard") or {}
        for field in QUALITY_FIELDS:
            quality_counts[field][scorecard.get(field, "unknown")] += 1
        risks = risky_dimensions(scorecard)
        if risks:
            risky_cases.append(
                {
                    "case_id": result["case_id"],
                    "article_type": article_type,
                    "risky_dimensions": risks,
                    "primary_failure_mode": scorecard.get("primary_failure_mode", "unknown"),
                    "product_decision": scorecard.get("product_decision", "unknown"),
                }
            )
        if scorecard.get("product_decision"):
            product_decisions[scorecard["product_decision"]] += 1
        comparison = result.get("actual_comparison") or {}
        if comparison.get("available"):
            actual_available_count += 1
            for artifact_name, present in (comparison.get("artifact_presence") or {}).items():
                if present:
                    actual_artifact_counts[artifact_name] += 1
            for queue, count in (comparison.get("review_queue_counts") or {}).items():
                actual_queue_counts[queue] += count
            for severity, count in (comparison.get("suggestion_severity_counts") or {}).items():
                actual_suggestion_severity_counts[severity] += count
            for field in ["claim_precision", "claim_recall", "verdict_accuracy_on_shared_ids"]:
                actual_metric_sums[field] += float(comparison.get(field, 0.0))
                actual_metric_denominators[field] += 1
            if (
                comparison.get("claim_recall", 1.0) < 1.0
                or comparison.get("verdict_accuracy_on_shared_ids", 1.0) < 1.0
                or comparison.get("refuted_false_positive_candidate_count", 0)
            ):
                actual_failure_cases.append({
                    "case_id": result["case_id"],
                    "claim_precision": comparison.get("claim_precision"),
                    "claim_recall": comparison.get("claim_recall"),
                    "verdict_accuracy_on_shared_ids": comparison.get("verdict_accuracy_on_shared_ids"),
                    "refuted_false_positive_candidate_count": comparison.get("refuted_false_positive_candidate_count", 0),
                })

    type_quality: dict[str, dict[str, float]] = {}
    for article_type, items in by_article_type.items():
        type_quality[article_type] = {}
        for field in QUALITY_FIELDS:
            values = [quality_score((item.get("scorecard") or {}).get(field)) for item in items]
            type_quality[article_type][field] = round(sum(values) / len(values), 2) if values else 0

    return {
        "case_count": len(results),
        "valid_case_count": sum(1 for r in results if r.get("ok")),
        "article_types": dict(article_types),
        "layers": dict(layers),
        "source_datasets": dict(source_datasets),
        "quality_counts": {field: dict(counts) for field, counts in quality_counts.items()},
        "type_quality": type_quality,
        "risky_cases": risky_cases,
        "product_decisions": dict(product_decisions),
        "actual_available_count": actual_available_count,
        "actual_metric_averages": {
            field: round(actual_metric_sums[field] / actual_metric_denominators[field], 4)
            for field in actual_metric_sums
            if actual_metric_denominators[field]
        },
        "actual_failure_cases": actual_failure_cases,
        "actual_artifact_counts": dict(actual_artifact_counts),
        "actual_queue_counts": dict(actual_queue_counts),
        "actual_suggestion_severity_counts": dict(actual_suggestion_severity_counts),
    }


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def find_artifact(case_dir: Path, name: str, actual_root: Path | None = None) -> Path | None:
    candidates = []
    if actual_root is not None:
        candidates.append(actual_root / case_dir.name / name)
    candidates.append(case_dir / name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def compare_actual_artifacts(
    case_dir: Path,
    expected_claims: list[dict[str, Any]],
    expected_verdicts: list[dict[str, Any]],
    actual_root: Path | None = None,
) -> dict[str, Any]:
    actual_claims_path = find_artifact(case_dir, "actual-claims.json", actual_root)
    actual_verdicts_path = find_artifact(case_dir, "actual-verdicts.json", actual_root)
    actual_evidence_path = find_artifact(case_dir, "actual-evidence.jsonl", actual_root)
    actual_review_path = find_artifact(case_dir, "actual-review-queue.json", actual_root)
    actual_suggestions_path = find_artifact(case_dir, "actual-suggestions.json", actual_root)
    actual_manifest_path = find_artifact(case_dir, "actual-manifest.json", actual_root)
    comparison: dict[str, Any] = {
        "available": bool(actual_claims_path and actual_verdicts_path),
        "actual_claims_path": str(actual_claims_path) if actual_claims_path else None,
        "actual_verdicts_path": str(actual_verdicts_path) if actual_verdicts_path else None,
        "artifact_presence": {
            "claims": bool(actual_claims_path),
            "evidence": bool(actual_evidence_path),
            "verdicts": bool(actual_verdicts_path),
            "review_queue": bool(actual_review_path),
            "suggestions": bool(actual_suggestions_path),
            "manifest": bool(actual_manifest_path),
        },
    }
    if not comparison["available"]:
        return comparison

    actual_claims = load_json(actual_claims_path)  # type: ignore[arg-type]
    actual_verdicts = load_json(actual_verdicts_path)  # type: ignore[arg-type]
    actual_review_queue = load_json(actual_review_path) if actual_review_path else []
    actual_suggestions = load_json(actual_suggestions_path) if actual_suggestions_path else []
    evidence_count = 0
    if actual_evidence_path:
        evidence_count = sum(1 for line in actual_evidence_path.read_text(encoding="utf-8").splitlines() if line.strip())

    expected_claim_texts = {normalize_text(item.get("claim_text", "")) for item in expected_claims}
    actual_claim_texts = {normalize_text(item.get("claim_text", "")) for item in actual_claims}
    claim_matches = expected_claim_texts & actual_claim_texts
    expected_claim_id_by_text = {normalize_text(item.get("claim_text", "")): item.get("claim_id") for item in expected_claims}
    actual_claim_id_by_text = {normalize_text(item.get("claim_text", "")): item.get("claim_id") for item in actual_claims}
    expected_by_id = {item.get("claim_id"): item for item in expected_verdicts}
    actual_by_id = {item.get("claim_id"): item for item in actual_verdicts}
    shared_ids = set(expected_by_id) & set(actual_by_id)
    shared_claim_texts = expected_claim_texts & actual_claim_texts
    verdict_comparisons: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for text_key in sorted(shared_claim_texts):
        expected_id = expected_claim_id_by_text.get(text_key)
        actual_id = actual_claim_id_by_text.get(text_key)
        if expected_id in expected_by_id and actual_id in actual_by_id:
            verdict_comparisons.append((text_key, expected_by_id[expected_id], actual_by_id[actual_id]))
    # Fall back to ID matching for legacy artifacts that do not preserve claim text.
    if not verdict_comparisons:
        verdict_comparisons = [(str(cid), expected_by_id[cid], actual_by_id[cid]) for cid in sorted(shared_ids)]
    verdict_matches = [
        key
        for key, expected_item, actual_item in verdict_comparisons
        if expected_item.get("truth_verdict") == actual_item.get("truth_verdict")
    ]
    refuted_false_positive_candidates = [
        key
        for key, expected_item, actual_item in verdict_comparisons
        if actual_item.get("truth_verdict") == "refuted"
        and expected_item.get("truth_verdict") != "refuted"
    ]
    comparison.update(
        {
            "expected_claim_count": len(expected_claims),
            "actual_claim_count": len(actual_claims),
            "claim_text_match_count": len(claim_matches),
            "claim_precision": round(len(claim_matches) / len(actual_claim_texts), 4) if actual_claim_texts else 0.0,
            "claim_recall": round(len(claim_matches) / len(expected_claim_texts), 4) if expected_claim_texts else 0.0,
            "expected_verdict_count": len(expected_verdicts),
            "actual_verdict_count": len(actual_verdicts),
            "verdict_shared_id_count": len(shared_ids),
            "verdict_shared_claim_count": len(verdict_comparisons),
            "verdict_match_count": len(verdict_matches),
            "verdict_accuracy_on_shared_ids": round(len(verdict_matches) / len(verdict_comparisons), 4) if verdict_comparisons else 0.0,
            "refuted_false_positive_candidate_count": len(refuted_false_positive_candidates),
            "refuted_false_positive_candidates": sorted(refuted_false_positive_candidates),
            "actual_evidence_count": evidence_count,
            "actual_review_queue_count": len(actual_review_queue),
            "actual_suggestion_count": len(actual_suggestions),
            "review_queue_counts": dict(Counter(item.get("queue", "unknown") for item in actual_review_queue)),
            "suggestion_severity_counts": dict(Counter(item.get("severity", "unknown") for item in actual_suggestions)),
            "missing_expected_claim_texts": sorted(expected_claim_texts - actual_claim_texts),
            "extra_actual_claim_texts": sorted(actual_claim_texts - expected_claim_texts),
        }
    )
    return comparison


def validate_case(case_dir: Path, actual_root: Path | None = None) -> dict[str, Any]:
    missing = [name for name in REQUIRED_CASE_FILES if not (case_dir / name).exists()]
    result: dict[str, Any] = {
        "case_id": case_dir.name,
        "path": str(case_dir),
        "missing": missing,
        "ok": not missing,
    }
    if missing:
        return result

    metadata = load_json(case_dir / "metadata.json")
    expected_claims = load_json(case_dir / "expected-claims.json")
    expected_verdicts = load_json(case_dir / "expected-verdicts.json")
    scorecard = parse_scorecard((case_dir / "human-review.md").read_text(encoding="utf-8"))
    actual_comparison = compare_actual_artifacts(case_dir, expected_claims, expected_verdicts, actual_root)

    result.update(
        {
            "article_type": metadata.get("article_type"),
            "layer": metadata.get("layer", "unknown"),
            "source_dataset": metadata.get("source_dataset", "unknown"),
            "input_type": metadata.get("input_type", "unknown"),
            "primary_subjects": metadata.get("primary_subjects", []),
            "expected_claims": len(expected_claims),
            "expected_verdicts": len(expected_verdicts),
            "visibility": metadata.get("visibility"),
            "scorecard": scorecard,
            "actual_comparison": actual_comparison,
        }
    )
    if len(expected_claims) != len(expected_verdicts):
        result["ok"] = False
        result.setdefault("errors", []).append(
            "expected-claims.json and expected-verdicts.json have different lengths"
        )
    return result


def render_markdown(results: list[dict[str, Any]]) -> str:
    summary = summarize_results(results)
    ok_count = summary["valid_case_count"]
    lines = [
        "# LLM Output Audit Benchmark Evaluation",
        "",
        f"Cases: `{len(results)}`",
        f"Valid cases: `{ok_count}`",
        "",
        "## Aggregate Scorecard",
        "",
        "### Layers",
        "",
    ]
    for layer, count in sorted(summary["layers"].items()):
        lines.append(f"- `{layer}`: `{count}`")
    lines.extend(["", "### Source datasets", ""])
    for source_dataset, count in sorted(summary["source_datasets"].items()):
        lines.append(f"- `{source_dataset}`: `{count}`")
    lines.extend(["", "### Article types", ""])
    for article_type, count in sorted(summary["article_types"].items()):
        lines.append(f"- `{article_type}`: `{count}`")
    lines.extend(["", "### Quality counts", ""])
    for field, counts in summary["quality_counts"].items():
        rendered = ", ".join(f"`{key}`={value}" for key, value in sorted(counts.items()))
        lines.append(f"- `{field}`: {rendered or '`none`'}")
    lines.extend(["", "### Average quality by article type", ""])
    for article_type, fields in sorted(summary["type_quality"].items()):
        rendered = ", ".join(f"`{key}`={value}" for key, value in sorted(fields.items()))
        lines.append(f"- `{article_type}`: {rendered}")
    lines.extend(["", "### Risky cases", ""])
    if summary["risky_cases"]:
        for item in summary["risky_cases"]:
            dims = ", ".join(f"`{dim}`" for dim in item["risky_dimensions"])
            lines.append(f"- `{item['case_id']}` ({item['article_type']}): {dims}")
            lines.append(f"  - failure: {item['primary_failure_mode']}")
            lines.append(f"  - decision: {item['product_decision']}")
    else:
        lines.append("- No risky dimensions found in parsed scorecards.")
    lines.extend(["", "### Actual artifact comparison", ""])
    lines.append(f"- cases with actual artifacts: `{summary['actual_available_count']}`")
    if summary["actual_metric_averages"]:
        for field, value in sorted(summary["actual_metric_averages"].items()):
            lines.append(f"- average `{field}`: `{value}`")
    else:
        lines.append("- no actual artifacts available")
    if summary["actual_artifact_counts"]:
        rendered = ", ".join(f"`{key}`={value}" for key, value in sorted(summary["actual_artifact_counts"].items()))
        lines.append(f"- artifact coverage: {rendered}")
    if summary["actual_queue_counts"]:
        rendered = ", ".join(f"`{key}`={value}" for key, value in sorted(summary["actual_queue_counts"].items()))
        lines.append(f"- review queues: {rendered}")
    if summary["actual_suggestion_severity_counts"]:
        rendered = ", ".join(f"`{key}`={value}" for key, value in sorted(summary["actual_suggestion_severity_counts"].items()))
        lines.append(f"- suggestion severities: {rendered}")
    if summary["actual_failure_cases"]:
        lines.append("- failure candidates:")
        for item in summary["actual_failure_cases"]:
            lines.append(f"  - `{item['case_id']}`: claim_recall=`{item['claim_recall']}`, verdict_accuracy=`{item['verdict_accuracy_on_shared_ids']}`, false_refuted_candidates=`{item['refuted_false_positive_candidate_count']}`")

    lines.extend(["", "### Product decisions", ""])
    if summary["product_decisions"]:
        for decision, count in sorted(summary["product_decisions"].items()):
            lines.append(f"- `{count}` × {decision}")
    else:
        lines.append("- No product decisions recorded.")
    lines.extend(["", "## Cases", ""])
    for r in results:
        status = "ok" if r.get("ok") else "failed"
        lines.append(f"- `{r['case_id']}` — `{status}`")
        if r.get("missing"):
            lines.append(f"  - missing: {', '.join(r['missing'])}")
        if r.get("errors"):
            lines.append(f"  - errors: {'; '.join(r['errors'])}")
        if r.get("article_type"):
            lines.append(f"  - article_type: `{r['article_type']}`")
        if r.get("layer"):
            lines.append(f"  - layer: `{r['layer']}`")
        if r.get("source_dataset"):
            lines.append(f"  - source_dataset: `{r['source_dataset']}`")
        if r.get("input_type"):
            lines.append(f"  - input_type: `{r['input_type']}`")
        if "expected_claims" in r:
            lines.append(f"  - expected claims: `{r['expected_claims']}`")
            lines.append(f"  - expected verdicts: `{r['expected_verdicts']}`")
        if r.get("actual_comparison", {}).get("available"):
            cmp = r["actual_comparison"]
            lines.append("  - actual comparison:")
            lines.append(f"    - claim precision: `{cmp['claim_precision']}`")
            lines.append(f"    - claim recall: `{cmp['claim_recall']}`")
            lines.append(f"    - verdict accuracy on shared IDs: `{cmp['verdict_accuracy_on_shared_ids']}`")
            lines.append(f"    - refuted false-positive candidates: `{cmp['refuted_false_positive_candidate_count']}`")
            lines.append(f"    - evidence records: `{cmp.get('actual_evidence_count', 0)}`")
            lines.append(f"    - review queue items: `{cmp.get('actual_review_queue_count', 0)}`")
            lines.append(f"    - suggestion items: `{cmp.get('actual_suggestion_count', 0)}`")
        if r.get("scorecard"):
            lines.append("  - scorecard:")
            for key, value in sorted(r["scorecard"].items()):
                lines.append(f"    - `{key}`: {value}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "This evaluator validates benchmark structure and, when actual artifacts are present, compares actual claims/verdicts against expected claims/verdicts. It still does not call an LLM or external services.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and summarize llm-output-audit v2 benchmark cases.")
    parser.add_argument("cases", help="Directory containing benchmark case directories")
    parser.add_argument("--output", help="Markdown report path")
    parser.add_argument("--json-output", help="JSON summary path")
    parser.add_argument("--actual-root", help="Optional directory containing per-case actual-claims.json and actual-verdicts.json artifacts")
    args = parser.parse_args()

    cases_root = Path(args.cases)
    if not cases_root.exists():
        raise SystemExit(f"cases directory not found: {cases_root}")
    case_dirs = sorted(p for p in cases_root.iterdir() if p.is_dir())
    actual_root = Path(args.actual_root) if args.actual_root else None
    results = [validate_case(p, actual_root) for p in case_dirs]

    report = render_markdown(results)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
    else:
        print(report)
    if args.json_output:
        payload = {"summary": summarize_results(results), "cases": results}
        Path(args.json_output).write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
