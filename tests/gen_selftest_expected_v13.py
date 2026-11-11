#!/usr/bin/env python3
"""Generate src/convpreserve/data/selftest_expected_v13.json.gz: the oracle v1.3 findings (rule_id, tier, object_id, detail,
evidence) for each of the 400 records of selftest_sample.json.gz, in the order of the sample.  `convpreserve selftest
--oracle v1.3` replays the sample and compares against this file.  The sample itself and its recorded v1.0 findings are
left as they are (their sha256 underlies the throughput benchmark and the 400/400 v1.0 selftest).
usage: python tests/gen_selftest_expected_v13.py [--check]
"""
import argparse
import gzip
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
from convpreserve.cli import _finding_dict  # noqa: E402
from convpreserve.oracles import get_judge  # noqa: E402

DATA = SRC / "convpreserve" / "data"


def build():
    compare = get_judge("v1.3").compare
    recs = json.load(gzip.open(DATA / "selftest_sample.json.gz", "rt", encoding="utf-8"))
    return [[_finding_dict(f) for f in compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"],
                                               context=r.get("context"))] for r in recs]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    raw = json.dumps(build(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    path = DATA / "selftest_expected_v13.json.gz"
    if a.check:
        same = gzip.open(path, "rb").read() == raw
        print("identical" if same else "DIFFERENT")
        return 0 if same else 1
    with open(path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
            g.write(raw)
    print("wrote", path.name, "sha256", hashlib.sha256(path.read_bytes()).hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
