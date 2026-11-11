#!/usr/bin/env python3
"""Throughput of convpreserve on the bundled selftest sample (feeds figure G7).

Judges every record of src/convpreserve/data/selftest_sample.json.gz through
convpreserve.judge.judge.compare, 5 timed runs after 1 warm-up run, and reports the
median pairs/second. Single process, single thread, no network.

    python bench/throughput.py [--runs 5] [--json OUT.json]

The numbers depend on the machine; the JSON records python version, platform,
cpu model and the sha256 of the sample so a run is attributable.
"""
import argparse
import gzip
import hashlib
import json
import os
import platform
import statistics
import time

from convpreserve import __version__
from convpreserve.judge import judge

SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__import__("convpreserve").__file__)),
                      "data", "selftest_sample.json.gz")


def _cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _one_run(records):
    t0 = time.perf_counter()
    for r in records:
        judge.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"],
                      r["side"], context=r.get("context"))
    return time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--json", help="write the result JSON here")
    args = ap.parse_args()
    with open(SAMPLE, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    with gzip.open(SAMPLE, "rt", encoding="utf-8") as f:
        records = json.load(f)
    _one_run(records)  # warm-up (imports, caches)
    times = [_one_run(records) for _ in range(args.runs)]
    rates = [len(records) / t for t in times]
    by_side = {}
    for side in ("request", "response", "stream"):
        sub = [r for r in records if r["side"] == side]
        ts = []
        for _ in range(args.runs):
            ts.append(_one_run(sub))
        by_side[side] = {"pairs": len(sub), "median_pairs_per_s": statistics.median(len(sub) / t for t in ts)}
    out = {
        "convpreserve": __version__, "pairs": len(records), "runs": args.runs,
        "seconds_per_run": times, "pairs_per_s_per_run": rates,
        "median_pairs_per_s": statistics.median(rates),
        "median_seconds_per_run": statistics.median(times),
        "by_side": by_side,
        "sample_sha256": sha, "python": platform.python_version(),
        "platform": platform.platform(), "cpu": _cpu_model(),
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
    }
    print(json.dumps(out, indent=2))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2)
            f.write("\n")


if __name__ == "__main__":
    main()
