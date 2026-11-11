"""Core comparator for Oracle v1.0.
Exposes `Finding(rule_id, tier, object_id, detail)` and
`compare(src_body, src_dialect, dst_body, dst_dialect, side, *, context=None) -> list[Finding]`.
Zero vendor wire field names or sentinel strings live in Python code; all come from `carriers.json`.
"""
from collections import namedtuple
import json
import os
import re

from . import carriers as C
from .ir import parse_ir
from .order import check_order, _canonicalize_refs, _coalesce_adjacent_texts, _ws
from .schema import check_schemas

Finding = namedtuple("Finding", ["rule_id", "tier", "object_id", "detail"])

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "..", "data", "catalog.json"), "r", encoding="utf-8") as _f:
    CATALOG = {r["id"]: r for r in json.load(_f)["rules"]}


def _parse_disabled(context=None):
    raw = os.environ.get("V100_DISABLE", "")
    out = {x.strip() for x in raw.split(",") if x.strip()}
    if isinstance(context, dict) and context.get("disabled"):
        out |= set(context["disabled"])
    return frozenset(out)


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _same_val(a, b):
    if _num(a) and _num(b):
        return float(a) == float(b)
    return json.dumps(a, sort_keys=True, ensure_ascii=False) == json.dumps(b, sort_keys=True, ensure_ascii=False)


def _emit(findings, rule_id, detail, object_id, disabled):
    base_Id = rule_id.split(".")[0]
    tier = CATALOG[base_Id]["tier"]
    if "P1" in disabled:
        tier = "T1"
    findings.append(Finding(rule_id, tier, object_id, detail))


def _check_integrity_s10(ir_src, ir_dst, findings, object_id, disabled, context):
    """S10 INTEGRITY (T1, P4): provenance check on integrity tokens."""
    src_tokens = {tok for e in ir_src["events"] for tok in e["tokens"] if tok}
    if isinstance(context, dict) and context.get("vendor_tokens"):
        src_tokens |= {str(t) for t in context["vendor_tokens"] if t}

    sentinels = set(C.common()["known_bypass_sentinels"])
    p4_disabled = "P4" in disabled

    for e in ir_dst["events"]:
        for tok in e["tokens"]:
            if not tok:
                continue
            if p4_disabled:
                # Ablation -P4: revert to literal sentinel string matching only
                if tok in sentinels:
                    _emit(findings, "S10", f"{e['kind']} {e['ref'] or ''} carries known bypass sentinel {tok[:28]!r}", object_id, disabled)
            else:
                if tok not in src_tokens:
                    _emit(findings, "S10", f"integrity token on {e['kind']} {e['ref'] or ''} not in conversation provenance: {tok[:28]!r}", object_id, disabled)


def _check_flags_s03(ir_src, ir_dst, findings, object_id, disabled):
    """S03 FLAG (T1, P3): is_error across dialects, signature loss per assistant turn, misplaced flags."""
    ev_a = _canonicalize_refs(ir_src["events"], ir_src["phase"])
    ev_b = _canonicalize_refs(ir_dst["events"], ir_dst["phase"])

    # 1. Misplaced is_error on tool_call or stream delta (RS67, ST78)
    for e in ev_b:
        if "misplaced_is_error" in e["flags"]:
            _emit(findings, "S03", f"is_error flag misplaced on {e['kind']} {e['ref'] or ''}", object_id, disabled)

    # 2. tool_result is_error comparison by canonical ref
    res_a = {}
    for e in ev_a:
        if e["kind"] == "tool_result":
            res_a.setdefault(e["ref"], []).append(e)
    res_b = {}
    for e in ev_b:
        if e["kind"] == "tool_result":
            res_b.setdefault(e["ref"], []).append(e)

    for ref in sorted(set(res_a) & set(res_b)):
        for ea, eb in zip(res_a[ref], res_b[ref]):
            err_a = "is_error" in ea["flags"]
            err_b = "is_error" in eb["flags"]
            if err_a != err_b:
                _emit(findings, "S03", f"tool_result {ea.get('orig_ref') or ref} is_error flag {err_a} -> {err_b}", object_id, disabled)

    # 3. Signature presence per contiguous assistant turn (v0.9 P11 semantics)
    def _asst_groups(ev_list):
        groups, cur = [], None
        for e in ev_list:
            if e["src"] == "assistant":
                if cur is None:
                    cur = []
                    groups.append(cur)
                cur.append(e)
            else:
                cur = None
        return groups

    ga_list = _asst_groups(ev_a)
    gb_list = _asst_groups(ev_b)
    for t_idx, (ga, gb) in enumerate(zip(ga_list, gb_list)):
        sa = [e for e in ga if "sig" in e["flags"]]
        sb = [e for e in gb if "sig" in e["flags"]]
        if sa and not sb:
            # Fire S03 only if at least one signed event survived in gb (otherwise S05 covers the dropped event)
            wb = {(e["kind"], _ws(e["payload"])) for e in gb} | {(e["kind"], e["ref"]) for e in gb if e["ref"] is not None}
            survived = any((e["kind"], _ws(e["payload"])) in wb or (e["ref"] is not None and (e["kind"], e["ref"]) in wb) for e in sa)
            if survived:
                _emit(findings, "S03", f"signature carrier lost in assistant turn {t_idx} on surviving {sorted({e['kind'] for e in sa})}", object_id, disabled)
        ra = [e for e in ga if "redacted" in e["flags"] and e["tokens"]]
        rb = [e for e in gb if "redacted" in e["flags"] and e["tokens"]]
        if ra and not rb and any("redacted" in e["flags"] for e in gb):
            _emit(findings, "S03", f"redacted_thinking data token lost in assistant turn {t_idx}", object_id, disabled)


def _check_refs_s06(ir_src, ir_dst, findings, object_id, disabled):
    """S06 REF (T1, P1): broken tool_call<->tool_result pairing, Gemini functionResponse pairing, id length/charset."""
    ev_a = _canonicalize_refs(ir_src["events"], ir_src["phase"])
    ev_b = _canonicalize_refs(ir_dst["events"], ir_dst["phase"])

    # Compare canonical call/result reference multisets (order is checked by S07/R07)
    calls_a = [e["ref"] for e in ev_a if e["kind"] == "tool_call"]
    calls_b = [e["ref"] for e in ev_b if e["kind"] == "tool_call"]
    res_a = sorted(e["ref"] for e in ev_a if e["kind"] == "tool_result")
    res_b = sorted(e["ref"] for e in ev_b if e["kind"] == "tool_result")
    if calls_a != calls_b or res_a != res_b:
        _emit(findings, "S06", f"tool call/result reference pairing broken: calls {calls_a}->{calls_b}, results {res_a}->{res_b}", object_id, disabled)

    # On response/stream, check required tool_call id on client side and id preservation
    if ir_dst["phase"] in ("response", "stream"):
        tc_a_ev = [e for e in ev_a if e["kind"] == "tool_call"]
        tc_b_ev = [e for e in ev_b if e["kind"] == "tool_call"]
        for idx, eb in enumerate(tc_b_ev):
            if ir_dst["dialect"] != "gemini_generatecontent" and not eb.get("wire_id"):
                _emit(findings, "S06", f"response tool_call[{idx}] missing required id", object_id, disabled)
        for idx, (ea, eb) in enumerate(zip(tc_a_ev, tc_b_ev)):
            if ea.get("orig_ref") and eb.get("orig_ref") and ea["orig_ref"] != eb["orig_ref"]:
                _emit(findings, "S06", f"response tool_call[{idx}] id changed: {ea['orig_ref']!r} -> {eb['orig_ref']!r}", object_id, disabled)

    # Check orphan tool_result (referencing unknown call "?...")
    for e in ev_b:
        if e["kind"] == "tool_result" and str(e["ref"]).startswith("?"):
            _emit(findings, "S06", f"tool_result references unknown call_id {e.get('orig_ref')!r}", object_id, disabled)

    # Check target dialect wire_id constraints from carriers.json (e.g. OpenAI max_length 40 -> #18160; Anthropic regex/64)
    if ir_dst["phase"] == "request":
        t_dst = C.table(ir_dst["dialect"], "request")
        idc = t_dst.get("id_constraints") or {}
        max_len = idc.get("max_length")
        pat_str = idc.get("pattern")
        pat = re.compile(pat_str) if pat_str else None
        for e in ir_dst["events"]:
            if e["kind"] not in ("tool_call", "tool_result"):
                continue
            wid = e.get("wire_id")
            if not isinstance(wid, str) or not wid:
                continue
            if max_len is not None and len(wid) > max_len:
                _emit(findings, "S06", f"{e['kind']} id length {len(wid)} exceeds {ir_dst['dialect']} max_length {max_len}: {wid[:32]!r}...", object_id, disabled)
            if pat is not None and not pat.match(wid):
                _emit(findings, "S06", f"{e['kind']} id violates {ir_dst['dialect']} charset pattern {pat_str}: {wid[:32]!r}", object_id, disabled)

    # Gemini functionResponse name/id pairing (V17 / V17b) and allowedFunctionNames (V06b)
    if ir_dst["dialect"] == "gemini_generatecontent" and ir_dst["phase"] == "request":
        # Map source call index -> name
        src_calls = [e for e in ev_a if e["kind"] == "tool_call"]
        src_call_name_by_ref = {e["ref"]: e.get("name") for e in src_calls}
        src_res = [e for e in ev_a if e["kind"] == "tool_result"]
        dst_calls = [e for e in ev_b if e["kind"] == "tool_call"]
        dst_res = [e for e in ev_b if e["kind"] == "tool_result"]
        for idx, (ra_e, rb_e) in enumerate(zip(src_res, dst_res)):
            want_name = src_call_name_by_ref.get(ra_e["ref"])
            if want_name and rb_e.get("name") and rb_e["name"] != want_name:
                _emit(findings, "S06", f"Gemini functionResponse[{idx}].name {rb_e['name']!r} != expected {want_name!r}", object_id, disabled)
        if any(c.get("wire_id") for c in dst_calls) and len(src_res) == len(dst_res):
            dst_call_wid_by_ref = {c["ref"]: c.get("wire_id") for c in dst_calls}
            for idx, (ra_e, rb_e) in enumerate(zip(src_res, dst_res)):
                want_wid = dst_call_wid_by_ref.get(ra_e["ref"])
                if want_wid is not None:
                    if rb_e.get("wire_id") is None:
                        _emit(findings, "S06", f"Gemini functionResponse[{idx}] missing id; answering functionCall has id {want_wid!r}", object_id, disabled)
                    elif str(rb_e.get("wire_id")) != str(want_wid):
                        _emit(findings, "S06", f"Gemini functionResponse[{idx}] id {rb_e.get('wire_id')!r} != functionCall id {want_wid!r}", object_id, disabled)

        tc_dst = ir_dst["controls"].get("tool_choice")
        if isinstance(tc_dst, dict) and isinstance(tc_dst.get("allowed"), list):
            allowed = tc_dst["allowed"]
            declared = [k for k, v in ir_src["tools"].items() if v.get("server_type") is None]
            undeclared = [n for n in allowed if n not in declared]
            tc_src = ir_src["controls"].get("tool_choice") or {}
            src_mode = tc_src.get("mode") if isinstance(tc_src, dict) else None
            if undeclared:
                _emit(findings, "S06", f"Gemini allowedFunctionNames contains undeclared tool(s): {undeclared}", object_id, disabled)
            elif src_mode == "tool":
                if sorted(allowed) != [tc_src.get("name")]:
                    _emit(findings, "S06", f"Gemini allowedFunctionNames {allowed} != forced tool [{tc_src.get('name')}]", object_id, disabled)
            elif src_mode == "any":
                if sorted(allowed) != sorted(declared):
                    _emit(findings, "S06", f"Gemini allowedFunctionNames {allowed} is strict subset of declared tools {declared}", object_id, disabled)
            else:
                _emit(findings, "S06", f"Gemini allowedFunctionNames {allowed} set when source tool_choice is {src_mode or 'auto'}", object_id, disabled)


def _check_events_content(ir_src, ir_dst, findings, object_id, disabled):
    """Compare events for S01 (MERGE), S02 (SOURCE), D02 (LOWER), S04 (REINTERP), S05 (CONTENT), S09 (TOOLNAME)."""
    src_has_sig = any("sig" in e.get("flags", ()) for e in ir_src["events"])
    dst_has_sig = any("sig" in e.get("flags", ()) for e in ir_dst["events"])
    no_refusal_block = ir_dst["dialect"] in ("anthropic_messages", "gemini_generatecontent")

    def _prep(ir, other_has_sig):
        ev = _canonicalize_refs(ir["events"], ir["phase"])
        out = []
        for e in ev:
            if e["kind"] == "text" and e["payload"] == "":
                continue
            if e["kind"] == "thinking" and e["payload"] == "" and "sig" in e.get("flags", ()) and other_has_sig:
                continue
            d = dict(e)
            if no_refusal_block and d["kind"] == "refusal":
                d["kind"] = "text" 
            # Image provenance normalization (v0.9 P10: tool image vs sibling user image)
            if d["kind"] == "image" and str(d["src"]).startswith("tool"):
                d["src"] = "user"
            # JSON normalization on tool_result payload (L06 + v0.9 P10 typed-text-list stringification)
            if d["kind"] == "tool_result" and isinstance(d["payload"], str):
                s = d["payload"].strip()
                if s[:1] in "{[":
                    try:
                        d["payload"] = json.dumps(json.loads(s), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                    except Exception:
                        pass
                for _ in range(2):
                    s = d["payload"].strip()
                    if s[:1] != "[":
                        break
                    try:
                        v = json.loads(s)
                    except Exception:
                        break
                    t_anth = C.table("anthropic_messages", "request")
                    allowed_keys = {t_anth["block_type_key"], t_anth["text_key"], t_anth["cache_hint_key"]}
                    if not (isinstance(v, list) and v and all(isinstance(x, dict) and x.get(t_anth["block_type_key"]) == t_anth["block_types"]["text"] and set(x) <= allowed_keys for x in v)):
                        break
                    txt = "".join(str(x.get(t_anth["text_key"], "")) for x in v)
                    if txt.strip()[:1] in "{[":
                        try:
                            txt = json.dumps(json.loads(txt), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
                        except Exception:
                            pass
                    d["payload"] = txt
            out.append(d)
        coal, _ = _coalesce_adjacent_texts(out)
        return coal

    A = _prep(ir_src, dst_has_sig)
    B = _prep(ir_dst, src_has_sig)

    # On openai_chat, if L07 hoisted text_after before tool_call, coalesce same-turn assistant texts for content multiset match
    if ir_dst["dialect"] == "openai_chat" and "P2" not in disabled:
        def _coalesce_turn_asst_texts(ev_list):
            res = []
            i = 0
            while i < len(ev_list):
                if ev_list[i]["src"] != "assistant":
                    res.append(ev_list[i])
                    i += 1
                    continue
                j = i
                while j < len(ev_list) and ev_list[j]["src"] == "assistant":
                    j += 1
                grp = ev_list[i:j]
                texts = [g for g in grp if g["kind"] == "text"]
                others = [g for g in grp if g["kind"] != "text"]
                if len(texts) >= 2:
                    merged = dict(texts[0])
                    merged["payload"] = "".join(t["payload"] for t in texts)
                    merged["n"] = sum(t.get("n", 1) for t in texts)
                    res.append(merged)
                    res.extend(others)
                else:
                    res.extend(grp)
                i = j
            return res
        # Only coalesce for content-loss check if B has single-merged assistant text
        A_check = _coalesce_turn_asst_texts(A)
        B_check = _coalesce_turn_asst_texts(B)
    else:
        A_check, B_check = A, B

    def _ekey(e):
        return (e["src"], e["kind"], e["ref"], _ws(e["payload"]))

    nblocks_b = {}
    for e in B_check:
        if e["kind"] == "text":
            nblocks_b.setdefault(_ekey(e), []).append(e.get("n", 1))

    ka = [_ekey(e) for e in A_check]
    kb = [_ekey(e) for e in B_check]

    # Multiset diff
    rem_b = list(kb)
    lost = []
    for k in ka:
        if k in rem_b:
            rem_b.remove(k)
        else:
            lost.append(k)
    rem_a = list(ka)
    new = []
    for k in kb:
        if k in rem_a:
            rem_a.remove(k)
        else:
            new.append(k)

    # Check malformed flags on B events (e.g. tool_use input is string instead of object)
    for e in B_check:
        for fl in e.get("flags", ()):
            if fl.startswith("malformed:"):
                _emit(findings, "S04", f"malformed payload on {e['kind']}: {fl}", object_id, disabled)

    # 1. Cross-source merge (S01) vs speaker change / lowering (S02 / D02)
    for n in list(new):
        if n[1] != "text":
            continue
        parts = [l for l in lost if l[1] == "text" and l[3] and l[3] in n[3]]
        parts.sort(key=lambda l: n[3].find(l[3]))
        if len(parts) >= 2 and "".join(p[3] for p in parts) == n[3]:
            srcs = sorted({p[0] for p in parts})
            foreign = [p for p in parts if p[0] != n[0]]
            blocks = (nblocks_b.get(n) or [1])[0]
            if len(srcs) > 1 and foreign and len(parts) - len(foreign) == 1 and blocks >= len(parts):
                for p in foreign:
                    if p[0] == "developer" and n[0] == "user":
                        _emit(findings, "D02", f"developer -> user lowering (adjacent to user text): {p[3][:50]!r}", object_id, disabled)
                    else:
                        _emit(findings, "S02", f"speaker changed {p[0]} -> {n[0]}: {p[3][:50]!r}", object_id, disabled)
            elif len(srcs) > 1 or n[0] not in srcs:
                _emit(findings, "S01", f"cross-source merge {srcs} -> one {n[0]} text: {n[3][:60]!r}", object_id, disabled)
            for p in parts:
                if p in lost:
                    lost.remove(p)
            new.remove(n)

    # Converse split around non-text (already flagged by S07 in order.py; remove from lost/new so not double-counted as S05)
    for l in list(lost):
        if l[1] != "text" or not l[3]:
            continue
        parts = [n for n in new if n[1] == "text" and n[0] == l[0] and n[3] and n[3] in l[3]]
        parts.sort(key=lambda n: l[3].find(n[3]))
        if len(parts) >= 2 and "".join(p[3] for p in parts) == l[3]:
            lost.remove(l)
            for p in parts:
                if p in new:
                    new.remove(p)

    # 2. Direct source relabeling: S02 (user<->assistant<->tool) vs D02 (developer->user/developer lowering)
    for l in list(lost):
        m = [n for n in new if n[1:] == l[1:] and n[0] != l[0]]
        if m:
            n0 = m[0]
            if l[0] == "developer" and n0[0] in ("user", "developer"):
                _emit(findings, "D02", f"system/developer lowered to {n0[0]}: {l[3][:50]!r}", object_id, disabled)
            else:
                _emit(findings, "S02", f"speaker changed {l[0]} -> {n0[0]}: {l[3][:50]!r}", object_id, disabled)
            lost.remove(l)
            new.remove(n0)

    # 3. S04 REINTERP on tool_result (same ref, different payload)
    for l in list(lost):
        if l[1] == "tool_result":
            m = [n for n in new if n[1] == "tool_result" and n[2] == l[2]]
            if m:
                _emit(findings, "S04", f"tool_result {l[2]} payload reinterpreted: {l[3][:40]!r} -> {m[0][3][:40]!r}", object_id, disabled)
                lost.remove(l)
                new.remove(m[0])

    # 4. S09 TOOLNAME vs S04 REINTERP on tool_call (same ref)
    for l in list(lost):
        if l[1] == "tool_call":
            m = [n for n in new if n[1] == "tool_call" and n[2] == l[2]]
            if m:
                n0 = m[0]
                la_parts = l[3].split("|", 1)
                nb_parts = n0[3].split("|", 1)
                if len(la_parts) == 2 and len(nb_parts) == 2:
                    if la_parts[0] != nb_parts[0]:
                        _emit(findings, "S09", f"tool_call {l[2]} name altered: {la_parts[0]!r} -> {nb_parts[0]!r}", object_id, disabled)
                    if la_parts[1] != nb_parts[1]:
                        _emit(findings, "S04", f"tool_call {l[2]} arguments altered: {la_parts[1][:40]!r} -> {nb_parts[1][:40]!r}", object_id, disabled)
                else:
                    _emit(findings, "S04", f"tool_call {l[2]} altered: {l[3][:40]!r} -> {n0[3][:40]!r}", object_id, disabled)
                lost.remove(l)
                new.remove(n0)

    # 5. Embedded text with adapter-inserted prefix/material
    for n in list(new):
        if n[1] != "text":
            continue
        parts = [l for l in lost if l[1] == "text" and l[3] and l[3] in n[3]]
        if not parts:
            continue
        parts.sort(key=lambda l: n[3].find(l[3]))
        rest = n[3]
        for p in parts:
            rest = rest.replace(p[3], "", 1)
        for p in parts:
            if p[0] != n[0]:
                if p[0] == "developer" and n[0] == "user":
                    _emit(findings, "D02", f"developer -> user (embedded with adapter prefix): {p[3][:50]!r}", object_id, disabled)
                else:
                    _emit(findings, "S02", f"{p[0]} -> {n[0]} (embedded with adapter prefix): {p[3][:50]!r}", object_id, disabled)
            if p in lost:
                lost.remove(p)
        if rest:
            _emit(findings, "S05", f"invented {n[0]} text (adapter-inserted): {rest[:60]!r}", object_id, disabled)
        new.remove(n)

    # 6. Remaining lost/new non-empty content events -> S05 CONTENT
    for l in lost:
        _emit(findings, "S05", f"lost {l[0]} {l[1]} {l[3][:60]!r}", object_id, disabled)
    for n in new:
        _emit(findings, "S05", f"invented {n[0]} {n[1]} {n[3][:60]!r}", object_id, disabled)


def _check_controls_and_tools(ir_src, ir_dst, findings, object_id, disabled):
    """Compare request controls (S08 vs D01 vs D03) and tool declarations (S09)."""
    ca = ir_src["controls"]
    cb = ir_dst["controls"]
    t_dst = C.table(ir_dst["dialect"], "request")
    doc_defaults = t_dst.get("documented_defaults") or {}

    # D03: prompt caching hints & service_tier
    if ir_src["cache_hints_count"] > ir_dst["cache_hints_count"] and (
        ir_src["dialect"] == ir_dst["dialect"] or t_dst.get("supports_inline_cache_control", False)
    ):
        _emit(findings, "D03", f"cache_control hints dropped ({ir_src['cache_hints_count']} -> {ir_dst['cache_hints_count']})", object_id, disabled)
    max_cc = t_dst.get("max_cache_control_blocks")
    if max_cc and ir_dst["cache_hints_count"] > max_cc and ir_src["cache_hints_count"] <= max_cc:
        _emit(findings, "D03", f"cache_control blocks exceed target limit ({ir_dst['cache_hints_count']} > {max_cc})", object_id, disabled)
    if ca.get("service_tier") != cb.get("service_tier"):
        if ca.get("service_tier") is not None or cb.get("service_tier") is not None:
            _emit(findings, "D03", f"service_tier {ca.get('service_tier')!r} -> {cb.get('service_tier')!r}", object_id, disabled)

    # R03: stop list -> scalar string representation change (same elements)
    if ca.get("stop") and cb.get("stop") and _same_val(ca.get("stop"), cb.get("stop")):
        if cb.get("stop_form") == "str" and isinstance(ca.get("stop"), list) and ca.get("stop_form") != "str":
            _emit(findings, "R03", f"control stop converted from list to scalar string: {cb.get('stop')!r}", object_id, disabled)

    # S08 / D01 on controls
    # Controls where adapter injection when source is unset is D01 (T2) vs S08 (T1)
    d01_injectable = {"seed", "top_k"}
    numeric_ctls = {"temperature", "top_p", "top_k", "seed", "max_tokens", "presence_penalty", "frequency_penalty", "candidate_count", "n"}

    all_ctl_keys = sorted((set(ca) | set(cb)) - {"metadata", "service_tier", "stop_form"})
    for k in all_ctl_keys:
        va = ca.get(k)
        vb = cb.get(k)
        if k in numeric_ctls and vb is not None and not _num(vb):
            _emit(findings, "S08", f"control {k} has invalid non-numeric type: {vb!r}", object_id, disabled)
            continue
        if _same_val(va, vb):
            continue
        if k == "tool_choice":
            # {"mode": "auto"} is the documented default when tools are present
            if (va == {"mode": "auto"} and vb is None) or (va is None and vb == {"mode": "auto"}):
                continue
        if k == "parallel_tool_calls":
            # parallel_tool_calls=True (disable_parallel_tool_use=False) is the documented default
            if va is True and vb is None:
                continue
            if va is None and vb is True:
                _emit(findings, "D01", f"control {k} injected with documented default {vb!r}", object_id, disabled)
                continue
        if va is not None:
            # Source explicitly set this control, and target dropped or changed it -> S08 (T1)
            if k == "tool_choice" and isinstance(va, dict) and isinstance(vb, dict):
                if va.get("mode") == vb.get("mode") and va.get("name") == vb.get("name"):
                    continue
            # Cross-dialect thinking control drop where target dialect lacks a native thinking control is not S08 (block/sig loss is S05/S03)
            if k == "thinking" and ir_src["dialect"] != ir_dst["dialect"] and vb is None:
                continue
            _emit(findings, "S08", f"control {k} changed/dropped: {va!r} -> {vb!r}", object_id, disabled)
        else:
            # Source left control unset (va is None), target injected vb
            if k in doc_defaults and _same_val(vb, doc_defaults[k]):
                _emit(findings, "D01", f"control {k} injected with documented default {vb!r}", object_id, disabled)
            elif k in d01_injectable:
                _emit(findings, "D01", f"adapter-default control {k} injected: {vb!r}", object_id, disabled)
            else:
                _emit(findings, "S08", f"unlicensed control {k} injected: {vb!r}", object_id, disabled)

    # S09: tool name declarations
    fn_tools_a = sorted(k for k, v in ir_src["tools"].items() if v.get("server_type") is None)
    fn_tools_b = sorted(k for k, v in ir_dst["tools"].items() if v.get("server_type") is None)
    if fn_tools_a != fn_tools_b:
        _emit(findings, "S09", f"declared tool names changed: {fn_tools_a} -> {fn_tools_b}", object_id, disabled)

    # Server tools declared on source but dropped on target -> S05 (rank 2: litellm #17737)
    srv_tools_a = sorted(k for k, v in ir_src["tools"].items() if v.get("server_type") is not None)
    srv_tools_b = sorted(k for k, v in ir_dst["tools"].items() if v.get("server_type") is not None)
    if srv_tools_a and not srv_tools_b:
        _emit(findings, "S05", f"server tool declarations dropped: {srv_tools_a}", object_id, disabled)

    # Required model key check on request (req_m249: missing model -> HTTP 400 -> T1 stays; model name change -> D05 T2)
    if ir_dst["dialect"] in ("openai_chat", "openai_responses", "anthropic_messages"):
        if ir_src["has_model_key"] and not ir_dst["has_model_key"]:
            _emit(findings, "S08", f"required model key dropped on {ir_dst['dialect']} request", object_id, disabled)
        elif ir_src["has_model_key"] and ir_dst["has_model_key"] and ir_src["model"] != ir_dst["model"]:
            # Only flag D05 on request if src and dst are the same dialect (e.g. X-O-53 mutant)
            if ir_src["dialect"] == ir_dst["dialect"]:
                _emit(findings, "D05", f"request model changed: {ir_src['model']!r} -> {ir_dst['model']!r}", object_id, disabled)


def _check_response_and_stream(ir_src, ir_dst, side, findings, object_id, disabled, context):
    """Check S12 (STOP), S13 (STREAM), D04 (USAGE), D05 (MODEL), D06 (ENVELOPE-LEAK), R01, R04, R05, R08."""
    p6_disabled = "P6" in disabled

    # S12 STOP (T1, P6)
    if not p6_disabled:
        src_stop = ir_src["stop"]
        dst_stop = ir_dst["stop"]
        if src_stop == "end_turn" and any(e["kind"] == "tool_call" for e in ir_src["events"]):
            src_stop = "tool_use"
        src_has_refusal = any(e["kind"] == "refusal" for e in ir_src["events"])
        dst_has_call = any(e["kind"] == "tool_call" for e in ir_dst["events"])
        stop_ok = (src_stop == dst_stop)
        if not stop_ok and ir_dst["dialect"] == "anthropic_messages":
            # Anthropic StopReason uses "refusal" as its filter/safety/refusal stop reason, and has no "other" enum value
            if src_stop == "content_filter" and dst_stop == "refusal":
                stop_ok = True
            elif src_has_refusal and dst_stop in ("refusal", "end_turn"):
                stop_ok = True
            elif src_stop == "other" and dst_stop == "end_turn":
                stop_ok = True
        if dst_stop == "end_turn" and dst_has_call:
            stop_ok = False
        if not stop_ok:
            _emit(findings, "S12", f"stop/finish reason semantic class changed: {src_stop!r} ({ir_src['stop_raw']!r}) -> {dst_stop!r} ({ir_dst['stop_raw']!r})", object_id, disabled)
        if ir_dst.get("stop_seq") is not None and ir_src.get("stop_seq") is None and ir_dst["stop_raw"] not in C.table(ir_dst["dialect"], "response").get("stop_sequence_allowed_reasons", []):
            _emit(findings, "S12", f"stop_sequence carrier invented when source did not stop on stop_sequence: {ir_dst.get('stop_seq')!r}", object_id, disabled)

    # S13 STREAM (T1, P6)
    ledger = ir_dst.get("stream_ledger")
    if side == "stream" and ledger and not p6_disabled:
        for ev_t in ledger.get("events_after_stop", ()):
            _emit(findings, "S13", f"stream event {ev_t} emitted after message_stop", object_id, disabled)
        for ub in ledger.get("unpaired_blocks", ()):
            _emit(findings, "S13", f"stream block pairing broken: {ub}", object_id, disabled)
        for idx, val in ledger.get("delta_is_error_events", ()):
            _emit(findings, "S13", f"is_error={val!r} emitted on stream delta {idx}", object_id, disabled)
            _emit(findings, "S03", f"is_error={val!r} invented on stream delta {idx}", object_id, disabled)
        for anom in ledger.get("anomalies", ()):
            _emit(findings, "S13", f"stream protocol anomaly: {anom}", object_id, disabled)
        for idx in ledger.get("start_nonempty_tool_inputs", ()):
            if idx in ledger.get("overwritten_start_tool_inputs", ()):
                _emit(findings, "S13", f"non-empty content_block_start tool input at index {idx} overwritten by input_json_delta", object_id, disabled)
            else:
                _emit(findings, "R03", f"tool input delivered in content_block_start at index {idx} instead of input_json_delta", object_id, disabled)

        if isinstance(context, dict) and context.get("non_stream_body") is not None:
            ir_ns = parse_ir(context["non_stream_body"], ir_dst["dialect"], "response", disabled=disabled)
            if [e["payload"] for e in ir_ns["events"]] != [e["payload"] for e in ir_dst["events"]]:
                _emit(findings, "S13", "reassembled stream content != non-stream equivalent", object_id, disabled)

    # D04 USAGE (T2)
    ua, ub = ir_src["usage"], ir_dst["usage"]
    for uk in ("input", "output", "cache_read", "cache_write", "reasoning"):
        va, vb = ua.get(uk), ub.get(uk)
        if isinstance(vb, int) and vb < 0:
            _emit(findings, "D04", f"usage.{uk} is negative: {vb!r}", object_id, disabled)
            continue
        if va is not None and vb is not None and va != vb:
            # Cross-dialect accounting equivalences (OpenAI/Gemini prompt includes cache_read; Anthropic input excludes cache_read; Gemini candidates excludes reasoning)
            if uk == "input" and ((ub.get("cache_read") and vb + ub["cache_read"] == va) or (ua.get("cache_read") and va + ua["cache_read"] == vb)):
                continue
            if uk == "output" and ((ua.get("reasoning") and vb == va + ua["reasoning"]) or (ub.get("reasoning") and va == vb + ub["reasoning"])):
                continue
            _emit(findings, "D04", f"usage.{uk} changed: {va!r} -> {vb!r}", object_id, disabled)
        elif va is not None and va != 0 and vb in (None, 0) and uk in ("input", "output", "cache_read"):
            _emit(findings, "D04", f"usage.{uk} dropped: {va!r} -> {vb!r}", object_id, disabled)
        elif va in (None, 0) and vb not in (None, 0) and uk in ("input", "output", "cache_write"):
            _emit(findings, "D04", f"usage.{uk} invented: {va!r} -> {vb!r}", object_id, disabled)
    extra_u = ir_dst["envelope"].get("extra_usage_keys") or []
    t_resp = C.table("anthropic_messages", "response")
    if extra_u:
        if t_resp["stop_key"] in extra_u:
            _emit(findings, "D06", f"stop_reason duplicated inside usage object", object_id, disabled)
        other_u = [k for k in extra_u if k != t_resp["stop_key"]]
        if other_u:
            _emit(findings, "D04", f"foreign/extra keys leaked in usage: {other_u}", object_id, disabled)

    if ledger and ledger.get("usage_anomalies"):
        for ua_msg in ledger["usage_anomalies"]:
            _emit(findings, "D04", f"stream usage anomaly: {ua_msg}", object_id, disabled)

    # D05 MODEL (T2)
    if ir_dst["dialect"] == "anthropic_messages" and not ir_dst["has_model_key"]:
        _emit(findings, "D05", "response model key missing on anthropic_messages", object_id, disabled)
    elif ir_src["model"] is not None and ir_dst["model"] is not None and ir_src["model"] != ir_dst["model"]:
        _emit(findings, "D05", f"response model changed: {ir_src['model']!r} -> {ir_dst['model']!r}", object_id, disabled)

    # D06 ENVELOPE-LEAK (T2) vs R04 CUSTOMKEY (T3)
    fkeys = set(ir_dst["envelope"].get("foreign_keys") or [])
    if ledger and ledger.get("message_start_foreign_keys"):
        fkeys |= set(ledger["message_start_foreign_keys"])
    for fk in sorted(fkeys):
        _emit(findings, "D06", f"foreign dialect reserved key leaked on envelope: {fk!r}", object_id, disabled)

    ckeys = [k for k in (ir_dst["envelope"].get("extra_keys") or []) if k not in fkeys]
    if ledger and ledger.get("extra_event_keys"):
        ckeys.extend(ledger["extra_event_keys"])
    for ck in ckeys:
        _emit(findings, "R04", f"unknown custom key on envelope: {ck!r}", object_id, disabled)

    # R01 NULLABLE (T3)
    for nk in (ir_dst["envelope"].get("missing_nullable_keys") or []):
        if nk not in (ir_src["envelope"].get("missing_nullable_keys") or []):
            _emit(findings, "R01", f"absent nullable required key on response: {nk!r}", object_id, disabled)
    if ledger:
        for ev_n, nk in ledger.get("missing_nullable_events", ()):
            _emit(findings, "R01", f"absent nullable required key on stream {ev_n}: {nk!r}", object_id, disabled)

    # R08 ENVELOPE-ID (T3)
    if ir_dst["dialect"] == "anthropic_messages" and not ir_dst.get("has_id_key", True):
        _emit(findings, "R08", "envelope id key missing on anthropic_messages", object_id, disabled)
    else:
        id_a = ir_src["envelope"].get("id")
        id_b = ir_dst["envelope"].get("id")
        if id_a is not None and id_b is not None and id_a != id_b:
            _emit(findings, "R08", f"envelope id changed: {id_a!r} -> {id_b!r}", object_id, disabled)


def _check_representation_r02_r03_r05(ir_src, ir_dst, findings, object_id, disabled):
    """Check R02 (EMPTYSTR), R03 (TEXTFORM), R04 (request custom keys), R05 (EMPTYBLOCK)."""
    # R02: "" vs null content beside tool_calls
    forms_a = dict(ir_src.get("text_forms") or [])
    forms_b = dict(ir_dst.get("text_forms") or [])
    for idx in sorted(set(forms_a) & set(forms_b)):
        fa, fb = forms_a[idx], forms_b[idx]
        if {fa, fb} == {"empty_str_beside_tool_call", "null_beside_tool_call"}:
            _emit(findings, "R02", f"turn {idx}: empty string vs null content beside tool_calls", object_id, disabled)
        elif {fa, fb} == {"str", "list1"} and ir_src["dialect"] == ir_dst["dialect"]:
            _emit(findings, "R03", f"turn {idx}: string vs 1-element text array", object_id, disabled)
    for idx, fb in (ir_dst.get("text_forms") or []):
        if fb == "empty_reasoning_str" and (idx, "empty_reasoning_str") not in (ir_src.get("text_forms") or []):
            _emit(findings, "R02", f"turn {idx}: empty reasoning string injected", object_id, disabled)
    if ir_src["dialect"] == ir_dst["dialect"] and ir_dst["phase"] == "request":
        nfa = dict(ir_src.get("nullable_forms") or [])
        nfb = dict(ir_dst.get("nullable_forms") or [])
        for kp in sorted(set(nfa) | set(nfb)):
            va, vb = nfa.get(kp, "absent"), nfb.get(kp, "absent")
            if {va, vb} == {"null", "absent"}:
                _emit(findings, "R01", f"request {kp}: absent vs null", object_id, disabled)

    # R04 on request envelope / toolConfig
    for ck in (ir_dst["envelope"].get("extra_keys") or []):
        if ck not in (ir_dst["envelope"].get("foreign_keys") or []):
            if ir_dst["phase"] == "request":
                _emit(findings, "R04", f"unknown custom key on request: {ck!r}", object_id, disabled)

    # R05: empty text block/part/delta injected
    if "P6" not in disabled:
        if ir_dst["empty_text_blocks"] > ir_src["empty_text_blocks"]:
            diff = ir_dst["empty_text_blocks"] - ir_src["empty_text_blocks"]
            _emit(findings, "R05", f"{diff} empty text block(s)/delta(s) injected ({ir_src['empty_text_blocks']} -> {ir_dst['empty_text_blocks']})", object_id, disabled)


def compare(src_body, src_dialect, dst_body, dst_dialect, side, *, context=None):
    """Compare source and destination wire payloads on `side` ('request' | 'response' | 'stream').
    Returns a list of `Finding(rule_id, tier, object_id, detail)`.
    """
    disabled = _parse_disabled(context)
    object_id = (context or {}).get("object_id", "") if isinstance(context, dict) else ""

    src_phase = side if side in ("request", "response") else (
        "stream" if isinstance(src_body, (str, bytes, list)) else "response"
    )
    dst_phase = side

    ir_src = parse_ir(src_body, src_dialect, src_phase, disabled=disabled)
    ir_dst = parse_ir(dst_body, dst_dialect, dst_phase, disabled=disabled)

    findings = []

    # 0. Any direct wire-malformed findings recorded by the parser via carriers.json
    for rule_id, _tier, detail in ir_dst.get("wire_findings", ()):
        _emit(findings, rule_id, detail, object_id, disabled)

    # 1. S10 INTEGRITY (provenance check)
    _check_integrity_s10(ir_src, ir_dst, findings, object_id, disabled, context)

    # 2. S03 FLAG (is_error, signature loss, misplaced flags)
    _check_flags_s03(ir_src, ir_dst, findings, object_id, disabled)

    # 3. S06 REF (reference pairing & id constraints)
    _check_refs_s06(ir_src, ir_dst, findings, object_id, disabled)

    # 4. S07 ORDER & R07 SIBLING (causal partial order E1-E5)
    for rule_id, _tier, detail in check_order(ir_src, ir_dst, disabled=disabled):
        _emit(findings, rule_id, detail, object_id, disabled)

    # 5. S01, S02, D02, S04, S05, S09 (event content & speaker comparison)
    _check_events_content(ir_src, ir_dst, findings, object_id, disabled)

    # 6. Request controls & schemas (S08, S09, S11, D01, D03, R06)
    if side == "request":
        _check_controls_and_tools(ir_src, ir_dst, findings, object_id, disabled)
        for rule_id, _tier, detail in check_schemas(ir_src, ir_dst, disabled=disabled):
            _emit(findings, rule_id, detail, object_id, disabled)
    else:
        _check_response_and_stream(ir_src, ir_dst, side, findings, object_id, disabled, context)

    # 7. Representation rules R02, R03, R04, R05
    _check_representation_r02_r03_r05(ir_src, ir_dst, findings, object_id, disabled)

    return findings
