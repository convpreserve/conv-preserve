#!/usr/bin/env python3
"""Stage breakdown and scale of convpreserve throughput (answers: what does the pairs/s number include?).

bench/throughput.py times only `compare()` on records that are already parsed in memory.  This script separates the
stages of a real run and scales the input:

  startup  process start up to a usable CLI: `python -c pass` and `python -m convpreserve version` (wall clock, subprocess)
  read     reading the payload bytes from disk (one JSONL manifest with inline payloads, or one file per payload)
  parse    json.loads of the bytes (for a stream pair the SSE text is only decoded here; its parsing into events is
           part of `compare`)
  judge    `compare()` on the parsed payloads (this is what bench/throughput.py times)
  e2e      `python -m convpreserve batch MANIFEST --fail-on none`, stdout to /dev/null, wall clock including startup

at 400, 4,000 and 40,000 pairs (the 400-pair selftest sample repeated; each stage over the whole set, so a rate is
pairs divided by the median stage time), plus single requests with a growing multi-turn history (synthetic text turns
and synthetic tool-call turns, same history on both sides).  Every timed quantity is repeated; the output reports
median, quartiles, minimum and maximum.  Standard library only, no network, temporary files only.

    python bench/scale_throughput.py --json OUT.json [--oracle v1.3] [--runs 7] [--runs-large 5] [--scales 400,4000,40000]

    python bench/scale_throughput.py --render OUT1.json [OUT2.json ...]     # markdown tables from saved results

Run it from tooling/convpreserve with PYTHONPATH=src (or with convpreserve installed).
"""
import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

import convpreserve
from convpreserve import __version__
from convpreserve.oracles import get_judge

PKG = os.path.dirname(os.path.abspath(convpreserve.__file__))
SAMPLE = os.path.join(PKG, "data", "selftest_sample.json.gz")
CARRIERS = os.path.join(PKG, "data", "carriers.json")


def _cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def stats(xs):
    xs = sorted(xs)
    q = statistics.quantiles(xs, n=4, method="inclusive") if len(xs) >= 2 else [xs[0]] * 3
    return {"n": len(xs), "median": statistics.median(xs), "q1": q[0], "q3": q[2], "iqr": q[2] - q[0],
            "min": xs[0], "max": xs[-1], "runs": xs}


def rate_stats(n_pairs, times):
    """Pairs per second computed from each run, summarised like `stats`."""
    return stats([n_pairs / t for t in times])


# ---------------------------------------------------------------- inputs

def wire_payload(body, side, which):
    """Bytes of one payload as a file would hold them, and its kind: JSON text or raw SSE text."""
    if isinstance(body, str):          # raw SSE text of a stream destination
        return body.encode("utf-8"), "sse"
    return json.dumps(body, separators=(",", ":")).encode("utf-8"), "json"


def count_events(body):
    """SSE events (`data:` lines) of a raw SSE payload; 0 for a JSON payload."""
    if not isinstance(body, str):
        return 0
    return sum(1 for ln in body.split("\n") if ln.startswith("data:"))


def load_sample():
    with gzip.open(SAMPLE, "rt", encoding="utf-8") as f:
        return json.load(f)


def make_pairs(sample, n):
    return [sample[i % len(sample)] for i in range(n)]


def describe(pairs):
    nbytes = {"request": 0, "response": 0, "stream": 0}
    count = {"request": 0, "response": 0, "stream": 0}
    events = 0
    total = 0
    for r in pairs:
        b = (len(wire_payload(r["src_body"], r["side"], "src")[0]) + len(wire_payload(r["dst_body"], r["side"], "dst")[0]))
        nbytes[r["side"]] += b
        count[r["side"]] += 1
        total += b
        events += count_events(r["src_body"]) + count_events(r["dst_body"])
    return {"pairs": len(pairs), "input_bytes": total, "bytes_per_pair": total / len(pairs), "sse_events": events,
            "pairs_by_side": count, "bytes_by_side": nbytes}


def write_inline_manifest(pairs, path):
    with open(path, "w", encoding="utf-8") as f:
        for i, r in enumerate(pairs):
            f.write(json.dumps({"id": f"p{i}", "side": r["side"], "src_dialect": r["src_dialect"],
                                "dst_dialect": r["dst_dialect"], "src": r["src_body"], "dst": r["dst_body"],
                                "context": r.get("context")}) + "\n")


def write_file_manifest(pairs, root):
    """One file per payload (stream destination .sse, everything else .json) and a manifest of paths."""
    man = os.path.join(root, "manifest.jsonl")
    with open(man, "w", encoding="utf-8") as mf:
        for i, r in enumerate(pairs):
            d = os.path.join(root, f"p{i}")
            os.makedirs(d)
            files = {}
            for key, body in (("src", r["src_body"]), ("dst", r["dst_body"])):
                data, kind = wire_payload(body, r["side"], key)
                name = f"{key}.sse" if kind == "sse" else f"{key}.json"
                with open(os.path.join(d, name), "wb") as f:
                    f.write(data)
                files[key + "_file"] = f"p{i}/{name}"
            mf.write(json.dumps({"id": f"p{i}", "side": r["side"], "src_dialect": r["src_dialect"],
                                 "dst_dialect": r["dst_dialect"], "context": r.get("context"), **files}) + "\n")
    return man


# ---------------------------------------------------------------- stages

def stage_inline(manifest, judge, runs):
    """read / parse / judge over a manifest with inline payloads; each stage runs over the whole set."""
    read, parse, jd, nfind = [], [], [], 0
    for _ in range(runs):
        t0 = time.perf_counter()
        with open(manifest, "rb") as f:
            lines = f.read().split(b"\n")
        t1 = time.perf_counter()
        recs = [json.loads(ln) for ln in lines if ln.strip()]
        t2 = time.perf_counter()
        nfind = 0
        for rec in recs:
            nfind += len(judge.compare(rec["src"], rec["src_dialect"], rec["dst"], rec["dst_dialect"], rec["side"],
                                       context=rec.get("context")))
        t3 = time.perf_counter()
        read.append(t1 - t0)
        parse.append(t2 - t1)
        jd.append(t3 - t2)
    return {"read": read, "parse": parse, "judge": jd, "findings": nfind}


def stage_files(manifest, judge, runs):
    """read / parse / judge with one file per payload (the `check` and `batch --src_file` path)."""
    base = os.path.dirname(manifest)
    with open(manifest, encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f if ln.strip()]
    read, parse, jd, nfind = [], [], [], 0
    for _ in range(runs):
        t0 = time.perf_counter()
        raw = [(open(os.path.join(base, r["src_file"]), "rb").read(), open(os.path.join(base, r["dst_file"]), "rb").read())
               for r in recs]
        t1 = time.perf_counter()
        bodies = []
        for r, (s, d) in zip(recs, raw):
            side = r["side"]
            def dec(b, name):
                if side == "stream" and not name.endswith(".json"):
                    return b.decode("utf-8")
                return json.loads(b)
            bodies.append((dec(s, r["src_file"]), dec(d, r["dst_file"])))
        t2 = time.perf_counter()
        nfind = 0
        for r, (s, d) in zip(recs, bodies):
            nfind += len(judge.compare(s, r["src_dialect"], d, r["dst_dialect"], r["side"], context=r.get("context")))
        t3 = time.perf_counter()
        read.append(t1 - t0)
        parse.append(t2 - t1)
        jd.append(t3 - t2)
    return {"read": read, "parse": parse, "judge": jd, "findings": nfind}


def stage_summary(n, st):
    out = {"findings": st["findings"]}
    for k in ("read", "parse", "judge"):
        out[k + "_seconds"] = stats(st[k])
        out[k + "_pairs_per_s"] = rate_stats(n, st[k])
    tot = [a + b + c for a, b, c in zip(st["read"], st["parse"], st["judge"])]
    out["read_parse_judge_seconds"] = stats(tot)
    out["read_parse_judge_pairs_per_s"] = rate_stats(n, tot)
    med = out["read_parse_judge_seconds"]["median"]
    out["share_of_median_total"] = {k: out[k + "_seconds"]["median"] / med for k in ("read", "parse", "judge")}
    return out


def run_wall(cmd, runs, env=None, stdout=subprocess.DEVNULL):
    ts, rc = [], None
    for _ in range(runs):
        t0 = time.perf_counter()
        p = subprocess.run(cmd, stdout=stdout, stderr=subprocess.DEVNULL, env=env)
        ts.append(time.perf_counter() - t0)
        rc = p.returncode
    return ts, rc


# ---------------------------------------------------------------- long history

def history_request(k, kind):
    """Same multi-turn history on both sides; `kind` text: k user/assistant text turns; tool: k tool-call turns."""
    if kind == "text":
        msgs = []
        for i in range(k):
            msgs.append({"role": "user", "content": f"question {i}: what is the weather in Paris today?"})
            msgs.append({"role": "assistant", "content": f"answer {i}: it is sunny, 21 degrees, light wind."})
        msgs.append({"role": "user", "content": "thanks"})
        return {"model": "m", "max_tokens": 256, "messages": msgs}, {"model": "m", "max_tokens": 256, "messages": list(msgs)}
    oa, an = [], []
    tools_oa = [{"type": "function", "function": {"name": "get_weather", "description": "weather",
                                                  "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]
    tools_an = [{"name": "get_weather", "description": "weather",
                 "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}]
    for i in range(k):
        oa.append({"role": "user", "content": f"question {i}: weather in Paris?"})
        oa.append({"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{i}", "type": "function",
                   "function": {"name": "get_weather", "arguments": json.dumps({"city": "Paris"})}}]})
        oa.append({"role": "tool", "tool_call_id": f"call_{i}", "content": "sunny, 21 degrees"})
        oa.append({"role": "assistant", "content": f"answer {i}: sunny."})
        an.append({"role": "user", "content": f"question {i}: weather in Paris?"})
        an.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{i}", "name": "get_weather",
                                                     "input": {"city": "Paris"}}]})
        an.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{i}", "content": "sunny, 21 degrees"}]})
        an.append({"role": "assistant", "content": f"answer {i}: sunny."})
    oa.append({"role": "user", "content": "thanks"})
    an.append({"role": "user", "content": "thanks"})
    return ({"model": "m", "max_tokens": 256, "messages": oa, "tools": tools_oa},
            {"model": "m", "max_tokens": 256, "messages": an, "tools": tools_an})


def history_run(judge, ks, kinds, runs):
    rows = []
    for kind in kinds:
        for k in ks:
            src, dst = history_request(k, kind)
            sb, db = json.dumps(src, separators=(",", ":")).encode(), json.dumps(dst, separators=(",", ":")).encode()
            parse_t, judge_t, nfind, rules = [], [], 0, {}
            for _ in range(runs):
                t0 = time.perf_counter()
                s, d = json.loads(sb), json.loads(db)
                t1 = time.perf_counter()
                fs = judge.compare(s, "openai_chat", d, "anthropic_messages" if kind == "tool" else "openai_chat", "request", context={})
                t2 = time.perf_counter()
                parse_t.append(t1 - t0)
                judge_t.append(t2 - t1)
                nfind = len(fs)
                rules = {}
                for f in fs:
                    rules[f.rule_id] = rules.get(f.rule_id, 0) + 1
            nmsg = len(src["messages"])
            rows.append({"kind": kind, "turns": k, "messages_per_side": nmsg, "input_bytes": len(sb) + len(db),
                         "findings": nfind, "findings_by_rule": rules,
                         "parse_seconds": stats(parse_t), "judge_seconds": stats(judge_t),
                         "judge_ms_per_message": 1000 * statistics.median(judge_t) / nmsg})
    return rows


# ---------------------------------------------------------------- main

def _f(x, nd=1):
    return f"{x:,.{nd}f}"


def _iqr(st, nd=1):
    return f"{_f(st['median'], nd)} ({_f(st['q1'], nd)}-{_f(st['q3'], nd)})"


def render(paths):
    """Markdown tables from saved result files.  Each cell is median (Q1-Q3) over the runs."""
    res = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            res.append((os.path.basename(p), json.load(f)))
    lines = []
    lines.append("| file | python | oracle (scales/history/e2e) | runs | run start UTC | load avg 1 min (start-end) |")
    lines.append("|---|---|---|---|---|---|")
    for name, d in res:
        lines.append(f"| `{name}` | {d['python']} | {d['oracle_for_scales']} | {d['runs']}/{d['runs_large']} | "
                     f"{d['timestamp_utc']} | {d['loadavg_start'][0]:.2f}-{d['loadavg_end'][0]:.2f} |")
    lines += ["", "Startup, wall clock in ms, median (Q1-Q3) over 10 runs:", "",
              "| file | `python -c pass` | `import convpreserve.cli` | `convpreserve version` | `convpreserve check` one pair |",
              "|---|---|---|---|---|"]
    for name, d in res:
        c = [_iqr({k: v * 1000 for k, v in d[key].items() if k in ("median", "q1", "q3")}, 1)
             for key in ("startup_python_empty_seconds", "startup_import_cli_seconds",
                         "startup_convpreserve_version_seconds", "e2e_check_one_pair_seconds")]
        lines.append(f"| `{name}` | " + " | ".join(c) + " |")
    lines += ["", "Compare only, 400 parsed pairs (same protocol as `bench/throughput.py`), pairs/s, median (Q1-Q3):", "",
              "| file | v1.0 judge | v1.3 judge | v1.3 / v1.0 |", "|---|---|---|---|"]
    for name, d in res:
        a = d["compare_only_400"]["v1.0"]["pairs_per_s"]
        b = d["compare_only_400"]["v1.3"]["pairs_per_s"]
        lines.append(f"| `{name}` | {_iqr(a, 0)} | {_iqr(b, 0)} | {b['median'] / a['median']:.3f} |")
    for name, d in res:
        lines += ["", f"Scales, `{name}` (oracle {d['oracle_for_scales']}); seconds for one pass over the whole set, "
                  "median (Q1-Q3); share = stage median / (read+parse+judge medians):", "",
                  "| pairs | input MB | layout | read s | parse s | judge s | judge pairs/s | read+parse+judge pairs/s | "
                  "share read/parse/judge | e2e `batch` s | e2e pairs/s | findings |",
                  "|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for n, e in d["scales"].items():
            for layout, ek in (("inline manifest", "inline"), ("file per payload", "file_per_payload")):
                s_ = e[ek]
                w = e["e2e_batch_inline" if ek == "inline" else "e2e_batch_files"]
                sh = s_["share_of_median_total"]
                lines.append(
                    f"| {int(n):,} | {e['description']['input_bytes'] / 1e6:.2f} | {layout} | "
                    f"{_iqr(s_['read_seconds'], 3)} | {_iqr(s_['parse_seconds'], 3)} | {_iqr(s_['judge_seconds'], 3)} | "
                    f"{_iqr(s_['judge_pairs_per_s'], 0)} | {_iqr(s_['read_parse_judge_pairs_per_s'], 0)} | "
                    f"{sh['read']:.3f}/{sh['parse']:.3f}/{sh['judge']:.3f} | {_iqr(w['seconds'], 3)} | "
                    f"{_iqr(w['pairs_per_s'], 0)} | {s_['findings']:,} |")
        lines += ["", f"Long single-request histories, `{name}` (judge only, one request pair, same history on both sides), "
                  "median (Q1-Q3) in ms:", "",
                  "| kind | turns | messages per side | input bytes | parse ms | judge ms | judge ms per message | findings |",
                  "|---|---|---|---|---|---|---|---|"]
        for h in d["history"]:
            lines.append(f"| {h['kind']} | {h['turns']} | {h['messages_per_side']} | {h['input_bytes']:,} | "
                         f"{_iqr({k: v * 1000 for k, v in h['parse_seconds'].items() if k in ('median', 'q1', 'q3')}, 3)} | "
                         f"{_iqr({k: v * 1000 for k, v in h['judge_seconds'].items() if k in ('median', 'q1', 'q3')}, 3)} | "
                         f"{h['judge_ms_per_message']:.4f} | {h['findings']} |")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="write the result JSON here")
    ap.add_argument("--render", nargs="+", metavar="JSON", help="print markdown tables from saved results and exit")
    ap.add_argument("--runs", type=int, default=7, help="repeats for small inputs (default 7, at least 5)")
    ap.add_argument("--runs-large", type=int, default=5, help="repeats for the 40,000-pair scale (default 5)")
    ap.add_argument("--oracle", choices=["v1.0", "v1.3"], default="v1.3", help="oracle for scales, history and e2e")
    ap.add_argument("--scales", default="400,4000,40000")
    ap.add_argument("--history-turns", default="1,10,100,1000")
    args = ap.parse_args()
    if args.render:
        sys.stdout.write(render(args.render))
        return
    if not args.json:
        ap.error("--json is required unless --render is given")
    if args.runs < 5 or args.runs_large < 5:
        ap.error("at least 5 runs")
    scales = [int(x) for x in args.scales.split(",")]
    ks = [int(x) for x in args.history_turns.split(",")]
    sample = load_sample()
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(PKG) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    py = sys.executable
    out = {
        "script": "bench/scale_throughput.py", "convpreserve": __version__, "python": platform.python_version(),
        "python_executable_basename": os.path.basename(py), "platform": platform.platform(), "cpu": _cpu_model(),
        "logical_cpus_available": len(os.sched_getaffinity(0)), "loadavg_start": os.getloadavg(),
        "sample_sha256": _sha(SAMPLE), "carriers_sha256": _sha(CARRIERS), "runs": args.runs,
        "runs_large": args.runs_large, "timestamp_utc": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
        "definition": "pairs_per_s = pairs / stage_seconds for one pass over the whole set; summaries are over runs",
    }
    # startup
    ts, _ = run_wall([py, "-c", "pass"], 10)
    out["startup_python_empty_seconds"] = stats(ts)
    ts, _ = run_wall([py, "-c", "import convpreserve.cli"], 10, env=env)
    out["startup_import_cli_seconds"] = stats(ts)
    ts, _ = run_wall([py, "-m", "convpreserve", "version"], 10, env=env)
    out["startup_convpreserve_version_seconds"] = stats(ts)
    qs = os.path.join(os.path.dirname(PKG), "..", "examples", "quickstart")
    if os.path.isdir(qs):
        cmd = [py, "-m", "convpreserve", "check", "--side", "request", "--src-dialect", "openai_chat", "--dst-dialect",
               "anthropic_messages", "--oracle", args.oracle, os.path.join(qs, "src.json"),
               os.path.join(qs, "dst_thinking_injected.json")]
        ts, rc = run_wall(cmd, 10, env=env)
        out["e2e_check_one_pair_seconds"] = dict(stats(ts), exit_code=rc)
    # same protocol as bench/throughput.py (400 parsed pairs, compare only) for both oracles
    out["compare_only_400"] = {}
    for ov in ("v1.0", "v1.3"):
        j = get_judge(ov)
        for r in sample:        # warm-up
            j.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
        ts = []
        for _ in range(args.runs):
            t0 = time.perf_counter()
            for r in sample:
                j.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
            ts.append(time.perf_counter() - t0)
        out["compare_only_400"][ov] = {"seconds": stats(ts), "pairs_per_s": rate_stats(len(sample), ts)}
    # scales
    judge = get_judge(args.oracle)
    out["oracle_for_scales"] = args.oracle
    out["scales"] = {}
    for n in scales:
        runs = args.runs_large if n >= 40000 else args.runs
        pairs = make_pairs(sample, n)
        entry = {"description": describe(pairs), "runs": runs}
        tmp = tempfile.mkdtemp(prefix="convpreserve-scale-")
        try:
            inline = os.path.join(tmp, "inline.jsonl")
            write_inline_manifest(pairs, inline)
            entry["inline_manifest_bytes"] = os.path.getsize(inline)
            # warm-up pass over a slice (imports, caches)
            stage_inline(inline, judge, 1)
            entry["inline"] = stage_summary(n, stage_inline(inline, judge, runs))
            froot = os.path.join(tmp, "files")
            os.makedirs(froot)
            fman = write_file_manifest(pairs, froot)
            entry["file_per_payload"] = stage_summary(n, stage_files(fman, judge, runs))
            ts, rc = run_wall([py, "-m", "convpreserve", "batch", inline, "--fail-on", "none", "--oracle", args.oracle], runs, env=env)
            entry["e2e_batch_inline"] = {"seconds": stats(ts), "pairs_per_s": rate_stats(n, ts), "exit_code": rc}
            ts, rc = run_wall([py, "-m", "convpreserve", "batch", fman, "--fail-on", "none", "--oracle", args.oracle], runs, env=env)
            entry["e2e_batch_files"] = {"seconds": stats(ts), "pairs_per_s": rate_stats(n, ts), "exit_code": rc}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        out["scales"][str(n)] = entry
    # long history
    out["history"] = history_run(judge, ks, ("text", "tool"), args.runs)
    out["loadavg_end"] = os.getloadavg()
    with open(args.json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"wrote {args.json}")


if __name__ == "__main__":
    main()
