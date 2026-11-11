"""Tool JSON-Schema structural/constraint checker (`S11` T1 vs `R06` T3) for Oracle v1.0 (`PREREG_V100.md` §3-4).
Imports `artifacts/g1/m1/procheck/schemacanon.py` read-only (`PREREG_V100.md` §1).
All keyword sets are loaded from `carriers.py` (`carriers.json`).
"""
import json
import os
import sys
from . import carriers as C

_HERE = os.path.dirname(os.path.abspath(__file__))
from . import schemacanon as _sc  # read-only import authorized by PREREG_V100.md §1


def _norm_ws(s):
    return " ".join(str(s).split()) if isinstance(s, str) else ""


def _diff_canon(a, b, path, out, tool_name, annot_keys, struct_keys, skw):
    """Compare canonical JSON Schemas `a` (source) and `b` (target), stratifying into S11 (T1) vs R06 (T3)."""
    if not isinstance(a, dict) or not isinstance(b, dict):
        if a != b:
            out.append(("S11", "T1", f"tool {tool_name}{path or '/'}: {json.dumps(a)[:40]} -> {json.dumps(b)[:40]}"))
        return

    props_key = skw["properties_key"]
    comb_keys = set(skw["combinator_list_keys"])
    nested_keys = set(skw["nested_schema_keys"])

    for k, v in a.items():
        if k == props_key and isinstance(v, dict):
            bp = b.get(props_key) if isinstance(b.get(props_key), dict) else {}
            for pk, pv in v.items():
                if pk not in bp:
                    out.append(("S11", "T1", f"tool {tool_name}{path}/{props_key}/{pk}: property dropped"))
                else:
                    _diff_canon(pv, bp[pk], f"{path}/{props_key}/{pk}", out, tool_name, annot_keys, struct_keys, skw)
            continue

        if k in annot_keys:
            if k not in b:
                out.append(("R06", "T3", f"tool {tool_name}{path}/{k}: schema annotation dropped"))
            elif v != b[k]:
                out.append(("R06", "T3", f"tool {tool_name}{path}/{k}: schema annotation changed"))
            continue

        if k not in b:
            out.append(("S11", "T1", f"tool {tool_name}{path}/{k}: constraint keyword dropped ({json.dumps(v)[:40]})"))
            continue

        if isinstance(v, dict) and isinstance(b[k], dict) and (k in nested_keys or k in struct_keys):
            _diff_canon(v, b[k], f"{path}/{k}", out, tool_name, annot_keys, struct_keys, skw)
            continue

        if isinstance(v, list) and isinstance(b[k], list) and len(v) == len(b[k]) and k in comb_keys:
            for i, (x, y) in enumerate(zip(v, b[k])):
                _diff_canon(x, y, f"{path}/{k}/{i}", out, tool_name, annot_keys, struct_keys, skw)
            continue

        if v != b[k]:
            out.append(("S11", "T1", f"tool {tool_name}{path}/{k}: {json.dumps(v)[:40]} -> {json.dumps(b[k])[:40]}"))

    for k, bv in b.items():
        if k == props_key:
            if isinstance(bv, dict):
                ap = a.get(props_key) if isinstance(a.get(props_key), dict) else {}
                for pk in bv:
                    if pk not in ap:
                        out.append(("S11", "T1", f"tool {tool_name}{path}/{props_key}/{pk}: property added"))
            continue
        if k not in a:
            if k in annot_keys:
                out.append(("R06", "T3", f"tool {tool_name}{path}/{k}: schema annotation added"))
            else:
                out.append(("S11", "T1", f"tool {tool_name}{path}/{k}: constraint keyword added ({json.dumps(bv)[:40]})"))


def _has_unresolved_refs(node, ref_keys):
    if isinstance(node, dict):
        if any(rk in node for rk in ref_keys):
            return True
        return any(_has_unresolved_refs(v, ref_keys) for v in node.values())
    if isinstance(node, list):
        return any(_has_unresolved_refs(x, ref_keys) for x in node)
    return False


def _has_gemini_null_anyof(node, skw):
    any_of_k = skw["any_of_key"]
    type_k = skw["type_key"]
    null_v = skw["null_type_val"]
    if isinstance(node, dict):
        ao = node.get(any_of_k)
        if isinstance(ao, list) and any(isinstance(x, dict) and x.get(type_k) == null_v for x in ao):
            return True
        return any(_has_gemini_null_anyof(v, skw) for v in node.values())
    if isinstance(node, list):
        return any(_has_gemini_null_anyof(x, skw) for x in node)
    return False


def check_schemas(ir_src, ir_dst, *, disabled=frozenset()):
    """Compare tool schemas, descriptions, and strict flags between ir_src and ir_dst.
    Returns list of (rule_id, tier, detail) tuples.
    """
    findings = []
    cm = C.common()
    annot_keys = set(cm["schema_annotation_keys"])
    struct_keys = set(cm["schema_structural_keys"])
    skw = cm["schema_keywords"]
    props_key = skw["properties_key"]
    desc_key = skw["description_key"]
    ref_keys = set(skw["ref_keys"])
    p5_disabled = "P5" in disabled

    src_tools = ir_src.get("tools") or {}
    dst_tools = ir_dst.get("tools") or {}

    for name in sorted(src_tools):
        if name not in dst_tools:
            continue  # S09 handles dropped/renamed tools
        ta = src_tools[name]
        tb = dst_tools[name]
        if ta.get("server_type") is not None:
            continue

        # 1. strict flag check (S03 T1 / S11 T1)
        if ta.get("strict") != tb.get("strict"):
            findings.append(("S03", "T1", f"tool {name}: strict flag changed {ta.get('strict')} -> {tb.get('strict')}"))
            if not p5_disabled:
                findings.append(("S11", "T1", f"tool {name}: strict constraint changed {ta.get('strict')} -> {tb.get('strict')}"))

        # 2. Tool-level description (R06 T3)
        if not p5_disabled:
            da = _norm_ws(ta.get("description"))
            db = _norm_ws(tb.get("description"))
            if da != db:
                findings.append(("R06", "T3", f"tool {name}: description changed {da[:40]!r} -> {db[:40]!r}"))

        # 3. JSON Schema comparison
        sa = ta.get("schema")
        sb = tb.get("schema")
        if p5_disabled:
            # Ablation -P5: revert to v0.8b top-level property key set comparison only
            pa = sorted((sa.get(props_key) or {}).keys()) if isinstance(sa, dict) else []
            pb = sorted((sb.get(props_key) or {}).keys()) if isinstance(sb, dict) else []
            if pa != pb:
                findings.append(("S09", "T1", f"tool {name} top-level param_keys changed: {pa} -> {pb}"))
            continue

        if isinstance(sa, dict) and not isinstance(sb, dict):
            findings.append(("S11", "T1", f"tool {name}: schema dropped on target"))
            continue
        if not isinstance(sa, dict):
            continue

        # Check raw object schema properties key deletion in-place (before schemacanon normalizes empty properties)
        if isinstance(sa, dict) and isinstance(sb, dict):
            if sa.get(skw["type_key"]) == skw["object_val"] and (props_key in sa) and (props_key not in sb):
                findings.append(("S11", "T1", f"tool {name}: object schema {props_key!r} key deleted in-place"))

        # Check raw target schema on Gemini for unresolved $ref/$defs and unsupported anyOf null
        if ir_dst["dialect"] == "gemini_generatecontent" and isinstance(sb, dict):
            if _has_unresolved_refs(sb, ref_keys):
                findings.append(("S11", "T1", f"tool {name}: unresolved ref/defs emitted to gemini_generatecontent"))
            if _has_gemini_null_anyof(sb, skw) and not _has_gemini_null_anyof(sa, skw):
                findings.append(("S11", "T1", f"tool {name}: unsupported anyOf null branch emitted to gemini_generatecontent"))

        ca = _sc.canon(sa)
        cb = _sc.canon(sb)
        # Keep title/default/examples/$comment comparison for R06 even if schemacanon strips title
        if isinstance(sa, dict) and isinstance(sb, dict):
            for ak in annot_keys:
                if ak == desc_key:
                    continue  # handled inside _diff_canon
                if (ak in sa) != (ak in sb) or (ak in sa and sa.get(ak) != sb.get(ak)):
                    findings.append(("R06", "T3", f"tool {name}/{ak}: schema annotation changed"))
        _diff_canon(ca, cb, "", findings, name, annot_keys, struct_keys, skw)

    return findings
