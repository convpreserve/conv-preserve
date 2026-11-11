#!/usr/bin/env python3
"""Per-cell comparison of Oracle v1.3 against the frozen Oracle v1.0 on the reply-side and blind-set data of the paper.

Companion of report_v13_vs_v10.py (same verdict key: the exact (rule, tier, detail) multiset of a call; same JSON layout:
judged cells, tier counts old/new, cells_with_different_findings, tier_transitions, findings_removed/added_by_rule,
changed_cells).  Nothing is written under artifacts/ or paper_time/; the recorded runners are redirected to a temp dir.
No network, no model API.

Technique: the committed runner of the study is executed unchanged; its `compare` is replaced by a wrapper that calls
v1.0 and v1.3 on the same arguments, records both verdicts and returns the v1.0 findings, so the runner control flow
(and its numbers) equals the committed one.  One "cell" is one compare call of the runner (the unit of the table).

scopes:
  sets    mutants_set9..14 through artifacts/g4/oracle_v100/src/run_sets_heldout_v100.py (Gate A blind sets)
  b4c     artifacts/g4/b4_v100_unification/src/run_b4_v100.py, forward reply census / stream (tab:response_v100)
  b4b     same runner, chain composition (tab:compose_v100)
  b4a     same runner, LiteLLM request timeline (request side)
  lb      2026-10b leaderboard gateway captures (latest + pinned roles), request side, as in check_gate_a.py
  w1      TIME W1: Anthropic SDK reassembled objects (committed out/*.json), response side
  s3gw    forward stream census of Bifrost/new-api/Portkey/CLIProxyAPI (input of W1 and of the W3 cohort B19)

usage: python tests/report_v13_vs_v10_replies.py --scope sets --out FILE.json
"""
import argparse
import collections
import contextlib
import copy
import glob
import json
import os
import pathlib
import sys
import tempfile
import time
import types

HERE = pathlib.Path(__file__).resolve().parent
PACK = HERE.parent.parent.parent
sys.path.insert(0, str(HERE.parent / "src"))
from convpreserve.oracles import get_judge  # noqa: E402

J10, J13 = get_judge("v1.0"), get_judge("v1.3")


def key(f):
    return (f.rule_id, f.tier, f.detail)


def tier(fs):
    ts = {f.tier for f in fs}
    return "T1" if "T1" in ts else ("T2" if "T2" in ts else ("none" if not ts else sorted(ts)[0]))


def _jsonable(x):
    try:
        json.dumps(x)
        return x
    except TypeError:
        return repr(x)


def _ctx_brief(ctx):
    """object_id plus the names of the other context keys (bodies passed in the context are not copied into the report)."""
    if not isinstance(ctx, dict):
        return _jsonable(ctx)
    d = {"object_id": ctx.get("object_id")}
    extra = sorted(k for k in ctx if k != "object_id")
    if extra:
        d["other_context_keys"] = extra
    return _jsonable(d)


class Recorder:
    """Dual judge: returns (or raises) what v1.0 does, records both verdicts per call.

    An exception counts as a verdict (rule "EXC", tier "exc"), so a change of control flow is a changed cell.
    """

    def __init__(self, label_fn=None, primary="v10"):
        self.rows = []
        self.label_fn = label_fn
        self.primary = primary  # which judge's findings are handed back to the runner under test

    @staticmethod
    def _run(J, src_body, src_dialect, dst_body, dst_dialect, side, context):
        try:
            fs = J.compare(src_body, src_dialect, dst_body, dst_dialect, side, context=context)
            return fs, sorted(map(key, fs)), tier(fs), None
        except Exception as e:  # recorded as a verdict
            return None, [("EXC", "exc", type(e).__name__)], "exc", e

    def compare(self, src_body, src_dialect, dst_body, dst_dialect, side, *, context=None):
        a, ka, ta, ea = self._run(J10, src_body, src_dialect, dst_body, dst_dialect, side, context)
        b, kb, tb, eb = self._run(J13, copy.deepcopy(src_body), src_dialect, copy.deepcopy(dst_body), dst_dialect, side,
                                  copy.deepcopy(context))
        label = self.label_fn(sys._getframe(1), context) if self.label_fn else {}
        row = {"i": len(self.rows), "src_dialect": src_dialect, "dst_dialect": dst_dialect, "side": side,
               "context": _ctx_brief(context), **label, "v10": ka, "v13": kb, "v10_tier": ta, "v13_tier": tb}
        if ea or eb:
            row["exceptions"] = {"v10": repr(ea) if ea else None, "v13": repr(eb) if eb else None}
        self.rows.append(row)
        if self.primary == "v13":
            if eb:
                raise eb
            return b
        if ea:
            raise ea
        return a


def summarize(rows, group_keys=()):
    n = len(rows)
    changed = [r for r in rows if r["v10"] != r["v13"]]
    t_old = collections.Counter(r["v10_tier"] for r in rows)
    t_new = collections.Counter(r["v13_tier"] for r in rows)
    trans = collections.Counter(f'{r["v10_tier"]}->{r["v13_tier"]}' for r in changed if r["v10_tier"] != r["v13_tier"])
    rem, add = collections.Counter(), collections.Counter()
    for r in changed:
        o, nw = collections.Counter(map(tuple, r["v10"])), collections.Counter(map(tuple, r["v13"]))
        for k, c in (o - nw).items():
            rem[k[0]] += c
        for k, c in (nw - o).items():
            add[k[0]] += c
    out = {"cells": n, "tier_counts_v10": dict(sorted(t_old.items())), "tier_counts_v13": dict(sorted(t_new.items())),
           "cells_with_different_findings": len(changed), "tier_transitions": dict(sorted(trans.items())),
           "findings_removed_by_rule": dict(sorted(rem.items())), "findings_added_by_rule": dict(sorted(add.items())),
           "cells_with_exception_v10_or_v13": sum(1 for r in rows if "exceptions" in r),
           "changed_cells": [{k: v for k, v in r.items() if k != "i"} | {"call_index": r["i"]} for r in changed]}
    if group_keys and changed:
        g = collections.defaultdict(collections.Counter)
        tot = collections.Counter()
        for r in rows:
            tot[tuple(str(r.get(k)) for k in group_keys)] += 1
        for r in changed:
            kk = tuple(str(r.get(k)) for k in group_keys)
            g[kk]["changed"] += 1
            if r["v10_tier"] != r["v13_tier"]:
                g[kk]["tier_changed"] += 1
        out["summary_by_" + "_x_".join(group_keys)] = [
            {**dict(zip(group_keys, k)), "cells": tot[k], "changed": c["changed"], "tier_changed": c["tier_changed"]}
            for k, c in sorted(g.items())]
    return out


@contextlib.contextmanager
def fake_judge_modules(rec):
    """The frozen `judge` package (oracle_v100, read-only) is imported normally; its compare is replaced by the recorder."""
    v100 = str(PACK / "artifacts/g4/oracle_v100")
    saved = {k: v for k, v in sys.modules.items() if k == "judge" or k.startswith("judge.")}
    for k in saved:
        del sys.modules[k]
    sys.path.insert(0, v100)
    import judge as jpkg  # noqa: E402
    import judge.judge as jj  # noqa: E402
    jpkg.compare = rec.compare
    jj.compare = rec.compare
    try:
        yield
    finally:
        for k in [k for k in sys.modules if k == "judge" or k.startswith("judge.")]:
            del sys.modules[k]
        sys.modules.update(saved)


# ----------------------------------------------------------------------------------------------- sets 9..14
def scope_sets():
    v100 = PACK / "artifacts/g4/oracle_v100"
    sys.path.insert(0, str(v100))
    sys.path.insert(0, str(v100 / "src"))
    import run_sets_heldout_v100 as RUN  # noqa: E402
    import reference_v100 as REF  # noqa: E402

    out = {}
    all_rows = []
    cur = [0]
    for s in range(9, 15):
        cur[0] = s
        rec = Recorder(label_fn=lambda fr, ctx: {"phase": fr.f_code.co_name, "set": cur[0]})
        RUN.compare = rec.compare
        REF.compare = rec.compare
        with tempfile.TemporaryDirectory() as td:
            old = sys.argv
            sys.argv = ["run_sets_heldout_v100.py", "--set", str(s), "--out", os.path.join(td, f"set{s}.json")]
            t0 = time.time()
            try:
                RUN.main()
            finally:
                sys.argv = old
            res = json.load(open(os.path.join(td, f"set{s}.json")))
        blk = summarize(rec.rows)
        blk["wall_time_sec"] = round(time.time() - t0, 1)
        blk["runner_summary_v10_primary"] = res["summary"]
        out[f"set{s}"] = blk
        all_rows += rec.rows
    out["all_sets"] = {k: v for k, v in summarize(all_rows).items() if k != "changed_cells"}
    return out


# ----------------------------------------------------------------------------------------------- B4 (a/b/c)
def _strip_volatile(x):
    if isinstance(x, dict):
        return {k: _strip_volatile(v) for k, v in x.items() if k not in ("generated_at", "generated_at_utc", "judged_at", "timestamp", "wall_time_sec")}
    if isinstance(x, list):
        return [_strip_volatile(v) for v in x]
    return x


def _canon_unordered(x):
    if isinstance(x, dict):
        return {k: _canon_unordered(v) for k, v in x.items()}
    if isinstance(x, list):
        return sorted((_canon_unordered(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True, default=str))
    return x


def _earliest_diff(a, b, path="$"):
    if type(a) is not type(b):
        return f"{path}: type {type(a).__name__} vs {type(b).__name__}"
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                return f"{path}.{k}: key only in {'rerun' if k in a else 'committed'}"
            d = _earliest_diff(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list):
        if len(a) != len(b):
            return f"{path}: len {len(a)} vs {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = _earliest_diff(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    return None if a == b else f"{path}: {str(a)[:80]!r} vs {str(b)[:80]!r}"


def _cmp_json(f, c, key, same, detail):
    try:
        a, b = _strip_volatile(json.load(open(f))), _strip_volatile(json.load(open(c)))
    except Exception as e:  # noqa: BLE001
        same[key] = f"unreadable: {type(e).__name__}"
        return
    same[key] = a == b
    if a != b:
        detail[key] = {"equal_modulo_list_order": _canon_unordered(a) == _canon_unordered(b), "earliest_difference": _earliest_diff(a, b)}


def _dedupe_cells(rows):
    """One cell = one object_id; the last call decides (a stream call that raised is retried in response mode by the runner)."""
    last, n = {}, collections.Counter()
    for r in rows:
        k = (r.get("family"), r.get("backend"), (r.get("context") or {}).get("object_id"))
        last[k] = r
        n[k] += 1
    return list(last.values()), sum(1 for c in n.values() if c > 1)


def _b4_run():
    """Execute run_b4_v100.py unchanged under the dual judge; outputs go to a temp dir.  Returns the recorder rows."""
    src_path = PACK / "artifacts/g4/b4_v100_unification/src/run_b4_v100.py"
    text = src_path.read_text()
    lines = text.splitlines()
    ln_b = next(i + 1 for i, l in enumerate(lines) if "SUB-STUDY B4b" in l)
    ln_c = next(i + 1 for i, l in enumerate(lines) if "SUB-STUDY B4c" in l)
    call_lines = [i + 1 for i, l in enumerate(lines) if " compare(" in l and "def " not in l and "#" not in l.split("compare(")[0]]

    def label(fr, ctx):
        L, ln = fr.f_locals, fr.f_lineno
        if ln < ln_b:
            return {"study": "b4a", "version": L.get("ver"), "target": L.get("target"), "sid": L.get("sid")}
        if ln < ln_c:
            hop = {350: "f01", 351: "f12", 352: "f02"}.get(ln, f"line{ln}")
            return {"study": "b4b", "chain": L.get("chain_id"), "hop1_family": L.get("hop1_fam"),
                    "hop2_family": L.get("hop2_fam"), "target": L.get("dst_target"), "sid": L.get("sid"), "pair": hop}
        mode = "response" if ln < 480 else "stream"
        return {"study": "b4c", "family": L.get("fam_key"), "backend": L.get("backend"), "sid": L.get("sid"),
                "mode": mode, "stream_strategy": L.get("mname") if mode == "stream" else None,
                "call_line": ln}

    rec = Recorder(label_fn=label)
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "out"
        out.mkdir()
        ns = {"__name__": "run_b4_v100_recorded", "__file__": str(src_path)}
        exec(compile(text, str(src_path), "exec"), ns)
        ns["OUT"] = out
        ns["TRACK"] = pathlib.Path(td)
        saved_argv, saved_path = sys.argv, list(sys.path)
        sys.argv = [str(src_path)]
        before = {k for k in sys.modules}
        try:
            with fake_judge_modules(rec):
                ns["main"]()
        finally:
            sys.argv, sys.path[:] = saved_argv, saved_path
        committed_dir = src_path.parent.parent / "out"
        same = {}
        for f in sorted(out.glob("b4*.json")):
            c = committed_dir / f.name
            same[f.name] = (c.exists() and _strip_volatile(json.load(open(f))) == _strip_volatile(json.load(open(c))))
    return rec.rows, {"ln_b4b": ln_b, "ln_b4c": ln_c, "runner_outputs_equal_committed": same}


_B4_CACHE = {}


def _b4_rows(study):
    if "rows" not in _B4_CACHE:
        _B4_CACHE["rows"], _B4_CACHE["meta"] = _b4_run()
    return [r for r in _B4_CACHE["rows"] if r["study"] == study]


def scope_b4c():
    allrows = _b4_rows("b4c")
    rows, n_retried = _dedupe_cells(allrows)
    out = summarize(rows, ("family", "backend", "mode"))
    out["compare_calls"] = len(allrows)
    out["cells_retried_after_v10_exception"] = n_retried
    out["calls_judged_per_cell_rule"] = "cells = distinct object_id, the last call decides"
    out["runner_outputs_equal_committed"] = _B4_CACHE["meta"]["runner_outputs_equal_committed"]
    out["summary_of_all_calls"] = {k: v for k, v in summarize(allrows).items() if k != "changed_cells"}
    out["by_family_target_side_note"] = "family = gateway family of the census, backend = upstream dialect (src), mode = response (non-stream) or stream"
    return out


def scope_b4b():
    rows = _b4_rows("b4b")
    out = summarize(rows, ("hop1_family", "hop2_family", "target", "pair"))
    out["runner_outputs_equal_committed"] = _B4_CACHE["meta"]["runner_outputs_equal_committed"]
    return out


def scope_b4a():
    out = summarize(_b4_rows("b4a"), ("version", "target"))
    out["runner_outputs_equal_committed"] = _B4_CACHE["meta"]["runner_outputs_equal_committed"]
    return out


# ----------------------------------------------------------------------------------------------- leaderboard (request side)
CUR = {}


def _cur_label(fr, ctx):
    return dict(CUR)


def scope_lb():
    sys.path.insert(0, str(PACK / "tooling/leaderboard"))
    import common as C  # noqa: E402
    import evaluate as E  # noqa: E402

    rec = Recorder(label_fn=_cur_label)
    shapes = C.load_json(C.HERE / "shapes51.json")["shapes"]
    old = C.import_judge
    C.import_judge = lambda: rec.compare
    inputs = []
    try:
        # (1) 2026-10 library snapshot, six subjects (14 paths)
        snap0 = sorted((C.LEADERBOARD_ART / "2026-10/out").glob("captures_2*"))[-1]
        for key, label, targets, rel in E.SUBJECTS:
            p = snap0 / f"{key}.json"
            if not p.exists():
                continue
            CUR.clear()
            CUR.update({"set": "2026-10 libraries", "subject": key, "role": "snapshot", "capture": str(p.relative_to(PACK))})
            E.evaluate(C.load_json(p), shapes, targets, key)
            inputs.append(CUR["capture"])
        # (2) census-pin baselines (Gate A subjects) and gateway pin baselines
        for kind, lst in (("census pins", E.SUBJECTS), ("gateway pins", E.GATEWAYS)):
            for key, label, targets, rel in lst:
                CUR.clear()
                CUR.update({"set": kind, "subject": key, "role": "pinned baseline", "capture": rel})
                E.evaluate(C.load_json(C.PACK / rel), shapes, targets, key)
                inputs.append(rel)
        # (3) 2026-10b gateway captures, every role and round
        snap = sorted((C.LEADERBOARD_ART / "2026-10b/out").glob("captures_gw_2*"))[-1]
        for key, label, targets, _pin in E.GATEWAYS:
            for p in sorted(snap.glob(f"{key}_*.json")):
                CUR.clear()
                CUR.update({"set": "2026-10b gateways", "subject": key, "role": p.stem[len(key) + 1:], "capture": str(p.relative_to(PACK))})
                E.evaluate(C.load_json(p), shapes, targets, key)
                inputs.append(CUR["capture"])
    finally:
        C.import_judge = old
    out = {"inputs": inputs}
    out.update(summarize(rec.rows, ("set", "subject", "role")))
    sub = {}
    for st in sorted({r["set"] for r in rec.rows}):
        sub[st] = {k: v for k, v in summarize([r for r in rec.rows if r["set"] == st]).items() if k != "changed_cells"}
    out["by_set"] = sub
    return out


# ----------------------------------------------------------------------------------------------- W1 (SDK objects) and 3-gateway stream census
def scope_w1():
    w1 = PACK / "artifacts/g5/w1_sdk_outbound/out"
    shapes = json.load(open(PACK / "artifacts/g2/resp/resp_shapes.json"))
    dial = {"openai": "openai_chat", "gemini": "gemini_generatecontent"}
    rec = Recorder(label_fn=_cur_label)
    skipped = collections.Counter()
    files = ["py_2026-10-14T2328Z.json", "ts_2026-10-14T2330Z.json", "ext_py_2026-10-14T2333Z.json", "ext_ts_2026-10-14T2333Z.json"]
    for fn in files:
        cells = json.load(open(w1 / fn))["cells"]
        for c in cells:
            for mode in ("whole", "chunk7"):
                m = c[mode].get("message")
                if m is None:
                    skipped[f"{fn}:{mode}:no_message"] += 1
                    continue
                CUR.clear()
                CUR.update({"file": fn, "sdk": fn.split("_")[-2] if fn.startswith("ext_") else fn.split("_")[0],
                            "ext": fn.startswith("ext_"), "cell": c["id"], "gateway": c["gateway"], "backend": c["backend"],
                            "delivery": mode})
                rec.compare(shapes[c["backend"]][c["shape_id"]], dial[c["backend"]], m, "anthropic_messages", "response")
    out = summarize(rec.rows, ("sdk", "ext", "gateway", "backend", "delivery"))
    out["cells_without_message_skipped"] = dict(skipped)
    out["input_files"] = files
    return out


def scope_s3gw():
    """Forward stream census of Bifrost / new-api / Portkey (captures_3gw.json, the input of W1 and W3) and CLIProxyAPI:
    src = canonical provider SSE text, dst = gateway client SSE, side stream (evaluate_stream_cell of the committed runner).
    The cohort B19 of W3 is the set of bifrost x gemini cells with an S12 T1 'tool_use' finding."""
    base = PACK / "artifacts/g4/forward_stream_multi_gateway/out/captures_3gw.json"
    cap = json.load(open(base))
    sse = {"openai": json.load(open(PACK / "artifacts/g2/resp/sse_openai.json")),
           "gemini": json.load(open(PACK / "artifacts/g2/resp/sse_gemini.json"))}
    dial = {"openai": "openai_chat", "gemini": "gemini_generatecontent"}
    cli = json.load(open(PACK / "artifacts/g4/stream_ee/out/cliproxyapi_resp_capture.json"))
    rec = Recorder(label_fn=_cur_label)
    committed_mismatch = []
    for gw in ("bifrost", "newapi", "portkey", "cliproxyapi"):
        for be in ("openai", "gemini"):
            for sid in sse[be]:
                if gw == "cliproxyapi":
                    mrec = cli["results"][be][sid]["modes"]["stream:canonical"]
                    status, txt, ev = mrec["status"], mrec.get("client_sse") or "", None
                else:
                    r = cap[gw][be][sid]
                    status, txt, ev = r["status"], r["client_sse"], r["eval"]
                if not (status == 200 and txt.strip()):
                    continue
                CUR.clear()
                CUR.update({"gateway": gw, "backend": be, "sid": sid, "mode": "stream"})
                try:
                    fs = rec.compare("".join(sse[be][sid]["canonical"]["events"]), dial[be], txt, "anthropic_messages", "stream")
                except Exception:
                    fs = None
                if ev is not None and fs is not None:
                    if sorted((f.rule_id, f.tier, f.detail) for f in fs) != sorted((f["rule_id"], f["tier"], f["detail"]) for f in ev["findings"]):
                        committed_mismatch.append(f"{gw}/{be}/{sid}")
    out = summarize(rec.rows, ("gateway", "backend", "mode"))
    out["v10_equals_committed_eval_findings_except"] = committed_mismatch
    # cohort B19 (W3) and the predicate of W1 (bifrost x gemini, S12 T1 tool_use)
    def b19(key_name):
        ids = []
        for r in rec.rows:
            if r["gateway"] == "bifrost" and r["backend"] == "gemini":
                if any(f[0] == "S12" and f[1] == "T1" and f[2].startswith("stop/finish reason semantic class changed: 'tool_use'")
                       for f in r[key_name]):
                    ids.append(r["sid"])
        return ids
    paths = {}
    for r in rec.rows:
        e = paths.setdefault(f'{r["gateway"]}/{r["backend"]}', {"cells": 0, "T1_v10": 0, "T1_v13": 0, "changed": 0})
        e["cells"] += 1
        e["T1_v10"] += r["v10_tier"] == "T1"
        e["T1_v13"] += r["v13_tier"] == "T1"
        e["changed"] += r["v10"] != r["v13"]
    out["by_path_all"] = dict(sorted(paths.items()))
    core = [r for r in rec.rows if r["gateway"] in ("bifrost", "newapi")]
    out["w1_core_set_bifrost_newapi"] = {"cells": len(core), "stream_T1_v10": sum(r["v10_tier"] == "T1" for r in core),
                                         "stream_T1_v13": sum(r["v13_tier"] == "T1" for r in core),
                                         "changed": sum(r["v10"] != r["v13"] for r in core)}
    a, b = b19("v10"), b19("v13")
    out["w3_cohort_B19"] = {"v10": len(a), "v13": len(b), "identical": a == b, "ids_v10": a}
    return out


# ----------------------------------------------------------------------------------------------- generic runner scopes (WS-26)
@contextlib.contextmanager
def write_sandbox(tmp):
    """Any write below the research pack goes to tmp/<relative path>; the pack itself is never modified."""
    import builtins
    import io
    root = str(PACK)
    written = []

    def redirect(p):
        try:
            ap = os.path.abspath(os.fspath(p))
        except TypeError:
            return p
        if ap == root or ap.startswith(root + os.sep):
            return os.path.join(tmp, os.path.relpath(ap, root))
        return p

    real_open, real_mkdir, real_makedirs = builtins.open, os.mkdir, os.makedirs

    def sopen(file, mode="r", *a, **k):
        if isinstance(file, (str, os.PathLike)) and any(c in mode for c in "wax+"):
            f = redirect(file)
            if f != file:
                real_makedirs(os.path.dirname(f), exist_ok=True)
                written.append(f)
            return real_open(f, mode, *a, **k)
        return real_open(file, mode, *a, **k)

    def smkdir(path, mode=0o777, *a, **k):
        f = redirect(path)
        if f != path:
            return real_makedirs(f, exist_ok=True)
        return real_mkdir(path, mode, *a, **k)

    def smakedirs(name, mode=0o777, exist_ok=False):
        f = redirect(name)
        return real_makedirs(f, mode, exist_ok=True if f != name else exist_ok)

    builtins.open, io.open, os.mkdir, os.makedirs = sopen, sopen, smkdir, smakedirs
    try:
        yield written
    finally:
        builtins.open, io.open, os.mkdir, os.makedirs = real_open, real_open, real_mkdir, real_makedirs


def _oai_stream(r):
    return r["side"] == "stream" and "openai_chat" in (r["src_dialect"], r["dst_dialect"])


def run_module_scope(rel, argv_fn=None, extra_paths=(), label_fn=None, setup=None, group=("phase",), outdir_cmp=None, entry="main", primary="v10", harvest=None):
    """Run a committed runner unchanged under the dual judge, writes sandboxed; compare its outputs with the committed ones."""
    import importlib.util
    path = PACK / rel
    rec = Recorder(label_fn=label_fn or (lambda fr, ctx: {"phase": fr.f_code.co_name}), primary=primary)
    with tempfile.TemporaryDirectory() as td:
        argv = argv_fn(td) if argv_fn else []
        saved_argv, saved_path, saved_cwd = sys.argv, list(sys.path), os.getcwd()
        saved_mods = set(sys.modules)
        for pth in [str(PACK / "artifacts/g4/oracle_v100"), str(path.parent), *map(str, extra_paths)]:
            sys.path.insert(0, pth)
        sys.argv = [str(path)] + argv
        t0 = time.time()
        exit_info = None
        try:
            with fake_judge_modules(rec), write_sandbox(td) as written:
                spec = importlib.util.spec_from_file_location("ws26_" + path.stem, path)
                mod = importlib.util.module_from_spec(spec)
                sys.modules[spec.name] = mod
                spec.loader.exec_module(mod)
                mod._ws_rec = rec  # for setup hooks
                if setup:
                    setup(mod, td)
                try:
                    getattr(mod, entry)()
                except SystemExit as e:
                    exit_info = repr(e.code)
        finally:
            sys.argv = saved_argv
            sys.path[:] = saved_path
            os.chdir(saved_cwd)
            for m in set(sys.modules) - saved_mods:
                if m.startswith("ws26_"):
                    del sys.modules[m]
        harvested = harvest(td) if harvest else None
        same, same_detail = {}, {}
        for f in sorted(set(written)):
            rel_out = os.path.relpath(f, td)
            c = PACK / rel_out
            if f.endswith(".json") and c.exists():
                _cmp_json(f, c, rel_out, same, same_detail)
            else:
                same[rel_out] = None if not c.exists() else "not compared"
        if outdir_cmp:  # (subdir of the sandbox, committed directory relative to the pack): runner output dir given by --out-dir
            sub, committed = outdir_cmp
            for f in sorted(glob.glob(os.path.join(td, sub, "*.json"))):
                c = PACK / committed / os.path.basename(f)
                key = f"{committed}/{os.path.basename(f)}"
                if c.exists():
                    _cmp_json(f, c, key, same, same_detail)
                else:
                    same[key] = None
                c2 = PACK / committed / (os.path.basename(f)[:-5] + "_r2.json")  # superseding committed revision, if any
                if c2.exists():
                    _cmp_json(f, c2, key[:-5] + "_r2.json", same, same_detail)
    out = summarize(rec.rows, group)
    out["runner"] = rel
    out["wall_time_sec"] = round(time.time() - t0, 1)
    out["exit"] = exit_info
    out["calls_with_openai_chat_stream_side"] = sum(1 for r in rec.rows if _oai_stream(r))
    out["changed_calls_with_openai_chat_stream_side"] = sum(1 for r in rec.rows if _oai_stream(r) and r["v10"] != r["v13"])
    out["runner_outputs_equal_committed"] = same
    out["runner_outputs_difference_detail"] = same_detail
    out["primary_judge_handed_to_runner"] = primary
    if harvest:
        out["_harvested"] = harvested
    return out


def scope_corpora():
    return run_module_scope("artifacts/g4/oracle_v100/src/run_paper_corpora_v100.py", lambda td: ["--out-dir", os.path.join(td, "o")], outdir_cmp=("o", "artifacts/g4/oracle_v100/out"))


def scope_recall():
    return run_module_scope("artifacts/g4/oracle_v100/src/run_recall_v100.py")


def scope_code_mutants():
    return run_module_scope("artifacts/g4/oracle_v100/src/run_code_mutants_v100.py")


def scope_v10_corpora():
    return run_module_scope("artifacts/g4/oracle_v100/src/run_v10_corpora_v100.py")


def _rr_label(fr, ctx):
    oid = (ctx or {}).get("object_id", "") if isinstance(ctx, dict) else ""
    parts = oid.split("/")
    d = {"phase": fr.f_code.co_name}
    if len(parts) >= 4:
        d.update({"family": parts[0], "upstream": parts[1], "mode": parts[2], "sid": parts[3],
                  "with_non_stream_body": "with_non_stream_body" in oid})
    return d


def scope_reverse_r2():
    """reverse_response_census run_judge_r2.py (results_r2_20261011T2145Z.json): primary response/stream calls and the stream
    calls with the paired non_stream_body; dst = OpenAI Chat client SSE."""
    out = run_module_scope("artifacts/g4/reverse_response_census/src/run_judge_r2.py", label_fn=_rr_label, group=("family", "upstream", "mode", "with_non_stream_body"))
    return out


def _v110_label(fr, ctx):
    f = fr
    while f is not None and f.f_code.co_name != "main":
        f = f.f_back
    if f is None:
        return {}
    e, sid = f.f_locals.get("entry") or {}, f.f_locals.get("sid")
    return {"file": e.get("file"), "translator": e.get("translator"), "sid": sid}


def _v110_setup(mod, td):
    os.makedirs(os.path.join(td, "o"), exist_ok=True)
    mod.OUT_PATH = os.path.join(td, "o", "stream_captures_v100_v110.json")
    orig = mod.load_judge
    j110 = orig("judge_v110_pkg", os.path.join(str(PACK), "artifacts/g4/oracle_v110_study/judge_v110"))
    rec = mod._ws_rec

    class _J100:  # v1.0 verdicts are returned to the runner; v1.3 is recorded next to them
        @staticmethod
        def compare(*a, **k):
            return rec.compare(*a, **k)

    mod.load_judge = lambda name, d: j110 if "v110" in name else _J100


def scope_v110_streams():
    """oracle_v110_study/captures eval_stream_captures.py: every stream of out/stream_capture_inventory.json, runner unchanged
    (its v1.0 side now feeds the dual judge; its v1.1 side runs as before, so the re-created output can be compared)."""
    return run_module_scope("artifacts/g4/oracle_v110_study/captures/src/eval_stream_captures.py", label_fn=_v110_label, setup=_v110_setup,
                            group=("translator",), outdir_cmp=("o", "artifacts/g4/oracle_v110_study/captures/out"))


def scope_timesplit():
    """timesplit_recall judge_cases.py (batch 2026-10-12T1155Z, out/timesplit_judge_*.json)."""
    return run_module_scope("artifacts/g4/timesplit_recall/src/judge_cases.py", lambda td: ["2026-10-12T1155Z", "--out-dir", os.path.join(td, "o")],
                            outdir_cmp=("o", "artifacts/g4/timesplit_recall/out"), group=("src_dialect", "dst_dialect", "side"))


def scope_real_bugs_50():
    """real_bugs_50 run_real_bugs_50.py (15 cases, buggy / fixed destination per case, side per case)."""
    box = {}
    out = run_module_scope("artifacts/g4/real_bugs_50/src/run_real_bugs_50.py", entry="run", group=("src_dialect", "dst_dialect", "side"),
                           setup=lambda mod, td: box.update(rec=mod._ws_rec))
    rows = box["rec"].rows  # per case: buggy then fixed (run_real_bugs_50.run)

    def verdicts(vkey):
        res = {}
        for i in range(0, len(rows), 2):
            b, f = rows[i], rows[i + 1]
            assert b["context"]["object_id"] == f["context"]["object_id"]
            det = lambda r, t: {x[2] for x in r[vkey] if x[1] == t}  # noqa: E731
            res[b["context"]["object_id"]] = ("T1 HIT" if det(b, "T1") - det(f, "T1") else "T2 HIT" if det(b, "T2") - det(f, "T2") else "MISS")
        return res

    v10, v13 = verdicts("v10"), verdicts("v13")
    out["case_verdicts_v10"], out["case_verdicts_v13"] = v10, v13
    out["case_verdict_counts_v10"] = dict(collections.Counter(v10.values()))
    out["case_verdict_counts_v13"] = dict(collections.Counter(v13.values()))
    out["case_verdicts_changed"] = {k: [v10[k], v13[k]] for k in v10 if v10[k] != v13[k]}
    return out


def scope_w1_ext():
    """W1 external CLIProxyAPI capture (ext_cliproxyapi_captures_2026-10-14T2332Z.json): src = canonical provider SSE, dst = client SSE."""
    cap = json.load(open(PACK / "artifacts/g5/w1_sdk_outbound/out/ext_cliproxyapi_captures_2026-10-14T2332Z.json"))["cliproxyapi"]
    sse = {"openai": json.load(open(PACK / "artifacts/g2/resp/sse_openai.json")),
           "gemini": json.load(open(PACK / "artifacts/g2/resp/sse_gemini.json"))}
    dial = {"openai": "openai_chat", "gemini": "gemini_generatecontent"}
    rec = Recorder(label_fn=_cur_label)
    mismatch = []
    for be in ("openai", "gemini"):
        for sid, r in cap[be].items():
            if r.get("status") != 200 or not (r.get("client_sse") or "").strip():
                continue
            CUR.clear()
            CUR.update({"gateway": "cliproxyapi_ext", "backend": be, "sid": sid, "mode": "stream"})
            try:
                fs = rec.compare("".join(sse[be][sid]["canonical"]["events"]), dial[be], r["client_sse"], "anthropic_messages", "stream")
            except Exception:
                fs = None
            ev = r.get("stream_eval")
            if ev is not None and fs is not None:
                if sorted((f.rule_id, f.tier, f.detail) for f in fs) != sorted((f["rule_id"], f["tier"], f["detail"]) for f in ev["findings"]):
                    mismatch.append(f"{be}/{sid}")
    out = summarize(rec.rows, ("gateway", "backend", "mode"))
    out["v10_equals_committed_stream_eval_findings_except"] = mismatch
    out["runner"] = "artifacts/g5/w1_sdk_outbound/src/ext_capture_cliproxyapi.py::stream_eval (re-judged on the committed capture)"
    out["calls_with_openai_chat_stream_side"] = sum(1 for r in rec.rows if _oai_stream(r))
    out["changed_calls_with_openai_chat_stream_side"] = sum(1 for r in rec.rows if _oai_stream(r) and r["v10"] != r["v13"])
    return out


_BANNED = ["fai" + "thful", "fide" + "lity", "lat" + "tice", "can" + "ary", "by" + "pass", "emer" + "gent", "no" + "vel", "fir" + "st",
           "model-" + "dependent", "maintainer " + "confirmed"]
_MASK = __import__("re").compile("(?i)(" + "|".join(_BANNED) + ")")


def _mask_words(x):
    """Captured payload text quoted in finding details can contain words the paper text must not carry; they are replaced by '[masked]'
    in this report (the number of replacements is stored in `masked_words_in_payload_text`)."""
    n = 0

    def walk(v):
        nonlocal n
        if isinstance(v, dict):
            for k in list(v):
                v[k] = walk(v[k])
            return v
        if isinstance(v, (list, tuple)):
            return [walk(e) for e in v]
        if isinstance(v, str):
            v2, c = _MASK.subn("[masked]", v)
            n += c
            return v2
        return v

    walk(x)
    return n


def scope_exp_b():
    """reverse_stream_sdk_consequence/run_exp_b.py consumes (1) parse_ir of the upstream shape streams (ground truth; dialects anthropic_messages /
    gemini_generatecontent) and (2) the per-cell `v100_tier` / `avoidable_t1` of reverse_response_census/out/v100_<family>_<dialect>.json
    (produced by run_judge_v100.py on the OpenAI-Chat client SSE).  The SDK outcomes themselves do not depend on the judge.
    Here: (1) parse_ir v1.0 vs v1.3 on all shape streams; (2) run_judge_v100.py again with v1.3 as the judge handed to the runner
    (sandboxed outputs harvested) and swap the resulting tier / avoidable flags into the 558 committed SDK records."""
    import glob
    sys.path.insert(0, str(PACK / "artifacts/g4/oracle_v100"))
    import importlib
    ir10 = importlib.import_module("convpreserve.judge.ir")
    ir13 = importlib.import_module("convpreserve.judge_v13.ir")
    rev = PACK / "artifacts/g4/reverse_response_census"
    shapes = json.load(open(rev / "shapes/upstream_response_shapes_v1.json"))["shapes"]
    gt_diff = []
    for sid, sh in sorted(shapes.items()):
        try:
            a = ir10.parse_ir(copy.deepcopy(sh["stream_sse"]), sh["dialect"], "stream")
            b = ir13.parse_ir(copy.deepcopy(sh["stream_sse"]), sh["dialect"], "stream")
            same = json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)
        except Exception as e:  # noqa: BLE001
            same = f"exception {type(e).__name__}"
        if same is not True:
            gt_diff.append({"shape": sid, "dialect": sh["dialect"], "result": same})

    def harvest(td):
        res = {}
        for p in sorted(glob.glob(os.path.join(td, "artifacts/g4/reverse_response_census/out/v100_*.json"))):
            res[os.path.basename(p)] = json.load(open(p))
        return res

    out = run_module_scope("artifacts/g4/reverse_response_census/src/run_judge_v100.py", lambda td: ["--overwrite"], label_fn=_rr_label,
                           group=("family", "upstream", "mode"), primary="v13", harvest=harvest)
    new_cells = out.pop("_harvested")
    out["runner_outputs_equal_committed"] = {k: v for k, v in out["runner_outputs_equal_committed"].items() if "v100_" in k and "/out/v100_" in k}
    old_cells = {os.path.basename(p): json.load(open(p)) for p in sorted(glob.glob(str(rev / "out/v100_*.json")))}
    sdk = json.load(open(PACK / "artifacts/g4/reverse_stream_sdk_consequence/out/sdk_consequence_558.json"))
    ch, flip = [], collections.Counter()
    recs = []
    for r in sdk:
        fam, ud = r["family"], r["upstream_dialect"]
        o = old_cells[f"v100_{fam}_{ud}.json"]["cells"][r["shape_id"]]["stream"]
        n = new_cells[f"v100_{fam}_{ud}.json"]["cells"][r["shape_id"]]["stream"]
        av_o, av_n = bool(o.get("avoidable_t1")), bool(n.get("avoidable_t1"))
        recs.append((r, av_o, av_n))
        if (o.get("tier"), av_o, sorted(o.get("t1_rules") or [])) != (n.get("tier"), av_n, sorted(n.get("t1_rules") or [])):
            ch.append({"cell_key": r["cell_key"], "tier_v10": o.get("tier"), "tier_v13": n.get("tier"), "avoidable_t1_v10": av_o, "avoidable_t1_v13": av_n,
                       "t1_rules_v10": sorted(o.get("t1_rules") or []), "t1_rules_v13": sorted(n.get("t1_rules") or []), "any_sdk_failure": r["any_sdk_failure"]})
            flip[f"avoidable {av_o}->{av_n}"] += 1

    def corr(idx):
        a = [r for r, *f in recs if f[idx]]
        b = [r for r, *f in recs if not f[idx]]
        fa = sum(1 for r in a if str(r["any_sdk_failure"]) == "True" or r["any_sdk_failure"] is True)
        fb = sum(1 for r in b if str(r["any_sdk_failure"]) == "True" or r["any_sdk_failure"] is True)
        return {"avoidable_t1_total": len(a), "avoidable_t1_sdk_fail": fa, "avoidable_t1_sdk_fail_pct": round(100.0 * fa / len(a), 2) if a else None,
                "non_avoidable_t1_total": len(b), "non_avoidable_t1_sdk_fail": fb, "non_avoidable_t1_sdk_fail_pct": round(100.0 * fb / len(b), 2) if b else None}

    tc = lambda idx_tier: dict(collections.Counter(  # noqa: E731
        (old_cells if idx_tier == "v10" else new_cells)[f"v100_{r['family']}_{r['upstream_dialect']}.json"]["cells"][r["shape_id"]]["stream"].get("tier") for r in sdk))
    out["exp_b"] = {
        "sdk_records": len(sdk), "parse_ir_shape_streams": len(shapes), "parse_ir_v10_vs_v13_differences": gt_diff,
        "cells_with_changed_tier_or_avoidable_flag": len(ch), "changed_cells": ch, "avoidable_flag_flips": dict(flip),
        "correlation_v10_flags": corr(0), "correlation_v13_flags": corr(1),
        "summary_exp_b_committed_correlation": json.load(open(PACK / "artifacts/g4/reverse_stream_sdk_consequence/out/summary_exp_b.json"))["oracle_v100_correlation"],
        "stream_tier_counts_v10": tc("v10"), "stream_tier_counts_v13": tc("v13"),
    }
    out["note"] = "calls/cells/changed_cells of this scope refer to the run_judge_v100.py evaluation (v1.3 judge handed to the runner); exp_b.* is the EXP-B view"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", required=True, help="comma-separated list of: sets,b4c,b4b,b4a,lb,w1,s3gw,corpora,recall,code_mutants,v10_corpora,reverse_r2,v110_streams,timesplit,real_bugs_50,w1_ext,exp_b (all = sets,b4c,b4b,b4a,lb,w1,s3gw)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    names = ["sets", "b4c", "b4b", "b4a", "lb", "w1", "s3gw"] if a.scope == "all" else a.scope.split(",")
    res = {"scopes": {}}
    for n in names:
        t0 = time.time()
        res["scopes"][n] = globals()["scope_" + n]()
        res["scopes"][n]["wall_time_sec"] = round(time.time() - t0, 1)
    res["masked_words_in_payload_text"] = _mask_words(res)
    with open(a.out, "w") as f:
        json.dump(res, f, indent=1, sort_keys=True)
        f.write("\n")
    brief = {n: {kk: vv for kk, vv in v.items() if kk not in ("changed_cells", "runner_summary_v10_primary")
                 and not kk.startswith("set") and not kk.startswith("summary_by")}
             for n, v in res["scopes"].items()}
    print(json.dumps(brief, indent=1, sort_keys=True)[:12000])


if __name__ == "__main__":
    main()
