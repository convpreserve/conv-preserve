"""Command line interface of convpreserve.

Exit codes (all sub-commands that judge pairs):
    0  no finding at or above --fail-on (default T1)
    1  at least one finding at or above --fail-on
    2  usage, parse or I/O error (also: any line of a batch manifest failed)

`probe` follows the same codes and is the only sub-command that opens sockets (loopback sink plus the
gateway under test).
"""
import argparse
import gzip
import hashlib
import json
import os
import sys

from . import ORACLE_VERSION, __version__
from .judge import carriers
from . import baseline as _baseline
from . import explain as _explain
from . import probe as _probe
from . import report as _report
from .oracles import DEFAULT_ORACLE, ORACLES, default_oracle, get_judge

TIERS = ("T1", "T2", "T3")
_TIER_RANK = {"T1": 1, "T2": 2, "T3": 3, "none": 0}


def _get_carriers_sha():
    c_path = os.path.join(os.path.dirname(__file__), "data", "carriers.json")
    with open(c_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _read_bytes(path):
    if path == "-":
        return sys.stdin.buffer.read()
    with open(path, "rb") as f:
        return f.read()


def _decode_body(content, side, name=""):
    """Decode one wire payload.

    request/response: JSON.  stream: a pre-parsed JSON event list if the file name
    ends in .json, otherwise the raw SSE text.
    """
    if side == "stream":
        if name.endswith(".json"):
            return json.loads(content)
        return content.decode("utf-8")
    return json.loads(content)


def _load_body(path, side):
    return _decode_body(_read_bytes(path), side, path)


# Context keys whose value is a list for the judge; on the command line the list is written comma-separated.
_CONTEXT_LIST_KEYS = ("vendor_tokens", "disabled")


def _parse_context(ctx_list):
    ctx = {}
    for item in ctx_list or []:
        if "=" not in item:
            raise ValueError(f"--context expects key=value, got {item!r}")
        k, v = item.split("=", 1)
        if k in _CONTEXT_LIST_KEYS:
            v = [x.strip() for x in v.split(",") if x.strip()]
        elif k == "non_stream_body":
            raise ValueError("--context non_stream_body needs a parsed response body and is only supported in the "
                             "`context` object of a batch manifest line")
        ctx[k] = v
    return ctx


def _breaches(findings, fail_on):
    """True iff some finding is at or above the --fail-on tier."""
    rank = _TIER_RANK[fail_on]
    if rank == 0:
        return False
    return any(_TIER_RANK.get(f.tier, 9) <= rank for f in findings)


# Formatting helpers live in report.py (text, json and github output); kept under their old names here.
_finding_dict = _report.finding_dict
_evidence_col = _report.evidence_col
_count = _report.count_tiers


def _resolve_format(args):
    """--format (text|json|github); --json is an alias of --format json. Raises ValueError on a conflict."""
    fmt = getattr(args, "format", None)
    if getattr(args, "json", False):
        if fmt not in (None, "json"):
            raise ValueError(f"--json conflicts with --format {fmt}")
        return "json"
    return fmt or "text"


def _load_baseline(args):
    """The Baseline given by --baseline (or the environment variable CONVPRESERVE_BASELINE), or None."""
    path = getattr(args, "baseline", None) or os.environ.get("CONVPRESERVE_BASELINE") or None
    if not path:
        return None
    base = _baseline.load(path)
    if base.oracle and base.oracle != args.oracle:
        print(f"note: baseline {base.name} was written with oracle {base.oracle}, this run uses {args.oracle}; "
              f"fingerprints may not match", file=sys.stderr)
    return base


def _add_baseline_args(p):
    p.add_argument("--baseline", metavar="PATH", default=None,
                   help="JSON file of known findings (see --write-baseline; env CONVPRESERVE_BASELINE). Known findings are "
                        "still printed, marked baselined, and do not count for the exit code or the tier counts")
    p.add_argument("--write-baseline", metavar="PATH", default=None,
                   help="write the fingerprints of all findings of this run to PATH (does not change the exit code; "
                        "add --fail-on none to accept the current state)")


def _oracle_short():
    return ORACLE_VERSION.split()[0]


# ---------------------------------------------------------------- check

def _cmd_check(args):
    try:
        fmt = _resolve_format(args)
        if args.src == "-" and args.dst == "-":
            raise ValueError("only one of src/dst may be '-' (stdin)")
        base = _load_baseline(args)
        src_body = _load_body(args.src, args.side)
        dst_body = _load_body(args.dst, args.side)
        context = _parse_context(args.context)
        findings = get_judge(args.oracle).compare(src_body, args.src_dialect, dst_body, args.dst_dialect,
                                                  args.side, context=context)
        pair = f"{os.path.basename(args.src)}::{os.path.basename(args.dst)}"
        if args.write_baseline:
            n = _baseline.write(args.write_baseline,
                                [_baseline.entry_of(f, args.side, args.src_dialect, args.dst_dialect, pair)
                                 for f in findings], args.oracle, __version__)
            print(f"wrote baseline {os.path.basename(args.write_baseline)}: {n} fingerprint(s)", file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - CLI boundary: report and exit 2
        print(f"Error: {e}", file=sys.stderr)
        return 2
    if base is None:
        counted = findings
        if fmt == "json":
            print(_report.render_check_json(findings, args.oracle, __version__, _get_carriers_sha()))
        elif fmt == "github":
            subject = f"`{args.src}` to `{args.dst}` ({args.side}, {args.src_dialect} to {args.dst_dialect})."
            print(_report.render_github(_report.finding_rows(findings), _count(findings), args.oracle, __version__,
                                        title="convpreserve check", subject=subject))
        else:
            sys.stdout.write(_report.render_check_text(findings))
    else:
        flags = base.flags(findings, args.side, args.src_dialect, args.dst_dialect, pair)
        counted, known = _baseline.split(findings, flags)
        if fmt == "json":
            print(_baseline.render_check_json(findings, flags, base.name, args.oracle, __version__,
                                              _get_carriers_sha()))
        elif fmt == "github":
            subject = (f"`{args.src}` to `{args.dst}` ({args.side}, {args.src_dialect} to {args.dst_dialect}). "
                       + _baseline.baselined_note(len(known), base.name))
            print(_baseline.relabel(_report.render_github(_report.finding_rows(counted), _count(counted), args.oracle,
                                                          __version__, title="convpreserve check", subject=subject))
                  + _baseline.baselined_markdown(_report.finding_rows(known)))
        else:
            sys.stdout.write(_baseline.render_check_text(findings, flags))
    return 1 if _breaches(counted, args.fail_on) else 0


# ---------------------------------------------------------------- batch

def _resolve_side(rec, key, side, base_dir):
    """Return the payload of `key` ('src' or 'dst'): inline, or from <key>_file."""
    if key in rec:
        return rec[key]
    fkey = key + "_file"
    if fkey in rec:
        path = rec[fkey]
        if not os.path.isabs(path):
            path = os.path.join(base_dir, path)
        return _decode_body(_read_bytes(path), side, path)
    raise KeyError(f"line has neither '{key}' nor '{fkey}'")


def render_summary_md(stats):
    """Markdown summary for a batch run. `stats` is the dict built by _cmd_batch."""
    n = stats["pairs"]
    lines = [
        "# convpreserve batch summary",
        "",
        f"- convpreserve {__version__}, oracle {stats['oracle']}, "
        f"carriers.json sha256 `{stats['carriers_sha256']}`",
        f"- manifest: `{stats['manifest']}`",
        f"- pairs judged: {n - stats['errors']} of {n} (errors: {stats['errors']})",
        f"- fail-on: {stats['fail_on']}; pairs at or above threshold: {stats['breaching_pairs']}",
    ]
    if stats.get("baseline"):
        lines.append(f"- baseline `{stats['baseline']}`: {stats['baselined']} finding(s) baselined, "
                     "not counted in the tables below")
    lines += [
        "",
        "## Findings by tier",
        "",
        "| Tier | Findings | Pairs with at least one |",
        "|---|---:|---:|",
    ]
    for t in TIERS:
        lines.append(f"| {t} | {stats['tier_findings'][t]} | {stats['tier_pairs'][t]} |")
    lines += ["", "## Findings by rule", "", "| Rule | Tier | Findings |", "|---|---|---:|"]
    for (rule, tier), c in sorted(stats["rules"].items()):
        lines.append(f"| {rule} | {tier} | {c} |")
    lines += ["", "## By pair", "",
              "| Side | Source dialect | Target dialect | Pairs | T1 | T2 | T3 | Pairs with T1 |",
              "|---|---|---|---:|---:|---:|---:|---:|"]
    for (side, sd, dd), p in sorted(stats["by_pair"].items()):
        lines.append(f"| {side} | {sd} | {dd} | {p['pairs']} | {p['T1']} | {p['T2']} | "
                     f"{p['T3']} | {p['pairs_T1']} |")
    if stats["error_lines"]:
        lines += ["", "## Errors", ""]
        lines += [f"- line {ln}: {msg}" for ln, msg in stats["error_lines"]]
    lines.append("")
    return "\n".join(lines)


def _batch_json(stats):
    """--format json of `batch`: the run statistics as one pretty-printed JSON object."""
    return json.dumps({
        "manifest": stats["manifest"], "pairs": stats["pairs"], "errors": stats["errors"],
        "breaching_pairs": stats["breaching_pairs"], "fail_on": stats["fail_on"],
        "summary": stats["tier_findings"], "pairs_with_tier": stats["tier_pairs"],
        "rules": [{"rule_id": r, "tier": tier, "findings": c} for (r, tier), c in sorted(stats["rules"].items())],
        "by_pair": [{"side": s, "src_dialect": sd, "dst_dialect": dd, **p} for (s, sd, dd), p in sorted(stats["by_pair"].items())],
        "error_lines": [{"line": ln, "message": msg} for ln, msg in stats["error_lines"]],
        "oracle": stats["oracle"], "convpreserve": __version__, "carriers_sha256": stats["carriers_sha256"],
        **({"baselined": stats["baselined"], "baseline": stats["baseline"]} if stats.get("baseline") else {}),
    }, indent=2)


def _cmd_batch(args):
    try:
        fmt = _resolve_format(args)
        base = _load_baseline(args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    gh_rows = []   # findings of all pairs, for --format github
    gh_known = []  # baselined findings of all pairs, for --format github
    base_entries = []  # fingerprints of all findings, for --write-baseline
    stats = {
        "manifest": os.path.basename(args.manifest), "pairs": 0, "errors": 0,
        "breaching_pairs": 0, "fail_on": args.fail_on,
        "tier_findings": {t: 0 for t in TIERS}, "tier_pairs": {t: 0 for t in TIERS},
        "rules": {}, "by_pair": {}, "error_lines": [],
        "carriers_sha256": _get_carriers_sha(), "oracle": args.oracle,
    }
    if base is not None:
        stats["baseline"], stats["baselined"] = base.name, 0
    base_dir = os.path.dirname(os.path.abspath(args.manifest))
    out_f = None
    try:
        out_f = open(args.out, "w", encoding="utf-8") if args.out else None
        with open(args.manifest, "r", encoding="utf-8") as mf:
            for ln, line in enumerate(mf, 1):
                if not line.strip():
                    continue
                stats["pairs"] += 1
                try:
                    rec = json.loads(line)
                    side = rec["side"]
                    src = _resolve_side(rec, "src", side, base_dir)
                    dst = _resolve_side(rec, "dst", side, base_dir)
                    findings = get_judge(args.oracle).compare(src, rec["src_dialect"], dst, rec["dst_dialect"],
                                                              side, context=rec.get("context"))
                except Exception as e:  # noqa: BLE001 - per-line error, keep going
                    stats["errors"] += 1
                    stats["error_lines"].append((ln, f"{type(e).__name__}: {e}"))
                    if out_f:
                        out_f.write(json.dumps({"line": ln, "error": f"{type(e).__name__}: {e}"}) + "\n")
                    continue
                pair_id = rec.get("id", f"line {ln}")
                if args.write_baseline:
                    base_entries += [_baseline.entry_of(fd, side, rec["src_dialect"], rec["dst_dialect"], str(pair_id))
                                     for fd in findings]
                flags = None
                if base is not None:
                    flags = base.flags(findings, side, rec["src_dialect"], rec["dst_dialect"], str(pair_id))
                    all_findings = findings
                    findings, known = _baseline.split(all_findings, flags)
                    stats["baselined"] += len(known)
                counts = _count(findings)
                key = (side, rec["src_dialect"], rec["dst_dialect"])
                p = stats["by_pair"].setdefault(
                    key, {"pairs": 0, "T1": 0, "T2": 0, "T3": 0, "pairs_T1": 0})
                p["pairs"] += 1
                for t in TIERS:
                    stats["tier_findings"][t] += counts[t]
                    p[t] += counts[t]
                    if counts[t]:
                        stats["tier_pairs"][t] += 1
                if counts["T1"]:
                    p["pairs_T1"] += 1
                for fd in findings:
                    rk = (fd.rule_id, fd.tier)
                    stats["rules"][rk] = stats["rules"].get(rk, 0) + 1
                if fmt == "github":
                    gh_rows += _report.finding_rows(findings, where_prefix=f"{pair_id}: ")
                    if base is not None:
                        gh_known += _report.finding_rows(known, where_prefix=f"{pair_id}: ")
                if _breaches(findings, args.fail_on):
                    stats["breaching_pairs"] += 1
                if out_f:
                    out = dict(rec)
                    if flags is None:
                        out["findings"] = [_finding_dict(fd) for fd in findings]
                    else:
                        out["findings"] = [dict(_finding_dict(fd), baselined=b) for fd, b in zip(all_findings, flags)]
                    out["summary"] = counts
                    out_f.write(json.dumps(out, ensure_ascii=False) + "\n")
        if args.write_baseline:
            n = _baseline.write(args.write_baseline, base_entries, args.oracle, __version__)
            print(f"wrote baseline {os.path.basename(args.write_baseline)}: {n} fingerprint(s)", file=sys.stderr)
        if args.summary_md:
            with open(args.summary_md, "w", encoding="utf-8") as sf:
                sf.write(render_summary_md(stats))
    except Exception as e:  # noqa: BLE001 - CLI boundary
        print(f"Error: {e}", file=sys.stderr)
        return 2
    finally:
        if out_f:
            out_f.close()
    tf = stats["tier_findings"]
    if fmt == "json":
        print(_batch_json(stats))
    elif fmt == "github":
        subject = (f"Manifest `{stats['manifest']}`: {stats['pairs']} pairs, {stats['pairs'] - stats['errors']} judged, "
                   f"{stats['errors']} errors; pairs with a T1 finding: {stats['tier_pairs']['T1']}.")
        errors = [f"line {ln}: {msg}" for ln, msg in stats["error_lines"]]
        if base is not None:
            subject += " " + _baseline.baselined_note(stats["baselined"], base.name)
        md = _report.render_github(gh_rows, tf, args.oracle, __version__, title="convpreserve batch", subject=subject,
                                   errors=errors)
        if base is not None:
            md = _baseline.relabel(md) + _baseline.baselined_markdown(gh_known)
        print(md)
    else:
        print(f"pairs={stats['pairs']} errors={stats['errors']} "
              f"T1={tf['T1']} T2={tf['T2']} T3={tf['T3']} "
              f"pairs_with_T1={stats['tier_pairs']['T1']}" + (f" baselined={stats['baselined']}" if base is not None else ""))
        for (rule, tier), c in sorted(stats["rules"].items()):
            print(f"{rule}\t{tier}\t{c}")
    for ln, msg in stats["error_lines"]:
        print(f"line {ln}: {msg}", file=sys.stderr)
    if stats["errors"]:
        return 2
    return 1 if stats["breaching_pairs"] else 0


# ---------------------------------------------------------------- others

def _data_path(name):
    return os.path.join(os.path.dirname(__file__), "data", name)


def _load_gz(name):
    with gzip.open(_data_path(name), "rt", encoding="utf-8") as f:
        return json.load(f)


def _same_findings(expected, findings, with_evidence):
    """v1.0: compare the (rule, tier, detail) sets.  v1.3: compare the full finding dicts, evidence included."""
    if not with_evidence:
        return ({(x["rule_id"], x["tier"], x["detail"]) for x in expected}
                == {(f.rule_id, f.tier, f.detail) for f in findings})
    return (sorted(json.dumps(x, sort_keys=True) for x in expected)
            == sorted(json.dumps(_finding_dict(f), sort_keys=True) for f in findings))


def _cmd_selftest(args):
    """Replay the bundled 400-record sample and the per-rule cases with the chosen oracle.

    v1.0 is compared with the recorded v1.0 findings (rule, tier, detail).  v1.3 is compared with the recorded v1.3
    findings (rule, tier, detail, evidence side, path and precision).  The sample's recorded findings are the
    frozen v1.0 output; v1.3 differs from it only where a documented change applies.
    """
    if not os.path.exists(_data_path("selftest_sample.json.gz")):
        print("selftest_sample.json.gz not found")
        return 2
    version = args.oracle
    compare = get_judge(version).compare
    with_ev = version != "v1.0"
    records = _load_gz("selftest_sample.json.gz")
    expected_all = None
    if with_ev:
        if not os.path.exists(_data_path("selftest_expected_v13.json.gz")):
            print("selftest_expected_v13.json.gz not found")
            return 2
        expected_all = _load_gz("selftest_expected_v13.json.gz")
    identical = 0
    for i, rec in enumerate(records):
        findings = compare(rec["src_body"], rec["src_dialect"], rec["dst_body"], rec["dst_dialect"],
                           rec["side"], context=rec.get("context"))
        exp = rec["findings"] if expected_all is None else expected_all[i]
        if _same_findings(exp, findings, with_ev):
            identical += 1
        else:
            print(f"Mismatch at index {i}")
    print(f"{identical} identical / {len(records)} total")
    ok = identical == len(records)
    if os.path.exists(_data_path("selftest_rule_cases.json.gz")):
        cases = _load_gz("selftest_rule_cases.json.gz")
        c_ok = 0
        for k, case in enumerate(cases):
            got = sorted({f.rule_id for f in compare(case["src"], case["src_dialect"], case["dst"], case["dst_dialect"],
                                                      case["side"], context=case.get("context"))})
            want = case["expect"].get(version, case["expect"].get("all"))
            if got == want:
                c_ok += 1
            else:
                print(f"Rule case mismatch: {case['id']} expected {want} got {got}")
        print(f"rule cases: {c_ok} identical / {len(cases)} total")
        ok = ok and c_ok == len(cases)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


def _cmd_dialects(_args):
    for k in sorted(carriers.wire_tables()["dialect_aliases"].keys()):
        print(k)
    return 0


def _cmd_version(_args):
    d = default_oracle()
    sha = _get_carriers_sha()
    print(f"convpreserve {__version__} / oracle v1.0 (frozen paper baseline)"
          + (" [default]" if d == "v1.0" else "") + f" / carriers.json sha256 {sha}")
    print(f"convpreserve {__version__} / oracle v1.3 (stream reasoning + JSON Pointer evidence)"
          + (" [default]" if d == "v1.3" else "") + f" / carriers.json sha256 {sha}")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        prog="convpreserve",
        description="Check translated LLM API payloads for silent semantic changes "
                    "(tiers T1/T2/T3). Exit codes: 0 clean, 1 findings at/above --fail-on, "
                    "2 usage/parse error.")
    parser.add_argument("--version", action="version", version=f"convpreserve {__version__} (oracles {', '.join(ORACLES)}; default {default_oracle()})")
    sub = parser.add_subparsers(dest="command", metavar="{check,batch,explain,probe,selftest,dialects,version}")

    check_p = sub.add_parser("check", help="compare one source/destination pair")
    check_p.add_argument("--side", choices=["request", "response", "stream"], required=True)
    check_p.add_argument("--src-dialect", required=True, help="see `convpreserve dialects`")
    check_p.add_argument("--dst-dialect", required=True, help="see `convpreserve dialects`")
    check_p.add_argument("--json", action="store_true", help="machine-readable output (alias of --format json)")
    check_p.add_argument("--format", choices=["text", "json", "github"], default=None,
                         help="output format (default text); github is Markdown for a pull-request comment or "
                              "a workflow step summary")
    check_p.add_argument("--fail-on", choices=["T1", "T2", "T3", "none"], default="T1")
    check_p.add_argument("--context", nargs="*", metavar="K=V",
                         help="extra judge context, K=V pairs. Keys: object_id=ID (label shown on each finding); "
                              "vendor_tokens=a,b (integrity tokens to count as present in the source conversation, "
                              "so rule S10 does not flag them on the destination); disabled=P1,P4 (ablation switch "
                              "for the judge's principles, for experiments). non_stream_body is manifest-only. "
                              "Place --context after src and dst, or end the pairs with --")
    _add_baseline_args(check_p)
    check_p.add_argument("src", help="source payload file ('-' = stdin; for --side stream: "
                                     "raw SSE text, or a .json event list)")
    check_p.add_argument("dst", help="destination payload file ('-' = stdin)")
    check_p.add_argument("--oracle", choices=sorted(ORACLES), default=default_oracle(),
                   help="judge version (default %(default)s; env CONVPRESERVE_ORACLE). v1.0 is the frozen "
                        "paper oracle, v1.3 adds the changes listed in CHANGELOG.md")
    check_p.set_defaults(fn=_cmd_check)

    batch_p = sub.add_parser(
        "batch", help="judge a JSONL manifest of pairs",
        description="Each manifest line: {\"id\":..., \"side\":..., \"src_dialect\":..., "
                    "\"dst_dialect\":..., \"src\":<inline> | \"src_file\":<path>, "
                    "\"dst\":<inline> | \"dst_file\":<path>, \"context\":{...}?}. Relative "
                    "*_file paths resolve against the manifest's directory.")
    batch_p.add_argument("manifest", help="JSONL manifest of pairs")
    batch_p.add_argument("--out", help="write one JSONL line per pair (input + findings)")
    batch_p.add_argument("--summary-md", help="write a Markdown summary (T1/T2/T3 counts)")
    batch_p.add_argument("--fail-on", choices=["T1", "T2", "T3", "none"], default="T1")
    _add_baseline_args(batch_p)
    batch_p.add_argument("--format", choices=["text", "json", "github"], default=None,
                         help="what to print on stdout (default text: totals and one line per rule); json prints the "
                              "run statistics as one object, github Markdown for a pull-request comment or step summary")
    batch_p.add_argument("--oracle", choices=sorted(ORACLES), default=default_oracle(),
                   help="judge version (default %(default)s; env CONVPRESERVE_ORACLE). v1.0 is the frozen "
                        "paper oracle, v1.3 adds the changes listed in CHANGELOG.md")
    batch_p.set_defaults(fn=_cmd_batch)

    explain_p = sub.add_parser(
        "explain", help="explain a rule: its meaning from the rule catalogue and curated common root causes",
        description="Prints what a rule (for example S08) means, using the bundled rule catalogue, followed by the "
                    "common root causes the study located in translator code (rules without a located mechanism "
                    "print 'no curated notes yet'). `--list` prints all rule ids. Exit codes: 0 found, "
                    "2 unknown rule (similar ids are listed).")
    _explain.add_arguments(explain_p)
    explain_p.set_defaults(fn=_explain.run)

    probe_p = sub.add_parser(
        "probe", help="send the 51 bundled request shapes through a running gateway and judge what it forwards",
        description="Starts a loopback sink that stands in for the upstream provider, POSTs the bundled Anthropic "
                    "Messages request shapes to --gateway, captures the body the gateway forwards to the sink and "
                    "judges it (request side). The judge opens no sockets; probe is the one sub-command that does. "
                    "Exit codes: 0 clean, 1 findings at/above --fail-on, 2 usage/bind/connection error.")
    _probe.add_arguments(probe_p)
    probe_p.set_defaults(fn=_probe.run)

    sub.add_parser("dialects", help="list dialect names").set_defaults(fn=_cmd_dialects)
    sub.add_parser("version", help="print versions and carriers hash").set_defaults(fn=_cmd_version)
    selftest_p = sub.add_parser("selftest", help="replay the bundled sample; must print PASS")
    selftest_p.add_argument("--oracle", choices=sorted(ORACLES), default=default_oracle(),
                   help="judge version (default %(default)s; env CONVPRESERVE_ORACLE). v1.0 is the frozen "
                        "paper oracle, v1.3 adds the changes listed in CHANGELOG.md")
    selftest_p.set_defaults(fn=_cmd_selftest)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        sys.exit(2)
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
