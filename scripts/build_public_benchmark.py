#!/usr/bin/env python3
"""Build public v2 benchmark smoke cases from bundled sample records.

This deterministic helper converts the small JSONL samples under
benchmark/public/samples into normalized benchmark cases. It does not download
data and does not call LLMs.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DATASETS = [
    "fever",
    "averitec",
    "factcheck-bench",
    "factscore",
    "technical-domain",
]


def write_source_pack(case_dir: Path) -> None:
    claims_path = case_dir / "expected-claims.json"
    verdicts_path = case_dir / "expected-verdicts.json"
    if not claims_path.exists() or not verdicts_path.exists():
        return
    claims = json.loads(claims_path.read_text(encoding="utf-8"))
    verdicts = json.loads(verdicts_path.read_text(encoding="utf-8"))
    claim_by_id = {item.get("claim_id"): item for item in claims}
    records = []
    for verdict in verdicts:
        claim = claim_by_id.get(verdict.get("claim_id"))
        if not claim:
            continue
        truth = verdict.get("truth_verdict")
        claim_text = claim.get("claim_text", "")
        records.append(
            {
                "evidence_id": (verdict.get("evidence_ids") or [f"src-{verdict.get('claim_id')}"])[0],
                "source_type": "benchmark_source_pack",
                "authority": "canonical" if truth in {"supported", "refuted"} else "unknown",
                "subject_match": "exact" if claim.get("subject") not in {None, "unknown"} else "unknown",
                "quote": verdict.get("reason") or verdict.get("notes") or claim_text,
                "url": f"benchmark://{case_dir.name}/{verdict.get('claim_id')}",
                "supports_claim_texts": [claim_text] if truth in {"supported", "partially_supported"} else [],
                "contradicts_claim_texts": [claim_text] if truth == "refuted" else [],
                "missing_claim_texts": [claim_text] if truth in {"not_enough_evidence", "not_publicly_verifiable"} else [],
                "notes": "Deterministic source-pack record for public benchmark smoke.",
            }
        )
    (case_dir / "source-pack.json").write_text(json.dumps(records, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build public benchmark smoke cases from bundled samples.")
    parser.add_argument("--output", default="benchmark/cases", help="Output benchmark cases directory")
    parser.add_argument("--clean-public", action="store_true", help="Remove existing public-* generated cases before conversion")
    args = parser.parse_args()

    repo = Path(__file__).resolve().parents[1]
    output = repo / args.output
    output.mkdir(parents=True, exist_ok=True)

    if args.clean_public:
        for case_dir in output.glob("public-*"):
            if case_dir.is_dir():
                shutil.rmtree(case_dir)

    for dataset in DATASETS:
        sample = repo / "benchmark" / "public" / "samples" / f"{dataset}.sample.jsonl"
        adapter = repo / "benchmark" / "public" / dataset / "adapter.py"
        run([sys.executable, str(adapter), "--input", str(sample), "--output", str(output), "--sample", "100"], cwd=repo)

    for case_dir in output.glob("public-*"):
        if case_dir.is_dir():
            write_source_pack(case_dir)

    print(f"built public benchmark cases in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
