#!/usr/bin/env python3
"""Fail if a safety-critical package is below its coverage gate.

Usage: check_coverage.py coverage.json [--min 90] [package ...]
Aggregates line+branch coverage per package directory from `coverage json` output.
"""

import argparse
import json
import sys

DEFAULT_PACKAGES = ("gate", "domain", "remediation", "consumer")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report")
    parser.add_argument("--min", type=float, default=90.0)
    parser.add_argument("packages", nargs="*", default=list(DEFAULT_PACKAGES))
    args = parser.parse_args()

    with open(args.report, encoding="utf-8") as fh:
        files = json.load(fh)["files"]

    failed = False
    for pkg in args.packages:
        marker = f"settleagent/{pkg}/"
        total = covered = 0
        for path, data in files.items():
            if marker in path.replace("\\", "/"):
                s = data["summary"]
                total += s["num_statements"] + s.get("num_branches", 0)
                covered += s["covered_lines"] + s.get("covered_branches", 0)
        if total == 0:
            print(f"FAIL {pkg}/: no coverage data found")
            failed = True
            continue
        pct = 100.0 * covered / total
        ok = pct >= args.min
        failed |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} {pkg}/: {pct:5.1f}% (gate {args.min:.0f}%)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
