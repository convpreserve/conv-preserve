#!/usr/bin/env python3
"""Generate examples/ for convpreserve from the bundled selftest sample.

For every (side, source dialect, target dialect) pair that has a record with at least
one T1 finding, pick the record with the smallest serialized size (ties: sample order)
and write its payloads to examples/<side>/<src>__<dst>/{src,dst}.json (or .sse for
streams whose bodies are strings) plus examples/manifest.jsonl for `convpreserve batch`.

Deterministic; no network. Usage:  python make_examples.py [OUT_DIR]
"""
import gzip
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "..", "src", "convpreserve", "data", "selftest_sample.json.gz")


def main(out_dir):
    with gzip.open(SAMPLE, "rt", encoding="utf-8") as f:
        records = json.load(f)
    best = {}
    for i, r in enumerate(records):
        if not any(x["tier"] == "T1" for x in r["findings"]):
            continue
        if r["src_dialect"] == r["dst_dialect"]:
            continue
        key = (r["side"], r["src_dialect"], r["dst_dialect"])
        size = len(json.dumps(r["src_body"])) + len(json.dumps(r["dst_body"]))
        if key not in best or size < best[key][0]:
            best[key] = (size, i, r)
    manifest = []
    for (side, sd, dd), (_, i, r) in sorted(best.items()):
        rel = os.path.join(side, f"{sd}__{dd}")
        d = os.path.join(out_dir, rel)
        os.makedirs(d, exist_ok=True)
        files = {}
        for name in ("src", "dst"):
            body = r[name + "_body"]
            if isinstance(body, str):
                fn, text = name + ".sse", body
            else:
                fn, text = name + ".json", json.dumps(body, indent=2, ensure_ascii=False) + "\n"
            with open(os.path.join(d, fn), "w", encoding="utf-8") as fh:
                fh.write(text)
            files[name] = rel.replace(os.sep, "/") + "/" + fn
        line = {"id": f"{side}:{sd}->{dd}", "side": side, "src_dialect": sd,
                "dst_dialect": dd, "src_file": files["src"], "dst_file": files["dst"]}
        if r.get("context"):
            line["context"] = r["context"]
        manifest.append(line)
    with open(os.path.join(out_dir, "manifest.jsonl"), "w", encoding="utf-8") as fh:
        for line in manifest:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    print(f"{len(manifest)} example pairs written to {out_dir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE))
