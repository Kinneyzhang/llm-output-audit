#!/usr/bin/env python3
"""Build public v2 benchmark smoke cases from bundled sample records.

This deterministic helper converts the small JSONL samples under
benchmark/public/samples into normalized benchmark cases. It does not download
data and does not call LLMs.
"""

from __future__ import annotations

import argparse
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

    print(f"built public benchmark cases in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
