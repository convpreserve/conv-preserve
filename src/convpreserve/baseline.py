"""Baseline of known findings for `check` and `batch` (`--baseline PATH`, `--write-baseline PATH`).

A baseline lets a team that cannot fix every finding at once see only what is new.  It is a JSON file listing
fingerprints of findings that are accepted for now.  A finding whose fingerprint is in the baseline is still printed
but marked as baselined, and it is not counted for the exit code, the tier counts of the run or the GitHub step outputs.

Fingerprint of a finding (the `detail` text, which may hold values, is not part of it):

    rule id, payload side (request / response / stream), source dialect, destination dialect,
    where (the evidence column `dst:/0/message/stop_reason` of oracle v1.3, or the object id of a v1.0 finding),
    pair (batch: the manifest line id, or "line N" when it has none; check: the source and destination file base names)

Two findings of one pair with the same fingerprint are both covered by one baseline entry.  A baseline written with
one oracle version does not match findings of the other (v1.0 findings have no evidence path); a note on stderr says so.
Standard library only.
"""
import json
import os

from . import report as _report

FORMAT = 1
KEY_FIELDS = ("rule_id", "side", "src_dialect", "dst_dialect", "where", "pair")


def where_of(f):
    """The location column of a finding; the same value the GitHub table shows."""
    return _report.evidence_col(f) or f.object_id or "-"


def entry_of(f, side, src_dialect, dst_dialect, pair):
    return {"rule_id": f.rule_id, "tier": f.tier, "side": side, "src_dialect": src_dialect,
            "dst_dialect": dst_dialect, "where": where_of(f), "pair": pair}


def key_of(entry):
    return tuple(str(entry[k]) for k in KEY_FIELDS)


class Baseline:
    def __init__(self, path, keys, oracle=None):
        self.path = path
        self.name = os.path.basename(path)
        self.keys = keys
        self.oracle = oracle

    def known(self, f, side, src_dialect, dst_dialect, pair):
        return key_of(entry_of(f, side, src_dialect, dst_dialect, pair)) in self.keys

    def flags(self, findings, side, src_dialect, dst_dialect, pair):
        return [self.known(f, side, src_dialect, dst_dialect, pair) for f in findings]


def load(path):
    """Read a baseline file.  Raises ValueError (message names the file) for anything that is not one."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except OSError as e:
        raise ValueError(f"cannot read baseline {path}: {e.strerror or e}") from e
    except ValueError as e:
        raise ValueError(f"baseline {path} is not valid JSON: {e}") from e
    if not isinstance(doc, dict) or doc.get("convpreserve_baseline") != FORMAT or not isinstance(doc.get("entries"), list):
        raise ValueError(f"baseline {path} is not an convpreserve baseline (format {FORMAT}); "
                         "create one with --write-baseline")
    keys = set()
    for i, e in enumerate(doc["entries"]):
        if not isinstance(e, dict) or any(k not in e for k in KEY_FIELDS):
            raise ValueError(f"baseline {path}: entry {i} lacks one of {', '.join(KEY_FIELDS)}")
        keys.add(key_of(e))
    return Baseline(path, keys, doc.get("oracle"))


def write(path, entries, oracle, version):
    """Write the distinct fingerprints of `entries` (dicts from entry_of), sorted, to `path`."""
    seen = {}
    for e in entries:
        seen.setdefault(key_of(e), e)
    ordered = [seen[k] for k in sorted(seen)]
    doc = {"convpreserve_baseline": FORMAT, "convpreserve": version, "oracle": oracle, "entries": ordered}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return len(ordered)


def split(findings, flags):
    """(new findings, baselined findings) for parallel lists of findings and baselined flags."""
    new = [f for f, b in zip(findings, flags) if not b]
    old = [f for f, b in zip(findings, flags) if b]
    return new, old


def render_check_text(findings, flags):
    """Like report.render_check_text, with a last column `[baselined]` on known findings."""
    out = []
    for f, b in zip(findings, flags):
        col = _report.evidence_col(f)
        out.append(f"{f.rule_id}\t{f.tier}\t{f.object_id}\t{f.detail}" + (f"\t{col}" if col else "")
                   + ("\t[baselined]" if b else "") + "\n")
    return "".join(out)


def render_check_json(findings, flags, baseline_name, oracle, version, carriers_sha256):
    """Like report.render_check_json.  `summary` counts only findings that are not baselined."""
    new, old = split(findings, flags)
    rows = []
    for f, b in zip(findings, flags):
        d = _report.finding_dict(f)
        d["baselined"] = b
        rows.append(d)
    return json.dumps({
        "findings": rows,
        "summary": _report.count_tiers(new),
        "baselined": len(old),
        "baseline": baseline_name,
        "oracle": oracle,
        "convpreserve": version,
        "carriers_sha256": carriers_sha256,
    }, indent=2)


def baselined_note(n, baseline_name):
    return f"{n} finding(s) baselined by `{baseline_name}` (listed below, not counted)."


def relabel(markdown):
    """In a GitHub summary rendered from the non-baselined findings, 'No findings.' means 'No new findings.'."""
    return markdown.replace("\nNo findings.\n", "\nNo new findings.\n", 1)


def baselined_markdown(old_rows):
    """Markdown block listing baselined rows (dicts from report.finding_rows); empty string when there are none."""
    if not old_rows:
        return ""
    meanings = _report.rule_meanings()
    lines = ["", f"<details><summary>{len(old_rows)} baselined finding(s), not counted</summary>", "",
             "| Rule | Tier | Path / field | Meaning | Detail |", "|---|---|---|---|---|"]
    for r in old_rows:
        lines.append(f"| `{r['rule_id']}` | {r['tier']} | {_report._cell(r['where'], 120)} | "
                     f"{_report._cell(meanings.get(r['rule_id'], ''), 160)} | {_report._cell(r['detail'])} |")
    lines += ["", "</details>", ""]
    return "\n".join(lines)
