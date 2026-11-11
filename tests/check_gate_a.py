#!/usr/bin/env python3
"""Gate A check of the convpreserve package: re-judge the committed census-pin captures and the committed 2026-10b gateway captures with the
judge vendored in this package and compare with (a) the frozen Oracle v1.0 judge and (b) the committed numbers.

Needs a checkout of the research repository (artifacts/ and tooling/leaderboard/); no network, no model API.
usage: python tests/check_gate_a.py [--snapshot 2026-10b] [--out FILE.json]
Exit 0 iff Gate A (sent 714 / refused 29 / judged 685 / T1 cells 489 on the six census subjects) holds with the vendored judge, the vendored judge
and the frozen judge give identical cell-level results, and the re-judged 2026-10b gateway captures equal the committed `gateways_*.json`.
"""
import argparse
import glob
import importlib.util
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PACK = HERE.parent.parent.parent
sys.path.insert(0, str(PACK / "tooling/leaderboard"))
import common as C  # noqa: E402
import evaluate as E  # noqa: E402

FROZEN = C.import_judge()
# Load the vendored package by path under an alias, so the check judges the checkout's source and not whichever convpreserve is installed.
_init = PACK / "src/convpreserve/__init__.py"
_spec = importlib.util.spec_from_file_location("convpreserve_vendored", _init, submodule_search_locations=[str(_init.parent)])
_mod = importlib.util.module_from_spec(_spec)
sys.modules["convpreserve_vendored"] = _mod
_spec.loader.exec_module(_mod)
import convpreserve_vendored.judge.judge as _vj  # noqa: E402

CONVPRESERVE = _vj.compare


def with_judge(fn, compare):
    old = C.import_judge
    C.import_judge = lambda: compare
    try:
        return fn()
    finally:
        C.import_judge = old


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="2026-10b")
    ap.add_argument("--out")
    a = ap.parse_args()
    shapes = C.load_json(C.HERE / "shapes51.json")["shapes"]

    def baselines():
        return {"libraries": E.pinned_baseline(shapes), "gateways": E.gateway_baseline(shapes)}

    fr = with_judge(baselines, FROZEN)
    cv = with_judge(baselines, CONVPRESERVE)
    gate = E.gate_a(cv["libraries"])
    same_cells = json.dumps(fr, sort_keys=True) == json.dumps(cv, sort_keys=True)
    # re-judge the committed gateway captures of the snapshot (latest c1 and pinned c1) and compare with the committed result file
    snap = C.LEADERBOARD_ART / a.snapshot / "out"
    res_file = sorted(glob.glob(str(snap / "gateways_2*.json")))[-1]
    committed = C.load_json(res_file)["gateways"]
    gw_rows, gw_ok = {}, True
    for key, label, targets, _pin in E.GATEWAYS:
        for role in ("latest", "pinned"):
            ent = committed[key].get(role) or {}
            if "capture" not in ent:
                continue
            cap = C.load_json(snap / ent["capture"])
            ev = with_judge(lambda: E.evaluate(cap, shapes, targets, key), CONVPRESERVE)
            ok = json.dumps(ev, sort_keys=True) == json.dumps({t: {k: v for k, v in x.items() if k != "refusal_reasons"} for t, x in ent["targets"].items()}, sort_keys=True)
            gw_rows[f"{key}/{role}"] = {"capture": ent["capture"], "equals_committed": ok,
                                        "summary": {t: ev[t]["summary"] for t in targets}}
            gw_ok &= ok
    out = {"gate_a": gate, "frozen_judge_vs_convpreserve_cell_level_identical": same_cells,
           "gateway_pin_baseline_summary": {k: {t: v2["summary"] for t, v2 in v["targets"].items()} for k, v in cv["gateways"].items()},
           "snapshot": a.snapshot, "committed_result_file": str(pathlib.Path(res_file).relative_to(PACK)), "gateway_rejudge": gw_rows,
           "pass": bool(gate["pass"] and same_cells and gw_ok)}
    print(json.dumps({"gate_a": gate, "frozen_vs_convpreserve_identical": same_cells, "gateway_rejudge_equals_committed": gw_ok, "pass": out["pass"]}, indent=1))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(out, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
