#!/usr/bin/env python3
"""Report of what Oracle v1.3 changes relative to the frozen Oracle v1.0, on committed inputs only (no network, no model API).

Three inputs:
  sample          the 400-record selftest sample: verdicts (rule, tier, detail) of v1.0 vs v1.3, and the evidence pointer of every
                  v1.3 finding (does it resolve in the judged payload? precision per rule).
  census_request  the six census subjects of Gate A (request side, tooling/leaderboard/evaluate.py pinned_baseline) re-judged with
                  v1.0 and v1.3: cell-level equality.
  reverse_response  artifacts/g4/reverse_response_census/out/captures_*.json (7 gateway families x 2 upstream dialects x 40 shapes x
                  response/stream) judged as in run_judge_v100.py with both oracles; every cell whose findings differ is listed.
The 2026-10b leaderboard captures are not re-judged here.
usage: python tests/report_v13_vs_v10.py [--out FILE.json]
"""
import argparse
import collections
import glob
import gzip
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PACK = HERE.parent.parent.parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(PACK / "tooling/leaderboard"))
from convpreserve.judge_v13 import evidence as EV  # noqa: E402
from convpreserve.oracles import get_judge  # noqa: E402
import common as C  # noqa: E402
import evaluate as E  # noqa: E402

J10, J13 = get_judge("v1.0"), get_judge("v1.3")


def key(f):
    return (f.rule_id, f.tier, f.detail)


def sample_part():
    recs = json.load(gzip.open(HERE.parent / "src/convpreserve/data/selftest_sample.json.gz", "rt", encoding="utf-8"))
    diffs, bad, prec, n_find = 0, [], collections.defaultdict(collections.Counter), 0
    for i, r in enumerate(recs):
        a = J10.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
        b = J13.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
        if sorted(map(key, a)) != sorted(map(key, b)):
            diffs += 1
        for f in b:
            n_find += 1
            ev = f.evidence
            prec[f.rule_id][ev["precision"]] += 1
            body, d = (r["src_body"], r["src_dialect"]) if ev["side"] == "src" else (r["dst_body"], r["dst_dialect"])
            try:
                EV.resolve_pointer(EV._doc(body, r["side"]), ev["path"])
            except (KeyError, IndexError, TypeError, ValueError):
                bad.append({"record": i, "rule": f.rule_id, "evidence": ev})
    return {"records": len(recs), "findings_v13": n_find, "records_with_different_verdicts": diffs,
            "unresolvable_evidence_pointers": len(bad), "unresolvable": bad[:10],
            "evidence_precision_by_rule": {r: dict(sorted(c.items())) for r, c in sorted(prec.items())}}


def census_request_part():
    shapes = C.load_json(C.HERE / "shapes51.json")["shapes"]

    def base(compare):
        old = C.import_judge
        C.import_judge = lambda: compare
        try:
            return E.pinned_baseline(shapes)
        finally:
            C.import_judge = old

    a, b = base(J10.compare), base(J13.compare)
    return {"gate_a_v10": E.gate_a(a)["observed"], "gate_a_v13": E.gate_a(b)["observed"],
            "cell_level_identical": json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)}


def tier(fs):
    t = {f.tier for f in fs}
    return "T1" if "T1" in t else "T2" if "T2" in t else "T3" if "T3" in t else "none"


def reverse_part():
    cells, changed = 0, []
    tot = collections.Counter()
    for path in sorted(glob.glob(str(PACK / "artifacts/g4/reverse_response_census/out/captures_*.json"))):
        cap = C.load_json(pathlib.Path(path))
        shapes = C.load_json(PACK / "artifacts/g4/reverse_response_census" / cap["shapes_file"])["shapes"]
        fam, dialect = cap["family"], cap["upstream_dialect"]
        for sid, res in sorted(cap["results"].items()):
            for side in ("response", "stream"):
                rs = res[side]
                body = rs.get("body") if side == "response" else rs.get("raw_sse")
                if rs.get("http_status") != 200 or body is None:
                    continue
                src = shapes[sid]["non_stream"] if side == "response" else shapes[sid]["stream_sse"]
                ctx = {"object_id": f"{fam}/{dialect}/{side}/{sid}"}
                a = J10.compare(src, dialect, body, "openai_chat", side, context=ctx)
                b = J13.compare(src, dialect, body, "openai_chat", side, context=ctx)
                cells += 1
                tot[f"{side}_{tier(a)}_v10"] += 1
                tot[f"{side}_{tier(b)}_v13"] += 1
                if sorted(map(key, a)) != sorted(map(key, b)):
                    ka, kb = collections.Counter(map(key, a)), collections.Counter(map(key, b))
                    changed.append({"cell": ctx["object_id"], "side": side, "tier_v10": tier(a), "tier_v13": tier(b),
                                    "removed": [list(k) for k in (ka - kb)], "added": [list(k) for k in (kb - ka)]})
    trans = collections.Counter(f"{c['tier_v10']}->{c['tier_v13']}" for c in changed)
    removed_rules = collections.Counter(k[0] for c in changed for k in c["removed"])
    added_rules = collections.Counter(k[0] for c in changed for k in c["added"])
    return {"judged_cells": cells, "tier_counts": dict(sorted(tot.items())), "cells_with_different_findings": len(changed),
            "tier_transitions": dict(sorted(trans.items())), "findings_removed_by_rule": dict(sorted(removed_rules.items())),
            "findings_added_by_rule": dict(sorted(added_rules.items())), "changed_cells": changed}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    a = ap.parse_args()
    out = {"oracles": ["v1.0", "v1.3"], "sample": sample_part(), "census_request": census_request_part(),
           "reverse_response_census": reverse_part()}
    txt = json.dumps(out, indent=1, sort_keys=True) + "\n"
    if a.out:
        pathlib.Path(a.out).write_text(txt, encoding="utf-8")
    s = out["sample"]
    print("sample:", s["records"], "records;", s["records_with_different_verdicts"], "with different verdicts;",
          s["unresolvable_evidence_pointers"], "unresolvable evidence pointers")
    print("census_request:", out["census_request"])
    r = out["reverse_response_census"]
    print("reverse_response_census:", r["judged_cells"], "cells;", r["cells_with_different_findings"], "differ;", r["tier_transitions"],
          "removed", r["findings_removed_by_rule"], "added", r["findings_added_by_rule"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
