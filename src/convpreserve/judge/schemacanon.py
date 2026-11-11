"""V11 tool-schema change with the L09 equivalence class.
extract_tools(target, wire) -> {tool name: parameter schema | None}; canon(schema) -> canonical JSON Schema under L09;
v11(src_tools, dst_tools) -> findings [{rule:'V11', sub:'a'|'b'|'c'|'d'|'e', tool, path, detail}].
L09 (representation, not meaning): parameters ↔ parametersJsonSchema ↔ input_schema (and snake_case containers);
Gemini upper-case type names; `nullable: true` ↔ `type: [T,"null"]` ↔ `anyOf:[{type:T},{type:"null"}]`; local `$ref` inlined;
removal of `$schema`, `title`, `additionalProperties: true`, empty `properties: {}`, empty `required: []`; key order; enum order;
description whitespace; const x ↔ enum [x]; single-member anyOf/oneOf/allOf ↔ the member; null in enum ↔ null in type.
NOT L09 (dropping them fires V11.a): default, examples, propertyOrdering, format, pattern, min*/max*, additionalProperties:false.
Everything else that differs after canonicalisation is a finding: V11.a keyword dropped, V11.b constraint changed
(type/enum/const/required/format/pattern/min*/max*/multipleOf/uniqueItems/additionalProperties/items), V11.c property dropped,
V11.d description changed, V11.e other (keyword added / value changed elsewhere).
Gated: ORACLE_V10=1 (callers decide; this module is pure)."""
import copy, json, re
_TYPES = {"string", "number", "integer", "boolean", "array", "object", "null"}
_CONSTRAINT = {"type", "enum", "const", "required", "format", "pattern", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
               "multipleOf", "minLength", "maxLength", "minItems", "maxItems", "uniqueItems", "additionalProperties", "items", "anyOf", "oneOf", "allOf", "nullable"}
def _camel(k): return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), k)

def extract_tools(target, wire):
    out = {}
    if not isinstance(wire, dict): return out
    tools = wire.get("tools") or []
    if target == "openai":
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("function"), dict): out[t["function"].get("name")] = t["function"].get("parameters")
    elif target == "gemini":
        for t in tools:
            if not isinstance(t, dict): continue
            for d in (t.get("functionDeclarations") or t.get("function_declarations") or []):
                if isinstance(d, dict): out[d.get("name")] = d.get("parametersJsonSchema") if "parametersJsonSchema" in d else d.get("parameters_json_schema", d.get("parameters"))
    elif target == "anthropic":
        for t in tools:
            if isinstance(t, dict) and "input_schema" in t: out[t.get("name")] = t.get("input_schema")
    return out

def _inline_refs(s, defs):
    if isinstance(s, dict):
        if "$ref" in s and isinstance(s["$ref"], str) and s["$ref"].startswith("#/"):
            parts = s["$ref"][2:].split("/"); node = defs.get("__root__")
            for p in parts:
                node = node.get(p) if isinstance(node, dict) else None
            if node is not None:
                merged = dict(node); merged.update({k: v for k, v in s.items() if k != "$ref"}); return _inline_refs(merged, defs)
        return {k: _inline_refs(v, defs) for k, v in s.items()}
    if isinstance(s, list): return [_inline_refs(v, defs) for v in s]
    return s

def canon(schema):
    if not isinstance(schema, dict): return schema
    root = copy.deepcopy(schema); root = _inline_refs(root, {"__root__": root})
    def walk(s):
        if isinstance(s, list): return [walk(v) for v in s]
        if not isinstance(s, dict): return s
        o = {}
        for k, v in s.items():
            nk = _camel(k) if k in ("property_ordering", "additional_properties", "min_items", "max_items", "min_length", "max_length", "exclusive_minimum", "exclusive_maximum", "multiple_of", "unique_items", "any_of", "one_of", "all_of") else k
            o[nk] = v
        s = o
        for k in ("$schema", "title", "$defs", "definitions"): s.pop(k, None)   # propertyOrdering is NOT stripped: not in the L09 equivalence list (dropping it is V11.a)
        # single-member anyOf/oneOf/allOf == that member (JSON Schema semantics)
        for comb in ("anyOf", "oneOf", "allOf"):
            if isinstance(s.get(comb), list) and len(s[comb]) == 1 and isinstance(s[comb][0], dict):
                member = s.pop(comb)[0]; inner = dict(member); inner.update({k: v for k, v in s.items()}); s = inner
        # const x == enum [x]
        if "const" in s and "enum" not in s: s["enum"] = [s.pop("const")]
        # type normalisation
        t = s.get("type")
        if isinstance(t, str) and t.lower() in _TYPES: t = t.lower()
        if isinstance(t, list): t = sorted(x.lower() if isinstance(x, str) else x for x in t)
        if s.pop("nullable", None) is True:
            t = sorted(set(([t] if isinstance(t, str) else (t or [])) + ["null"]))
        # anyOf [{type:T},{type:null}] -> type [T, null]
        if isinstance(s.get("anyOf"), list) and len(s["anyOf"]) == 2 and all(isinstance(x, dict) and set(x) == {"type"} for x in s["anyOf"]) and any(x["type"] == "null" for x in s["anyOf"]):
            other = [x["type"] for x in s["anyOf"] if x["type"] != "null"]; t = sorted([other[0].lower() if isinstance(other[0], str) else other[0], "null"]); s.pop("anyOf")
        if t is not None: s["type"] = t
        else: s.pop("type", None)
        if isinstance(t, list) and len(t) == 1: s["type"] = t[0]
        if s.get("additionalProperties") is True: s.pop("additionalProperties")
        if s.get("properties") == {}: s.pop("properties")
        if s.get("required") == []: s.pop("required")
        if isinstance(s.get("required"), list): s["required"] = sorted(s["required"])
        if isinstance(s.get("enum"), list):
            if isinstance(s.get("type"), list) and "null" in s["type"]: s["enum"] = [x for x in s["enum"] if x is not None]   # null admitted by the type already
            s["enum"] = sorted(s["enum"], key=lambda x: json.dumps(x))
        if isinstance(s.get("description"), str): s["description"] = " ".join(s["description"].split())
        for k, v in list(s.items()):
            if k == "properties" and isinstance(v, dict): s[k] = {pk: walk(pv) for pk, pv in v.items()}
            elif k in ("items", "additionalProperties", "not") and isinstance(v, dict): s[k] = walk(v)
            elif k in ("anyOf", "oneOf", "allOf", "prefixItems") and isinstance(v, list): s[k] = [walk(x) for x in v]
            elif k in ("$defs", "definitions"): pass
        return s
    return walk(root)

def _diff(a, b, path, out, tool):
    """a = source canonical, b = target canonical (both dicts)"""
    if not isinstance(a, dict) or not isinstance(b, dict):
        if a != b: out.append({"rule": "V11", "sub": "e", "tool": tool, "path": path, "detail": f"{json.dumps(a)[:40]} -> {json.dumps(b)[:40]}"})
        return
    for k, v in a.items():
        if k == "properties" and isinstance(v, dict):
            bp = b.get("properties") if isinstance(b.get("properties"), dict) else {}
            for pk, pv in v.items():
                if pk not in bp: out.append({"rule": "V11", "sub": "c", "tool": tool, "path": f"{path}/properties/{pk}", "detail": "property dropped"})
                else: _diff(pv, bp[pk], f"{path}/properties/{pk}", out, tool)
            continue
        if k not in b:
            out.append({"rule": "V11", "sub": "a", "tool": tool, "path": f"{path}/{k}", "detail": f"keyword dropped ({json.dumps(v)[:40]})"}); continue
        if k == "description":
            if v != b[k]: out.append({"rule": "V11", "sub": "d", "tool": tool, "path": f"{path}/{k}", "detail": "description changed"})
            continue
        if k in ("items", "additionalProperties", "not") and isinstance(v, dict) and isinstance(b[k], dict): _diff(v, b[k], f"{path}/{k}", out, tool); continue
        if k in ("anyOf", "oneOf", "allOf") and isinstance(v, list) and isinstance(b[k], list) and len(v) == len(b[k]):
            for i, (x, y) in enumerate(zip(v, b[k])): _diff(x, y, f"{path}/{k}/{i}", out, tool)
            continue
        if v != b[k]:
            out.append({"rule": "V11", "sub": "b" if k in _CONSTRAINT else "e", "tool": tool, "path": f"{path}/{k}", "detail": f"{json.dumps(v)[:40]} -> {json.dumps(b[k])[:40]}"})
    for k in b:
        if k not in a and k != "properties": out.append({"rule": "V11", "sub": "e", "tool": tool, "path": f"{path}/{k}", "detail": f"keyword added ({json.dumps(b[k])[:40]})"})
        if k == "properties" and isinstance(b[k], dict) and isinstance(a.get("properties"), dict):
            for pk in b[k]:
                if pk not in a["properties"]: out.append({"rule": "V11", "sub": "e", "tool": tool, "path": f"{path}/properties/{pk}", "detail": "property added"})

def v11(src_tools, dst_tools):
    F = []
    for name, s in src_tools.items():
        if name not in dst_tools: continue   # V09 (tool gone) is the existing rule's job
        d = dst_tools[name]
        if isinstance(s, dict) and not isinstance(d, dict):
            for pk in (s.get("properties") or {}): F.append({"rule": "V11", "sub": "c", "tool": name, "path": f"/properties/{pk}", "detail": "property dropped (target has no schema)"})
            if not (s.get("properties")): F.append({"rule": "V11", "sub": "a", "tool": name, "path": "/", "detail": "schema dropped"})
            continue
        if not isinstance(s, dict): continue
        _diff(canon(s), canon(d), "", F, name)
    return F

def anthropic_tools(req): return {t.get("name"): t.get("input_schema") for t in (req.get("tools") or []) if isinstance(t, dict)}
