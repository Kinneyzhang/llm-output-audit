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
    ok_count = sum(1 for r in results if r.get("ok"))
    lines = [
        "# LLM Output Audit Benchmark Evaluation",
        "",
        f"Cases: `{len(results)}`",
        f"Valid cases: `{ok_count}`",
        "",
        "## Cases",
        "",
    ]
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
        Path(args.json_output).write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
