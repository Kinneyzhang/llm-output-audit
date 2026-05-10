#!/usr/bin/env python3
"""Adapter scaffold.

This placeholder documents the intended adapter boundary:
raw public dataset record -> normalized llm-output-audit benchmark case.

It intentionally does not download data or call LLMs.
"""

from __future__ import annotations

import argparse


def main() -> int:
    parser = argparse.ArgumentParser(description="Convert raw dataset records into normalized benchmark cases (scaffold).")
    parser.add_argument("--input", help="Raw dataset path or cache directory")
    parser.add_argument("--output", required=True, help="Output benchmark case directory")
    parser.add_argument("--sample", type=int, default=10, help="Maximum records to convert")
    parser.parse_args()
    raise SystemExit("adapter scaffold only; implementation pending")


if __name__ == "__main__":
    raise SystemExit(main())
