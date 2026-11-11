"""`convpreserve explain RULE`: rule definition and known open-source translator bug patterns."""
import difflib
import json
import os
import sys

_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _load(name):
    with open(os.path.join(_DATA, name), encoding="utf-8") as f:
        return json.load(f)


def load_catalog():
    """Ordered list of rule dicts from data/catalog.json."""
    return _load("catalog.json")["rules"]


def load_notes():
    """Parsed data/explain_notes.json."""
    return _load("explain_notes.json")


def rule_meanings():
    """{rule_id: meaning} for every rule in the catalogue."""
    return {r["id"]: r.get("meaning", "") for r in load_catalog()}


def _lookup(query, rules):
    q = query.strip().upper()
    for r in rules:
        if r["id"].upper() == q:
            return r
    by_name = [r for r in rules if r.get("name", "").upper() == q]
    return by_name[0] if len(by_name) == 1 else None


def similar(query, rules, limit=5):
    """Rule ids close to `query` (by id or name), closest match first."""
    q = query.strip().upper()
    scored = {}
    for r in rules:
        for key in (r["id"].upper(), r.get("name", "").upper()):
            ratio = difflib.SequenceMatcher(None, q, key).ratio()
            if q and (key.startswith(q) or q.startswith(key) or ratio >= 0.5):
                scored[r["id"]] = max(scored.get(r["id"], 0), ratio + (0.5 if key.startswith(q) else 0))
    if not scored and len(q) >= 1:
        for r in rules:
            if r["id"].upper()[:1] == q[:1]:
                scored[r["id"]] = 0.1
    return [rid for rid, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def explain_dict(rule, notes):
    """Machine-readable explanation of one rule."""
    d = {k: rule[k] for k in rule}
    d["root_causes"] = list(notes.get("rules", {}).get(rule["id"], []))
    d["curated_notes"] = bool(d["root_causes"])
    return d


def render_text(rule, notes):
    d = explain_dict(rule, notes)
    lines = [
        f"{d['id']}  {d.get('name', '')}  ({d.get('tier', '?')})",
        "",
        f"Summary: {d.get('meaning', '')}",
        "",
    ]
    if d["curated_notes"]:
        lines.append("Known translator bug patterns:")
        for n in d["root_causes"]:
            projs = ", ".join(n["projects"])
            lines += [
                "",
                f"- [{n['pattern']}] ({projs})",
                f"  {n['cause']}",
            ]
    else:
        lines.append("Known translator bug patterns: none recorded.")
    lines.append("")
    return "\n".join(lines)


def add_arguments(p):
    p.add_argument("rule", nargs="?", help="rule ID (e.g. S08) or rule name (e.g. CONTROL)")
    p.add_argument("--list", action="store_true", help="list all 27 rule IDs")
    p.add_argument("--json", action="store_true", help="output JSON")


def run(args):
    try:
        rules = load_catalog()
        notes = load_notes()
    except (OSError, ValueError, KeyError) as e:
        print(f"Error: cannot read bundled rule data: {e}", file=sys.stderr)
        return 2
    if args.list:
        if args.json:
            print(json.dumps([{"id": r["id"], "name": r.get("name", ""), "tier": r.get("tier", "")} for r in rules], indent=2))
        else:
            for r in rules:
                print(r["id"])
        return 0
    if not args.rule:
        print("Error: specify a rule ID (e.g. `convpreserve explain S08`) or pass --list", file=sys.stderr)
        return 2
    rule = _lookup(args.rule, rules)
    if rule is None:
        sim = similar(args.rule, rules)
        msg = f"Error: unknown rule {args.rule!r}."
        msg += (" Similar rules: " + ", ".join(sim) + ".") if sim else ""
        print(msg + " Run `convpreserve explain --list` to see all rule IDs.", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(explain_dict(rule, notes), indent=2, ensure_ascii=False))
    else:
        print(render_text(rule, notes), end="")
    return 0
