"""Output formats of `check` and `batch`: text, json and GitHub-flavoured Markdown.

`render_check_text` and `render_check_json` produce exactly what `convpreserve check` printed before this module
existed (tab-separated lines; one pretty-printed JSON object).  `render_github` produces Markdown for a pull-request
comment or a workflow step summary: a heading with the tier counts and the oracle version, then one table of
findings (rule id, tier, path or field, meaning of the rule, detail); more than FOLD_AFTER findings are folded into a
<details> block.  Standard library only.
"""
import json

from .explain import rule_meanings

TIERS = ("T1", "T2", "T3")
FOLD_AFTER = 50


def finding_dict(f):
    d = {"rule_id": f.rule_id, "tier": f.tier, "object_id": f.object_id, "detail": f.detail}
    if hasattr(f, "evidence"):  # oracle v1.3 only; v1.0 findings have no evidence field
        d["evidence"] = f.evidence
    return d


def evidence_col(f):
    e = getattr(f, "evidence", None)
    if not e:
        return None
    return f"{e['side']}:{e['path'] or '(root)'}"


def count_tiers(findings):
    counts = {t: 0 for t in TIERS}
    for f in findings:
        counts[f.tier] = counts.get(f.tier, 0) + 1
    return counts


def render_check_text(findings):
    """The tab-separated lines of `check` (empty string when there is no finding); lines end with a newline."""
    out = []
    for f in findings:
        col = evidence_col(f)
        out.append(f"{f.rule_id}\t{f.tier}\t{f.object_id}\t{f.detail}" + (f"\t{col}" if col else "") + "\n")
    return "".join(out)


def render_check_json(findings, oracle, version, carriers_sha256):
    return json.dumps({
        "findings": [finding_dict(f) for f in findings],
        "summary": count_tiers(findings),
        "oracle": oracle,
        "convpreserve": version,
        "carriers_sha256": carriers_sha256,
    }, indent=2)


def finding_rows(findings, where_prefix=""):
    """Rows for the GitHub table: rule, tier, path or field, detail."""
    rows = []
    for f in findings:
        where = evidence_col(f) or f.object_id or "-"
        rows.append({"rule_id": f.rule_id, "tier": f.tier, "where": where_prefix + where, "detail": f.detail})
    return rows


def _cell(text, limit=300):
    t = " ".join(str(text).split())
    if len(t) > limit:
        t = t[:limit - 3] + "..."
    return t.replace("\\", "\\\\").replace("|", "\\|").replace("`", "'").replace("&", "&amp;").replace("<", "&lt;")


def render_github(rows, counts, oracle, version, title="convpreserve check", subject=None, errors=None, fold_after=FOLD_AFTER):
    """GitHub-flavoured Markdown.  `rows`: dicts from finding_rows; `counts`: {T1, T2, T3} finding counts."""
    meanings = rule_meanings()
    total = sum(counts.get(t, 0) for t in TIERS)
    head = f"### {title}: T1 {counts.get('T1', 0)}, T2 {counts.get('T2', 0)}, T3 {counts.get('T3', 0)} (oracle {oracle})"
    lines = [head, "", f"convpreserve {version}." + (f" {subject}" if subject else ""), ""]
    if total == 0:
        lines += ["No findings.", ""]
    else:
        table = ["| Rule | Tier | Path / field | Meaning | Detail |", "|---|---|---|---|---|"]
        for r in rows:
            table.append(f"| `{r['rule_id']}` | {r['tier']} | {_cell(r['where'], 120)} | "
                         f"{_cell(meanings.get(r['rule_id'], ''), 160)} | {_cell(r['detail'])} |")
        if len(rows) > fold_after:
            lines += [f"<details><summary>{len(rows)} findings (click to expand)</summary>", ""] + table + ["", "</details>", ""]
        else:
            lines += table + [""]
        lines += ["`convpreserve explain RULE` prints the meaning of a rule and its common root causes.", ""]
    if errors:
        lines += ["**Errors**", ""] + [f"- {_cell(e)}" for e in errors] + [""]
    return "\n".join(lines)
