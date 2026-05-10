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
    quality_counts: dict[str, Counter[str]] = {field: Counter() for field in QUALITY_FIELDS}
    risky_cases: list[dict[str, Any]] = []
    product_decisions: Counter[str] = Counter()
    by_article_type: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for result in results:
        article_type = result.get("article_type") or "unknown"
        article_types[article_type] += 1
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
        "quality_counts": {field: dict(counts) for field, counts in quality_counts.items()},
        "type_quality": type_quality,
        "risky_cases": risky_cases,
        "product_decisions": dict(product_decisions),
    }


def validate_case(case_dir: Path) -> dict[str, Any]:
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

    result.update(
        {
            "article_type": metadata.get("article_type"),
            "primary_subjects": metadata.get("primary_subjects", []),
            "expected_claims": len(expected_claims),
            "expected_verdicts": len(expected_verdicts),
            "visibility": metadata.get("visibility"),
            "scorecard": scorecard,
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
        "### Article types",
        "",
    ]
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
        if "expected_claims" in r:
            lines.append(f"  - expected claims: `{r['expected_claims']}`")
            lines.append(f"  - expected verdicts: `{r['expected_verdicts']}`")
        if r.get("scorecard"):
            lines.append("  - scorecard:")
            for key, value in sorted(r["scorecard"].items()):
                lines.append(f"    - `{key}`: {value}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "This scaffold evaluator validates benchmark structure only. Future v2 work should compare actual audit artifacts against expected claims, evidence, verdicts, and suggestions.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and summarize llm-output-audit v2 benchmark cases.")
    parser.add_argument("cases", help="Directory containing benchmark case directories")
    parser.add_argument("--output", help="Markdown report path")
    parser.add_argument("--json-output", help="JSON summary path")
    args = parser.parse_args()

    cases_root = Path(args.cases)
    if not cases_root.exists():
        raise SystemExit(f"cases directory not found: {cases_root}")
    case_dirs = sorted(p for p in cases_root.iterdir() if p.is_dir())
    results = [validate_case(p) for p in case_dirs]

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
