"""Unified 4-dialect × {request, response, stream} IR for Oracle v1.0.
All vendor wire field names, role names, block/item types, error carriers, and stop maps
are read exclusively from `carriers.py` (`carriers.json`).
"""
import base64
import hashlib
import json
import re
from . import carriers as C

_DATA_URL_RE = re.compile(r"^data:([^;,]*)((?:;[^;,]*)*),(.*)$", re.S)


def _canon_json(x):
    return json.dumps(x, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _canon_args(a):
    if isinstance(a, str):
        s = a.strip()
        if s == "":
            return "{}", []
        try:
            return _canon_json(json.loads(s)), []
        except Exception:
            return a, ["args_unparsable"]
    if a is None:
        return "{}", []
    return _canon_json(a), []


def _img_payload(media_type=None, data=None, url=None):
    if data is not None:
        try:
            raw = base64.b64decode(re.sub(r"\s+", "", str(data)), validate=False)
            h = hashlib.sha256(raw).hexdigest()
        except Exception:
            h = hashlib.sha256(str(data).encode("utf-8", "replace")).hexdigest()
        return _canon_json({"media_type": (media_type or "").lower(), "sha256": h})
    return _canon_json({"url": url})


def _img_from_data_or_url(url_val):
    if isinstance(url_val, dict):
        u = url_val.get(C.table("openai_chat", "request")["url_key"])
    else:
        u = url_val
    m = _DATA_URL_RE.match(str(u or ""))
    if m and "base64" in (m.group(2) or ""):
        return _img_payload(m.group(1), data=m.group(3))
    return _img_payload(url=u)


def _extract_oob_sig_tokens(obj, depth=0):
    if not isinstance(obj, dict):
        return []
    cm = C.common()
    sub = cm["signature_key_substring"]
    excl = set(cm["signature_exclude_keys"])
    out = []
    for k, v in obj.items():
        if k in excl:
            continue
        if sub in k.lower() and v and not isinstance(v, (dict, list, bool)):
            out.append(str(v))
        elif isinstance(v, dict) and depth < 3:
            out.extend(_extract_oob_sig_tokens(v, depth + 1))
    return out


def _split_id_sig(cid):
    delim = C.common()["id_embedded_sig_delim"]
    if isinstance(cid, str) and delim in cid:
        prefix, sig = cid.split(delim, 1)
        return prefix, [sig] if sig else []
    return cid, []


def make_event(turn_index, src, kind, payload="", ref=None, flags=(), tokens=(), empty_count=0,
               wire_id=None, name=None, raw_role=None, is_list_result=False):
    return {
        "turn_index": turn_index,
        "src": src,
        "kind": kind,
        "ref": ref,
        "payload": payload,
        "flags": sorted(set(flags)),
        "tokens": [str(t) for t in tokens if t is not None and str(t) != ""],
        "empty_count": int(empty_count),
        "wire_id": wire_id,
        "name": name,
        "raw_role": raw_role,
        "is_list_result": bool(is_list_result),
    }


def _blank_ir(dialect, phase):
    return {
        "dialect": dialect,
        "phase": phase,
        "events": [],
        "controls": {},
        "tools": {},
        "model": None,
        "has_model_key": False,
        "has_id_key": True,
        "stop": None,
        "stop_raw": None,
        "stop_seq": None,
        "usage": {"input": None, "output": None, "cache_read": None, "cache_write": None, "reasoning": None, "total": None},
        "envelope": {"id": None, "type": None, "role": None, "extra_keys": [], "foreign_keys": [], "missing_nullable_keys": []},
        "stream_ledger": None,
        "empty_text_blocks": 0,
        "text_forms": [],
        "nullable_forms": [],
        "wire_findings": [],
        "cache_hints_count": 0,
    }


# ----------------------------------------------------------------------
# REQUEST PARSERS
# ----------------------------------------------------------------------

def _parse_anthropic_request(req, disabled):
    t = C.table("anthropic_messages", "request")
    cm = C.common()
    ir = _blank_ir("anthropic_messages", "request")
    if not isinstance(req, dict):
        return ir
    ir["has_model_key"] = t["model_key"] in req
    ir["model"] = req.get(t["model_key"])

    bt_key = t["block_type_key"]
    btypes = t["block_types"]
    cache_key = t["cache_hint_key"]
    txt_key = t["text_key"]

    # Top-level system
    sysv = req.get(t["system_key"])
    if isinstance(sysv, str):
        if sysv == "":
            ir["empty_text_blocks"] += 1
        else:
            ir["events"].append(make_event(-1, "developer", "text", sysv, raw_role=t["system_key"]))
    elif isinstance(sysv, list):
        for b in sysv:
            if not isinstance(b, dict):
                continue
            fl = []
            if cache_key in b and b[cache_key]:
                fl.append("cache")
                ir["cache_hints_count"] += 1
            if b.get(bt_key) == btypes["text"]:
                tx = b.get(txt_key, "")
                if tx == "":
                    ir["empty_text_blocks"] += 1
                    ir["events"].append(make_event(-1, "developer", "text", "", flags=fl, empty_count=1, raw_role=t["system_key"]))
                else:
                    ir["events"].append(make_event(-1, "developer", "text", tx, flags=fl, raw_role=t["system_key"]))

    # Messages
    msgs = req.get(t["messages_key"])
    if not isinstance(msgs, list):
        msgs = []
    roles = t["roles"]
    tu_f = t["tool_use_fields"]
    tr_f = t["tool_result_fields"]
    th_f = t["thinking_fields"]
    rth_f = t["redacted_thinking_fields"]
    img_f = t["image_fields"]

    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        r = m.get(t["role_key"])
        c = m.get(t["content_key"])
        if isinstance(c, str):
            ir["text_forms"].append((i, "str"))
            blocks = [{bt_key: btypes["text"], txt_key: c}]
        elif isinstance(c, list):
            if len(c) == 1 and isinstance(c[0], dict) and c[0].get(bt_key) == btypes["text"]:
                ir["text_forms"].append((i, "list1"))
            blocks = c
        else:
            blocks = []

        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get(bt_key)
            fl = []
            if cache_key in b and b[cache_key]:
                fl.append("cache")
                ir["cache_hints_count"] += 1
            if r == roles["system"]:
                tx = b.get(txt_key, "")
                ec = 1 if tx == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(i, "developer", "text", tx, flags=fl, empty_count=ec, raw_role=r))
            elif bt == btypes["text"]:
                src = "assistant" if r == roles["assistant"] else "user"
                tx = b.get(txt_key, "")
                ec = 1 if tx == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                sigs = _extract_oob_sig_tokens(b)
                if sigs:
                    fl.append("sig")
                ir["events"].append(make_event(i, src, "text", tx, flags=fl, tokens=sigs, empty_count=ec, raw_role=r))
            elif bt == btypes["tool_use"]:
                raw_id = b.get(tu_f["id"])
                cid, id_sigs = _split_id_sig(raw_id)
                oob_sigs = _extract_oob_sig_tokens(b)
                all_sigs = id_sigs + oob_sigs
                if all_sigs:
                    fl.append("sig")
                nm = b.get(tu_f["name"]) or ""
                inp_json, afl = _canon_args(b.get(tu_f["input"]))
                fl.extend(afl)
                ir["events"].append(make_event(
                    i, "assistant", "tool_call", f"{nm}|{inp_json}",
                    ref=cid, flags=fl, tokens=all_sigs, wire_id=raw_id, name=nm, raw_role=r
                ))
            elif bt == btypes["tool_result"]:
                raw_id = b.get(tr_f["id"])
                cid, _ = _split_id_sig(raw_id)
                if b.get(tr_f["is_error"]) and "P3" not in disabled:
                    fl.append("is_error")
                elif b.get(tr_f["is_error"]) and "P3" in disabled:
                    fl.append("is_error")  # source Anthropic always has its own is_error
                rc = b.get(tr_f["content"], "")
                is_list = False
                imgs = []
                if isinstance(rc, list):
                    is_list = len(rc) > 1
                    texts = []
                    other = False
                    for p in rc:
                        if not isinstance(p, dict):
                            other = True
                            continue
                        pt = p.get(bt_key)
                        if pt == btypes["text"]:
                            tx = p.get(txt_key, "")
                            if tx == "":
                                ir["empty_text_blocks"] += 1
                            texts.append(tx)
                        elif pt == btypes["image"]:
                            isrc = p.get(img_f["source"]) or {}
                            if isrc.get(img_f["type"]) == img_f["base64"]:
                                imgs.append(_img_payload(isrc.get(img_f["media_type"]), data=isrc.get(img_f["data"], "")))
                            else:
                                imgs.append(_img_payload(url=isrc.get(img_f["url"])))
                        else:
                            other = True
                    payload = _canon_json(rc) if other else "".join(texts)
                elif isinstance(rc, str):
                    payload = rc
                else:
                    payload = _canon_json(rc)
                ir["events"].append(make_event(
                    i, f"tool:{cid}", "tool_result", payload,
                    ref=cid, flags=fl, wire_id=raw_id, raw_role=r, is_list_result=is_list
                ))
                for im in imgs:
                    ir["events"].append(make_event(i, f"tool:{cid}", "image", im, ref=cid, raw_role=r))
            elif bt == btypes["thinking"]:
                tx = b.get(th_f["text"], "")
                sig = b.get(th_f["signature"])
                sigs = [sig] if sig else _extract_oob_sig_tokens(b)
                if sigs:
                    fl.append("sig")
                ir["events"].append(make_event(i, "assistant", "thinking", tx, flags=fl, tokens=sigs, raw_role=r))
            elif bt == btypes["redacted_thinking"]:
                dat = b.get(rth_f["data"])
                toks = [dat] if dat else []
                fl.append("redacted")
                ir["events"].append(make_event(i, "assistant", "redacted_thinking", "", flags=fl, tokens=toks, raw_role=r))
            elif bt == btypes["image"]:
                src = "assistant" if r == roles["assistant"] else "user"
                isrc = b.get(img_f["source"]) or {}
                if isinstance(isrc, dict) and isrc.get(img_f["type"]) == img_f["base64"]:
                    im = _img_payload(isrc.get(img_f["media_type"]), data=isrc.get(img_f["data"], ""))
                elif isinstance(isrc, dict) and isrc.get(img_f["type"]) == img_f["url"]:
                    im = _img_payload(url=isrc.get(img_f["url"]))
                else:
                    im = _canon_json(isrc)
                ir["events"].append(make_event(i, src, "image", im, flags=fl, raw_role=r))
            elif bt == cm["ai_sdk_prompt_keys"]["reasoning_type"]:
                tx = b.get(txt_key, "")
                sigs = _extract_oob_sig_tokens(b)
                if sigs:
                    fl.append("sig")
                ir["events"].append(make_event(i, "assistant", "thinking", tx, flags=fl, tokens=sigs, raw_role=r))
            elif bt in (btypes["server_tool_use"], btypes["web_search_tool_result"], btypes["code_execution_tool_result"],
                        btypes["mcp_tool_use"], btypes["mcp_tool_result"], btypes["document"]):
                src = "assistant" if r == roles["assistant"] else "user"
                ir["events"].append(make_event(i, src, "server_tool", _canon_json(b), flags=fl, name=bt, raw_role=r))
            else:
                src = "assistant" if r == roles["assistant"] else ("developer" if r == roles["system"] else "user")
                sigs = _extract_oob_sig_tokens(b)
                if sigs:
                    fl.append("sig")
                ir["events"].append(make_event(i, src, "other", _canon_json(b), flags=fl, tokens=sigs, name=bt, raw_role=r))

    # Controls & Tools
    ctls = t["controls"]
    for ck, wk in ctls.items():
        if wk in req and req[wk] is not None:
            if ck == "metadata":
                md = req[wk]
                if isinstance(md, dict) and t["metadata_user_id_key"] in md:
                    ir["controls"]["user_id"] = str(md[t["metadata_user_id_key"]])
                ir["controls"]["metadata"] = md
            elif ck == "tool_choice":
                tc = req[wk]
                tcf = t["tool_choice_fields"]
                if isinstance(tc, dict):
                    ty = tc.get(tcf["type"])
                    if ty == tcf["modes"]["tool"]:
                        ir["controls"]["tool_choice"] = {"mode": "tool", "name": tc.get(tcf["name"])}
                    elif ty in (tcf["modes"]["auto"], tcf["modes"]["any"], tcf["modes"]["none"]):
                        ir["controls"]["tool_choice"] = {"mode": ty}
                    else:
                        ir["controls"]["tool_choice"] = {"mode": str(ty)}
                    if tc.get(tcf["disable_parallel"]) is True:
                        ir["controls"]["parallel_tool_calls"] = False
                    elif tc.get(tcf["disable_parallel"]) is False:
                        ir["controls"]["parallel_tool_calls"] = True
            else:
                ir["controls"][ck] = req[wk]

    # AI-SDK prompt format compatibility (gen4 aisdk captures)
    aisdk = cm["ai_sdk_prompt_keys"]
    if aisdk["tool_choice"] in req and "tool_choice" not in ir["controls"]:
        tc = req[aisdk["tool_choice"]]
        if isinstance(tc, dict) and tc.get("type") == "tool":
            ir["controls"]["tool_choice"] = {"mode": "tool", "name": tc.get(aisdk["tool_name"])}
        elif isinstance(tc, str):
            ir["controls"]["tool_choice"] = {"mode": "any" if tc == "required" else tc}
    if aisdk["provider_options"] in req and isinstance(req[aisdk["provider_options"]], dict):
        po = req[aisdk["provider_options"]]
        if isinstance(po.get(aisdk["openai"]), dict) and po[aisdk["openai"]].get(aisdk["parallel_tool_calls"]) is False:
            ir["controls"]["parallel_tool_calls"] = False
        if isinstance(po.get(aisdk["anthropic"]), dict) and po[aisdk["anthropic"]].get(aisdk["disable_parallel_tool_use"]) is True:
            ir["controls"]["parallel_tool_calls"] = False

    # Tools
    tf = t["tool_fields"]
    raw_tools = req.get(t["tools_key"])
    if isinstance(raw_tools, list):
        for tl in raw_tools:
            if not isinstance(tl, dict):
                continue
            if tf["cache_control"] in tl and tl[tf["cache_control"]]:
                ir["cache_hints_count"] += 1
            nm = tl.get(tf["name"])
            if nm:
                st_exp = tl.get(tf["strict"])
                ir["tools"][nm] = {
                    "schema": tl.get(tf["schema"]),
                    "strict": bool(st_exp) if st_exp is not None else t["strict_default"],
                    "strict_explicit": st_exp,
                    "description": tl.get(tf["description"]),
                    "server_type": tl.get(tf["type"]),
                }
            elif tl.get(tf["type"]):
                # Server tool declaration (e.g. web_search_20250305, bash_20250124)
                st_type = tl.get(tf["type"])
                ir["tools"][f"__server_tool__:{st_type}"] = {
                    "schema": None,
                    "strict": False,
                    "strict_explicit": None,
                    "description": None,
                    "server_type": st_type,
                }
    elif isinstance(raw_tools, dict):
        # AI-SDK tools dict format
        for nm, tl in raw_tools.items():
            if isinstance(tl, dict):
                ir["tools"][nm] = {
                    "schema": tl.get(aisdk["input_schema"]) or tl.get(tf["schema"]),
                    "strict": False,
                    "strict_explicit": None,
                    "description": tl.get(tf["description"]),
                    "server_type": None,
                }
    return ir


def _parse_openai_chat_request(req, disabled):
    t = C.table("openai_chat", "request")
    ir = _blank_ir("openai_chat", "request")
    if not isinstance(req, dict):
        return ir
    ir["has_model_key"] = t["model_key"] in req
    ir["model"] = req.get(t["model_key"])
    allowed_env = set(t["envelope_allowed_keys"])
    for ek in sorted(k for k in req if k not in allowed_env):
        ir["envelope"]["extra_keys"].append(ek)
    cc_pts = req.get(t["cache_control_injection_points_key"])
    if isinstance(cc_pts, list):
        ir["cache_hints_count"] += len(cc_pts)
    if req.get(t["web_search_options_key"]):
        st_type = t["web_search_server_type"]
        ir["tools"][f"__server_tool__:{st_type}"] = {
            "schema": None, "strict": False, "strict_explicit": None, "description": None, "server_type": st_type,
        }

    roles = t["roles"]
    pt_key = t["part_type_key"]
    ptypes = t["part_types"]
    txt_key = t["text_key"]
    cache_key = t["cache_hint_key"]
    tc_f = t["tool_call_fields"]
    rk = t["assistant_reasoning_keys"]
    tbf = t["thinking_block_fields"]

    msgs = req.get(t["messages_key"])
    if not isinstance(msgs, list):
        msgs = []

    msg_allowed = set(t["message_allowed_keys"])
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        r = m.get(t["role_key"])
        c = m.get(t["content_key"])
        for mk in sorted(k for k in m if k not in msg_allowed):
            ir["envelope"]["extra_keys"].append(f"messages[{i}].{mk}")
        for nk in t["nullable_optional_message_keys"]:
            if nk in m and m[nk] is None:
                ir["nullable_forms"].append((f"messages[{i}].{nk}", "null"))
            elif nk not in m:
                ir["nullable_forms"].append((f"messages[{i}].{nk}", "absent"))

        if r in (roles["system"], roles["developer"]):
            if isinstance(c, str):
                if c == "":
                    ir["empty_text_blocks"] += 1
                    ir["events"].append(make_event(i, "developer", "text", "", empty_count=1, raw_role=r))
                else:
                    ir["events"].append(make_event(i, "developer", "text", c, raw_role=r))
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and cache_key in p and p[cache_key]:
                        ir["cache_hints_count"] += 1
                    if isinstance(p, dict) and p.get(pt_key) == ptypes["text"]:
                        tx = p.get(txt_key, "")
                        ec = 1 if tx == "" else 0
                        if ec:
                            ir["empty_text_blocks"] += 1
                        ir["events"].append(make_event(i, "developer", "text", tx, empty_count=ec, raw_role=r))
        elif r == roles["user"]:
            if isinstance(c, str):
                ir["text_forms"].append((i, "str"))
                if c == "":
                    ir["empty_text_blocks"] += 1
                    ir["events"].append(make_event(i, "user", "text", "", empty_count=1, raw_role=r))
                else:
                    ir["events"].append(make_event(i, "user", "text", c, raw_role=r))
            elif isinstance(c, list):
                if len(c) == 1 and isinstance(c[0], dict) and c[0].get(pt_key) == ptypes["text"]:
                    ir["text_forms"].append((i, "list1"))
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get(pt_key)
                    fl = []
                    if cache_key in p and p[cache_key]:
                        fl.append("cache")
                        ir["cache_hints_count"] += 1
                    if pt == ptypes["text"]:
                        tx = p.get(txt_key, "")
                        ec = 1 if tx == "" else 0
                        if ec:
                            ir["empty_text_blocks"] += 1
                        ir["events"].append(make_event(i, "user", "text", tx, flags=fl, empty_count=ec, raw_role=r))
                    elif pt == ptypes["image_url"]:
                        im = _img_from_data_or_url(p.get(t["image_url_key"]))
                        ir["events"].append(make_event(i, "user", "image", im, flags=fl, raw_role=r))
                    else:
                        ir["events"].append(make_event(i, "user", "other", _canon_json(p), flags=fl, name=pt, raw_role=r))
        elif r == roles["assistant"]:
            # Reasoning / thinking carriers
            tbs = m.get(rk["thinking_blocks"]) or []
            rc_str = m.get(rk["reasoning_content"])
            if rc_str == "":
                ir["text_forms"].append((i, "empty_reasoning_str"))
            tb_texts = "".join(tb.get(tbf["thinking"], "") for tb in tbs if isinstance(tb, dict) and tb.get(tbf["type"]) == tbf["thinking"])
            if isinstance(rc_str, str) and rc_str and rc_str != tb_texts:
                ir["events"].append(make_event(i, "assistant", "thinking", rc_str, raw_role=r))
            for tb in tbs:
                if not isinstance(tb, dict):
                    continue
                if tb.get(tbf["type"]) == tbf["redacted_type"]:
                    dat = tb.get(tbf["data"])
                    ir["events"].append(make_event(i, "assistant", "redacted_thinking", "", flags=["redacted"], tokens=[dat] if dat else [], raw_role=r))
                elif tb.get(tbf["type"]) == tbf["thinking"]:
                    sig = tb.get(tbf["signature"])
                    ir["events"].append(make_event(
                        i, "assistant", "thinking", tb.get(tbf["thinking"], ""),
                        flags=["sig"] if sig else [], tokens=[sig] if sig else [], raw_role=r
                    ))
                else:
                    ir["wire_findings"].append(("S04", "T1", f"invalid thinking_blocks[].type: {tb.get(tbf['type'])!r}"))
            th_obj = m.get(rk["thinking"])
            if isinstance(th_obj, dict) and (th_obj.get(tbf["content"]) or th_obj.get(tbf["signature"])):
                sig = th_obj.get(tbf["signature"])
                ir["events"].append(make_event(
                    i, "assistant", "thinking", th_obj.get(tbf["content"]) or "",
                    flags=["sig"] if sig else [], tokens=[sig] if sig else [], raw_role=r
                ))
            rd = m.get(rk["reasoning_details"])
            if isinstance(rd, list) and rd:
                txt = "".join(str(x.get(tbf["text"]) or x.get(tbf["summary"]) or "") for x in rd if isinstance(x, dict))
                enc_toks = [x.get(tbf["data"]) for x in rd if isinstance(x, dict) and (x.get(tbf["type"]) == tbf["encrypted_type"] or x.get(tbf["data"]))]
                if (txt or enc_toks) and txt != (rc_str or ""):
                    ir["events"].append(make_event(
                        i, "assistant", "thinking", txt,
                        flags=["sig"] if enc_toks else [], tokens=enc_toks, raw_role=r
                    ))
            elif isinstance(m.get(rk["reasoning"]), str) and m[rk["reasoning"]] and not rc_str:
                ir["events"].append(make_event(i, "assistant", "thinking", m[rk["reasoning"]], raw_role=r))

            # Assistant content vs tool_calls
            tcs = m.get(t["assistant_tool_calls_key"]) or []
            if isinstance(c, str):
                if c == "":
                    if tcs:
                        ir["text_forms"].append((i, "empty_str_beside_tool_call"))
                    else:
                        ir["empty_text_blocks"] += 1
                        ir["events"].append(make_event(i, "assistant", "text", "", empty_count=1, raw_role=r))
                else:
                    ir["events"].append(make_event(i, "assistant", "text", c, raw_role=r))
            elif isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and cache_key in p and p[cache_key]:
                        ir["cache_hints_count"] += 1
                    if isinstance(p, dict) and p.get(pt_key) == ptypes["text"]:
                        tx = p.get(txt_key, "")
                        ec = 1 if tx == "" else 0
                        if ec:
                            ir["empty_text_blocks"] += 1
                        ir["events"].append(make_event(i, "assistant", "text", tx, empty_count=ec, raw_role=r))
            elif c is None and tcs:
                ir["text_forms"].append((i, "null_beside_tool_call"))

            fn_allowed = set(t["tool_call_function_allowed_keys"])
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get(tc_f["function"]) or {}
                if isinstance(fn, dict):
                    for fk in sorted(k for k in fn if k not in fn_allowed):
                        ir["envelope"]["extra_keys"].append(f"tool_calls.function.{fk}")
                nm = fn.get(tc_f["name"]) or ""
                args_str, afl = _canon_args(fn.get(tc_f["arguments"]))
                raw_id = tc.get(tc_f["id"])
                cid, id_sigs = _split_id_sig(raw_id)
                oob_sigs = _extract_oob_sig_tokens(tc)
                all_sigs = id_sigs + oob_sigs
                fl = list(afl) + (["sig"] if all_sigs else [])
                ir["events"].append(make_event(
                    i, "assistant", "tool_call", f"{nm}|{args_str}",
                    ref=cid, flags=fl, tokens=all_sigs, wire_id=raw_id, name=nm, raw_role=r
                ))
        elif r in (roles["tool"], roles["function"]):
            raw_id = m.get(t["tool_result_id_key"]) or m.get(t["tool_result_name_key"])
            cid, _ = _split_id_sig(raw_id)
            imgs = []
            is_list = False
            if isinstance(c, list):
                is_list = len(c) > 1
                texts = []
                other = False
                for p in c:
                    if not isinstance(p, dict):
                        other = True
                        continue
                    pt = p.get(pt_key)
                    if pt == ptypes["text"]:
                        texts.append(p.get(txt_key, ""))
                    elif pt == ptypes["image_url"]:
                        imgs.append(_img_from_data_or_url(p.get(t["image_url_key"])))
                    else:
                        other = True
                payload = _canon_json(c) if other else "".join(texts)
            elif isinstance(c, str):
                payload = c
            elif c is None:
                payload = ""
            else:
                payload = _canon_json(c)
            ir["events"].append(make_event(
                i, f"tool:{cid}", "tool_result", payload,
                ref=cid, wire_id=raw_id, raw_role=r, is_list_result=is_list
            ))
            for im in imgs:
                ir["events"].append(make_event(i, f"tool:{cid}", "image", im, ref=cid, raw_role=r))
        else:
            ir["events"].append(make_event(i, "other", "other", _canon_json(m), raw_role=str(r)))

    # Controls
    ctls = t["controls"]
    if ctls["temperature"] in req and req[ctls["temperature"]] is not None:
        ir["controls"]["temperature"] = req[ctls["temperature"]]
    if ctls["top_p"] in req and req[ctls["top_p"]] is not None:
        ir["controls"]["top_p"] = req[ctls["top_p"]]
    if ctls["top_k"] in req and req[ctls["top_k"]] is not None:
        ir["controls"]["top_k"] = req[ctls["top_k"]]
    if ctls["seed"] in req and req[ctls["seed"]] is not None:
        ir["controls"]["seed"] = req[ctls["seed"]]
    if ctls["stop"] in req and req[ctls["stop"]] is not None:
        st = req[ctls["stop"]]
        ir["controls"]["stop"] = [st] if isinstance(st, str) else st
        ir["controls"]["stop_form"] = "str" if isinstance(st, str) else "list"
    if ctls["max_completion_tokens"] in req and req[ctls["max_completion_tokens"]] is not None:
        ir["controls"]["max_tokens"] = req[ctls["max_completion_tokens"]]
    elif ctls["max_tokens"] in req and req[ctls["max_tokens"]] is not None:
        ir["controls"]["max_tokens"] = req[ctls["max_tokens"]]
    if ctls["parallel_tool_calls"] in req and req[ctls["parallel_tool_calls"]] is not None:
        ir["controls"]["parallel_tool_calls"] = req[ctls["parallel_tool_calls"]]
    if ctls["user"] in req and req[ctls["user"]] is not None:
        ir["controls"]["user_id"] = str(req[ctls["user"]])
    if ctls["store"] in req and req[ctls["store"]] is not None:
        ir["controls"]["store"] = req[ctls["store"]]
    if ctls["logprobs"] in req and req[ctls["logprobs"]] is not None:
        ir["controls"]["logprobs"] = req[ctls["logprobs"]]
    if ctls["top_logprobs"] in req and req[ctls["top_logprobs"]] is not None:
        ir["controls"]["top_logprobs"] = req[ctls["top_logprobs"]]
    if ctls["service_tier"] in req and req[ctls["service_tier"]] is not None:
        ir["controls"]["service_tier"] = req[ctls["service_tier"]]
    if ctls["presence_penalty"] in req and req[ctls["presence_penalty"]] is not None:
        ir["controls"]["presence_penalty"] = req[ctls["presence_penalty"]]
    if ctls["frequency_penalty"] in req and req[ctls["frequency_penalty"]] is not None:
        ir["controls"]["frequency_penalty"] = req[ctls["frequency_penalty"]]
    if ctls["logit_bias"] in req and req[ctls["logit_bias"]] is not None:
        ir["controls"]["logit_bias"] = req[ctls["logit_bias"]]
    if ctls["n"] in req and req[ctls["n"]] not in (None, 1):
        ir["controls"]["n"] = req[ctls["n"]]
    if ctls["reasoning_effort"] in req and req[ctls["reasoning_effort"]] is not None:
        if ctls["thinking"] in req and req[ctls["thinking"]] is not None:
            ir["controls"]["thinking"] = {"thinking": req[ctls["thinking"]], "effort": req[ctls["reasoning_effort"]]}
        else:
            ir["controls"]["thinking"] = {"effort": req[ctls["reasoning_effort"]]}
    elif ctls["thinking"] in req and req[ctls["thinking"]] is not None:
        ir["controls"]["thinking"] = req[ctls["thinking"]]

    # tool_choice
    if ctls["tool_choice"] in req and req[ctls["tool_choice"]] is not None:
        tc = req[ctls["tool_choice"]]
        tcf = t["tool_choice_fields"]
        if isinstance(tc, str):
            ir["controls"]["tool_choice"] = {"mode": "any" if tc == tcf["modes"]["required"] else tc}
        elif isinstance(tc, dict):
            ty = tc.get(tcf["type"])
            if ty == tcf["modes"]["function"] and isinstance(tc.get(tcf["function"]), dict):
                ir["controls"]["tool_choice"] = {"mode": "tool", "name": tc[tcf["function"]].get(tcf["name"])}
            else:
                ir["controls"]["tool_choice"] = {"mode": str(ty)}

    # response_format
    if ctls["response_format"] in req and isinstance(req[ctls["response_format"]], dict):
        rf = req[ctls["response_format"]]
        rff = t["response_format_fields"]
        rft = rf.get(rff["type"])
        if rft and rft != rff["modes"]["text"]:
            ir["controls"]["response_format"] = {"mode": rff["modes"].get(rft, rft)}

    # Tools
    tf = t["tool_fields"]
    for t_idx, tl in enumerate(req.get(t["tools_key"]) or []):
        if not isinstance(tl, dict):
            continue
        for tk in sorted(tl.keys()):
            if tk not in (tf["type"], tf["function"], cache_key):
                ir["nullable_forms"].append((f"tools[{t_idx}].{tk}", "null" if tl[tk] is None else "present"))
        if tl.get(tf["type"]) == tf["function_val"] and isinstance(tl.get(tf["function"]), dict):
            fn = tl[tf["function"]]
            nm = fn.get(tf["name"])
            if nm:
                st_exp = fn.get(tf["strict"])
                ir["tools"][nm] = {
                    "schema": fn.get(tf["schema"]),
                    "strict": bool(st_exp) if st_exp is not None else t["strict_default"],
                    "strict_explicit": st_exp,
                    "description": fn.get(tf["description"]),
                    "server_type": None,
                }
    return ir


def _parse_openai_responses_request(req, disabled):
    if "P3" in disabled:
        # P-V100-15 (-P3): without native Responses dialect & error carrier table, falls back to openai_chat
        return _parse_openai_chat_request(req, disabled)

    t = C.table("openai_responses", "request")
    ir = _blank_ir("openai_responses", "request")
    if not isinstance(req, dict):
        return ir
    ir["has_model_key"] = t["model_key"] in req
    ir["model"] = req.get(t["model_key"])

    # Top-level instructions
    inst = req.get(t["instructions_key"])
    if isinstance(inst, str):
        if inst == "":
            ir["empty_text_blocks"] += 1
        else:
            ir["events"].append(make_event(-1, "developer", "text", inst, raw_role=t["instructions_key"]))

    roles = t["roles"]
    itypes = t["item_types"]
    ptypes = t["part_types"]
    it_key = t["item_type_key"]
    r_key = t["role_key"]
    c_key = t["content_key"]
    txt_key = t["text_key"]
    fc_f = t["function_call_fields"]
    fco_f = t["function_call_output_fields"]
    rs_f = t["reasoning_fields"]

    inp = req.get(t["input_key"])
    if isinstance(inp, str):
        ir["events"].append(make_event(0, "user", "text", inp, raw_role=roles["user"]))
        inp = []
    elif not isinstance(inp, list):
        inp = []

    # Group items into logical turns while preserving exact item sequence:
    # Consecutive items belonging to the same speaker turn (e.g., assistant reasoning + message + function_call)
    # share a turn_index when contiguous, while role switches advance turn_index.
    turn_idx = 0
    prev_coarse = None

    for idx, it in enumerate(inp):
        if not isinstance(it, dict):
            continue
        itype = it.get(it_key)
        irole = it.get(r_key)

        if itype == itypes["message"] or (itype is None and irole is not None):
            if irole in (roles["system"], roles["developer"]):
                coarse = "developer"
            elif irole == roles["assistant"]:
                coarse = "assistant"
            else:
                coarse = "user"
            if prev_coarse is not None and coarse != prev_coarse:
                turn_idx += 1
            prev_coarse = coarse

            c = it.get(c_key)
            if isinstance(c, str):
                ec = 1 if c == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(turn_idx, coarse, "text", c, empty_count=ec, wire_id=it.get(t["id_key"]), raw_role=irole))
            elif isinstance(c, list):
                for p in c:
                    if not isinstance(p, dict):
                        continue
                    pt = p.get(it_key)
                    if pt in (ptypes["input_text"], ptypes["output_text"], ptypes["text"]):
                        tx = p.get(txt_key, "")
                        ec = 1 if tx == "" else 0
                        if ec:
                            ir["empty_text_blocks"] += 1
                        ir["events"].append(make_event(turn_idx, coarse, "text", tx, empty_count=ec, wire_id=it.get(t["id_key"]), raw_role=irole))
                    elif pt == ptypes["input_image"]:
                        im = _img_from_data_or_url(p.get(t["image_url_key"]))
                        ir["events"].append(make_event(turn_idx, coarse, "image", im, wire_id=it.get(t["id_key"]), raw_role=irole))
                    elif pt == ptypes["refusal"]:
                        rf = p.get(t["refusal_key"], "")
                        ir["events"].append(make_event(turn_idx, coarse, "refusal", rf, wire_id=it.get(t["id_key"]), raw_role=irole))
                    else:
                        ir["events"].append(make_event(turn_idx, coarse, "other", _canon_json(p), name=pt, raw_role=irole))
        elif itype == itypes["function_call"]:
            coarse = "assistant"
            if prev_coarse is not None and coarse != prev_coarse:
                turn_idx += 1
            prev_coarse = coarse

            raw_id = it.get(fc_f["call_id"]) or it.get(fc_f["id"])
            cid, id_sigs = _split_id_sig(raw_id)
            oob_sigs = _extract_oob_sig_tokens(it)
            all_sigs = id_sigs + oob_sigs
            nm = it.get(fc_f["name"]) or ""
            args_str, afl = _canon_args(it.get(fc_f["arguments"]))
            fl = list(afl) + (["sig"] if all_sigs else [])
            ir["events"].append(make_event(
                turn_idx, "assistant", "tool_call", f"{nm}|{args_str}",
                ref=cid, flags=fl, tokens=all_sigs, wire_id=raw_id, name=nm, raw_role="assistant"
            ))
        elif itype == itypes["function_call_output"]:
            coarse = "tool"
            if prev_coarse is not None and coarse != prev_coarse:
                turn_idx += 1
            prev_coarse = coarse

            raw_id = it.get(fco_f["call_id"]) or it.get(fco_f["id"])
            cid, _ = _split_id_sig(raw_id)
            fl = []
            # Error carrier from carriers.json: status == "incomplete" or incomplete_details truthy
            if it.get(fco_f["status"]) == fco_f["error_status_val"] or it.get(fco_f["incomplete_details"]):
                fl.append("is_error")

            out_val = it.get(fco_f["output"], "")
            is_list = False
            imgs = []
            if isinstance(out_val, list):
                is_list = len(out_val) > 1
                texts = []
                other = False
                for p in out_val:
                    if not isinstance(p, dict):
                        other = True
                        continue
                    pt = p.get(it_key)
                    if pt in (ptypes["input_text"], ptypes["output_text"], ptypes["text"]):
                        texts.append(p.get(txt_key, ""))
                    elif pt == ptypes["input_image"]:
                        imgs.append(_img_from_data_or_url(p.get(t["image_url_key"])))
                    else:
                        other = True
                payload = _canon_json(out_val) if other else "".join(texts)
            elif isinstance(out_val, str):
                payload = out_val
            elif out_val is None:
                payload = ""
            else:
                payload = _canon_json(out_val)

            ir["events"].append(make_event(
                turn_idx, f"tool:{cid}", "tool_result", payload,
                ref=cid, flags=fl, wire_id=raw_id, raw_role="tool", is_list_result=is_list
            ))
            for im in imgs:
                ir["events"].append(make_event(turn_idx, f"tool:{cid}", "image", im, ref=cid, raw_role="tool"))
        elif itype == itypes["reasoning"]:
            coarse = "assistant"
            if prev_coarse is not None and coarse != prev_coarse:
                turn_idx += 1
            prev_coarse = coarse

            sums = it.get(rs_f["summary"]) or []
            sum_txt = "".join(
                p.get(rs_f["text"], "") for p in sums
                if isinstance(p, dict) and p.get(it_key) in (rs_f["summary_text_type"], ptypes["text"])
            )
            enc = it.get(rs_f["encrypted_content"])
            toks = [enc] if enc else []
            if sum_txt:
                ir["events"].append(make_event(
                    turn_idx, "assistant", "thinking", sum_txt,
                    flags=["sig"] if toks else [], tokens=toks, wire_id=it.get(rs_f["id"]), raw_role="assistant"
                ))
            elif enc:
                ir["events"].append(make_event(
                    turn_idx, "assistant", "redacted_thinking", "",
                    flags=["redacted"], tokens=toks, wire_id=it.get(rs_f["id"]), raw_role="assistant"
                ))
            # Note: an empty reasoning item (summary == [] and no encrypted_content) carries no content event
        else:
            ir["events"].append(make_event(turn_idx, "assistant", "server_tool", _canon_json(it), name=itype, raw_role="assistant"))

    # Controls
    ctls = t["controls"]
    if ctls["temperature"] in req and req[ctls["temperature"]] is not None:
        ir["controls"]["temperature"] = req[ctls["temperature"]]
    if ctls["top_p"] in req and req[ctls["top_p"]] is not None:
        ir["controls"]["top_p"] = req[ctls["top_p"]]
    if ctls["max_output_tokens"] in req and req[ctls["max_output_tokens"]] is not None:
        ir["controls"]["max_tokens"] = req[ctls["max_output_tokens"]]
    if ctls["parallel_tool_calls"] in req and req[ctls["parallel_tool_calls"]] is not None:
        ir["controls"]["parallel_tool_calls"] = req[ctls["parallel_tool_calls"]]
    if ctls["user"] in req and req[ctls["user"]] is not None:
        ir["controls"]["user_id"] = str(req[ctls["user"]])
    if ctls["store"] in req and req[ctls["store"]] is not None:
        ir["controls"]["store"] = req[ctls["store"]]
    if ctls["service_tier"] in req and req[ctls["service_tier"]] is not None:
        ir["controls"]["service_tier"] = req[ctls["service_tier"]]
    if ctls["reasoning"] in req and isinstance(req[ctls["reasoning"]], dict) and req[ctls["reasoning"]]:
        ir["controls"]["thinking"] = req[ctls["reasoning"]]

    if ctls["tool_choice"] in req and req[ctls["tool_choice"]] is not None:
        tc = req[ctls["tool_choice"]]
        tcf = t["tool_choice_fields"]
        if isinstance(tc, str):
            ir["controls"]["tool_choice"] = {"mode": "any" if tc == tcf["modes"]["required"] else tc}
        elif isinstance(tc, dict):
            ty = tc.get(tcf["type"])
            if ty == tcf["modes"]["function"]:
                ir["controls"]["tool_choice"] = {"mode": "tool", "name": tc.get(tcf["name"])}
            else:
                ir["controls"]["tool_choice"] = {"mode": str(ty)}

    if ctls["text"] in req and isinstance(req[ctls["text"]], dict):
        tff = t["text_format_fields"]
        fmt = req[ctls["text"]].get(tff["format"])
        if isinstance(fmt, dict):
            ft = fmt.get(tff["type"])
            if ft and ft != tff["modes"]["text"]:
                ir["controls"]["response_format"] = {"mode": tff["modes"].get(ft, ft)}

    # Tools
    tf = t["tool_fields"]
    raw_tools = req.get(t["tools_key"])
    if isinstance(raw_tools, dict):
        raw_tools = [raw_tools]
    for tl in (raw_tools or []):
        if not isinstance(tl, dict):
            continue
        if tl.get(tf["type"]) == tf["function_val"]:
            nm = tl.get(tf["name"])
            if nm:
                st_exp = tl.get(tf["strict"])
                ir["tools"][nm] = {
                    "schema": tl.get(tf["schema"]),
                    "strict": bool(st_exp) if st_exp is not None else t["strict_default"],
                    "strict_explicit": st_exp,
                    "description": tl.get(tf["description"]),
                    "server_type": None,
                }
        elif tl.get(tf["type"]):
            st_type = tl.get(tf["type"])
            ir["tools"][f"__server_tool__:{st_type}"] = {
                "schema": None,
                "strict": False,
                "strict_explicit": None,
                "description": None,
                "server_type": st_type,
            }
    return ir


def _parse_gemini_request(req, disabled):
    t = C.table("gemini_generatecontent", "request")
    ir = _blank_ir("gemini_generatecontent", "request")
    if not isinstance(req, dict):
        return ir
    ir["has_model_key"] = t["model_key"] in req
    ir["model"] = req.get(t["model_key"])

    pk = t["part_keys"]
    roles = t["roles"]
    fc_f = t["function_call_fields"]
    fr_f = t["function_response_fields"]
    med_f = t["media_fields"]

    # System instruction
    si = C.first_present(req, t["system_keys"])
    if isinstance(si, dict):
        if (si.get(t["parts_key"]) or []) and si.get(t["role_key"]) in t["system_disallowed_roles"]:
            ir["wire_findings"].append(("S02", "T1", f"disallowed role {si.get(t['role_key'])!r} on systemInstruction"))
        for p in (si.get(t["parts_key"]) or []):
            if isinstance(p, dict) and pk["text"] in p:
                tx = p[pk["text"]]
                ec = 1 if tx == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(-1, "developer", "text", tx, empty_count=ec, raw_role=t["system_keys"][0]))

    call_refs = {}
    seen_results = {}
    contents = req.get(t["contents_key"])
    if not isinstance(contents, list):
        contents = []

    for i, m in enumerate(contents):
        if not isinstance(m, dict):
            continue
        r = m.get(t["role_key"])
        parts = m.get(t["parts_key"])
        if not isinstance(parts, list):
            parts = []
        for p in parts:
            if not isinstance(p, dict):
                continue
            sig = C.first_present(p, pk["thought_sigs"])
            toks = [sig] if sig else []
            fl = ["sig"] if toks else []

            if pk["text"] in p:
                tx = p[pk["text"]]
                kind = "thinking" if p.get(pk["thought"]) else "text"
                src = "assistant" if r == roles["model"] else "user"
                ec = 1 if (kind == "text" and tx == "") else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(i, src, kind, tx, flags=fl, tokens=toks, empty_count=ec, raw_role=r))
            elif C.has_any_key(p, pk["function_calls"]):
                fc = C.first_present(p, pk["function_calls"]) or {}
                nm = fc.get(fc_f["name"]) or ""
                raw_id = fc.get(fc_f["id"])
                if raw_id:
                    ref = raw_id
                else:
                    occ = len(call_refs.get(nm, []))
                    ref = f"{nm}@{occ}"
                call_refs.setdefault(nm, []).append(ref)
                args_str, afl = _canon_args(fc.get(fc_f["args"]))
                ir["events"].append(make_event(
                    i, "assistant", "tool_call", f"{nm}|{args_str}",
                    ref=ref, flags=fl + afl, tokens=toks, wire_id=raw_id, name=nm, raw_role=r
                ))
            elif C.has_any_key(p, pk["function_responses"]):
                fr = C.first_present(p, pk["function_responses"]) or {}
                nm = fr.get(fr_f["name"]) or ""
                raw_id = fr.get(fr_f["id"])
                if raw_id:
                    cid = raw_id
                else:
                    n = seen_results.get(nm, 0)
                    seen_results[nm] = n + 1
                    refs = call_refs.get(nm, [])
                    cid = refs[n] if n < len(refs) else f"{nm}@{n}"
                resp = fr.get(fr_f["response"])
                rfl = []
                if isinstance(resp, dict) and fr_f["error_key"] in resp and "P3" not in disabled:
                    rfl.append("is_error")
                    rest = {k: v for k, v in resp.items() if k not in (fr_f["error_key"], fr_f["name"])}
                    if not rest:
                        resp = {fr_f["wrapper_keys"][0]: resp[fr_f["error_key"]]}
                    else:
                        resp = rest
                wkey = next(
                    (k for k in fr_f["wrapper_keys"] if isinstance(resp, dict) and k in resp and set(resp) <= {k, fr_f["name"]}),
                    None
                )
                if wkey and isinstance(resp[wkey], str):
                    payload = resp[wkey]
                elif wkey:
                    payload = _canon_json(resp[wkey])
                else:
                    payload = _canon_json(resp)
                ir["events"].append(make_event(
                    i, f"tool:{cid}", "tool_result", payload,
                    ref=cid, flags=rfl, wire_id=raw_id, name=nm, raw_role=r
                ))
                for q in (fr.get(fr_f["parts"]) or []):
                    if isinstance(q, dict) and (C.has_any_key(q, pk["inline_datas"]) or C.has_any_key(q, pk["file_datas"])):
                        im = _parse_gemini_media(q, pk, med_f)
                        ir["events"].append(make_event(i, f"tool:{cid}", "image", im, ref=cid, raw_role=r))
            elif C.has_any_key(p, pk["inline_datas"]) or C.has_any_key(p, pk["file_datas"]):
                src = "assistant" if r == roles["model"] else "user"
                im = _parse_gemini_media(p, pk, med_f)
                ir["events"].append(make_event(i, src, "image", im, flags=fl, tokens=toks, raw_role=r))
            else:
                src = "assistant" if r == roles["model"] else "user"
                ir["events"].append(make_event(i, src, "other", _canon_json(p), flags=fl, tokens=toks, raw_role=r))

    # Controls
    gc = C.first_present(req, t["gen_config_keys"])
    if isinstance(gc, dict):
        ctls = t["controls"]
        for ck in ("temperature", "top_p", "top_k", "seed", "stop", "max_tokens"):
            val = C.first_present(gc, ctls[ck])
            if val is not None:
                ir["controls"][ck] = val
        mt = C.first_present(gc, ctls["response_mime_type"])
        if mt and mt != "text/plain":
            rs = C.first_present(gc, ctls["response_schema"])
            if mt == "application/json":
                ir["controls"]["response_format"] = {"mode": "json_schema" if rs else "json"}
            else:
                ir["controls"]["response_format"] = {"mode": mt}
        thc = C.first_present(gc, ctls["thinking_config"])
        if thc is not None:
            ir["controls"]["thinking"] = thc
        cc_val = C.first_present(gc, ctls["candidate_count"])
        if cc_val is not None and cc_val != 1:
            ir["controls"]["candidate_count"] = cc_val

    tc_raw = C.first_present(req, t["tool_config_keys"])
    if isinstance(tc_raw, dict):
        tc_allowed = set(t["tool_config_allowed_keys"])
        for tck in sorted(k for k in tc_raw if k not in tc_allowed):
            ir["envelope"]["extra_keys"].append(f"{t['tool_config_keys'][0]}.{tck}")
        fcc = C.first_present(tc_raw, t["func_calling_config_keys"])
        if isinstance(fcc, dict):
            tcf = t["tool_choice_fields"]
            mode_raw = fcc.get(tcf["mode"])
            allowed = C.first_present(fcc, tcf["allowed_names"])
            mode_norm = tcf["modes"].get(mode_raw, str(mode_raw).lower() if mode_raw else None)
            if mode_norm == "any" and isinstance(allowed, list) and len(allowed) == 1:
                ir["controls"]["tool_choice"] = {"mode": "tool", "name": allowed[0], "allowed": allowed}
            elif mode_norm:
                ir["controls"]["tool_choice"] = {"mode": mode_norm, "allowed": allowed}
            elif allowed is not None:
                ir["controls"]["tool_choice"] = {"mode": "unspecified", "allowed": allowed}

    # Tools
    tf = t["tool_fields"]
    raw_tls = req.get(t["tools_key"]) or []
    if isinstance(raw_tls, dict):
        raw_tls = [raw_tls]
    for tl in raw_tls:
        if not isinstance(tl, dict):
            continue
        decls = C.first_present(tl, t["func_decls_keys"]) or []
        for fd in decls:
            if not isinstance(fd, dict):
                continue
            nm = fd.get(tf["name"])
            if nm:
                sch = C.first_present(fd, tf["schemas"])
                ir["tools"][nm] = {
                    "schema": sch,
                    "strict": False,
                    "strict_explicit": None,
                    "description": fd.get(tf["description"]),
                    "server_type": None,
                }
    return ir


def _parse_gemini_media(p, pk, med_f):
    inl = C.first_present(p, pk["inline_datas"])
    if isinstance(inl, dict):
        return _img_payload(C.first_present(inl, med_f["mime_types"]), data=inl.get(med_f["data"], ""))
    fd = C.first_present(p, pk["file_datas"])
    if isinstance(fd, dict):
        return _img_payload(url=C.first_present(fd, med_f["file_uris"]))
    return _canon_json(p)


# ----------------------------------------------------------------------
# RESPONSE PARSERS
# ----------------------------------------------------------------------

def _parse_anthropic_response(msg, disabled):
    t = C.table("anthropic_messages", "response")
    tr = C.table("anthropic_messages", "request")
    ir = _blank_ir("anthropic_messages", "response")
    if not isinstance(msg, dict):
        return ir

    ir["has_model_key"] = t["model_key"] in msg
    ir["has_id_key"] = t["id_key"] in msg
    ir["model"] = msg.get(t["model_key"])
    ir["stop_seq"] = msg.get(t["stop_seq_key"])
    if t["role_key"] not in msg or msg.get(t["role_key"]) != t["role_val"]:
        ir["wire_findings"].append(("S02", "T1", f"response role invalid or missing: {msg.get(t['role_key'])!r}"))
    if t["type_key"] not in msg or msg.get(t["type_key"]) != t["type_val"]:
        ir["wire_findings"].append(("S04", "T1", f"response envelope type invalid or missing: {msg.get(t['type_key'])!r}"))

    allowed_env = set(t["envelope_allowed_keys"])
    foreign_env = set(t["foreign_envelope_keys"])
    extra = sorted(k for k in msg if k not in allowed_env)
    foreign = sorted(k for k in msg if k in foreign_env)
    missing_null = sorted(k for k in t["nullable_required_keys"] if k not in msg)
    ir["envelope"] = {
        "id": msg.get(t["id_key"]),
        "type": msg.get(t["type_key"]),
        "role": msg.get(t["role_key"]),
        "extra_keys": extra,
        "foreign_keys": foreign,
        "missing_nullable_keys": missing_null,
    }

    bt_key = tr["block_type_key"]
    btypes = tr["block_types"]
    txt_key = tr["text_key"]
    tu_f = tr["tool_use_fields"]
    th_f = tr["thinking_fields"]
    rth_f = tr["redacted_thinking_fields"]

    for idx, b in enumerate(msg.get(t["content_key"]) or []):
        if not isinstance(b, dict):
            continue
        bt = b.get(bt_key)
        oob_sigs = _extract_oob_sig_tokens(b)
        if bt == btypes["text"]:
            tx = b.get(txt_key)
            if isinstance(tx, str):
                ec = 1 if tx == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(
                    0, "assistant", "text", tx,
                    flags=["sig"] if oob_sigs else [], tokens=oob_sigs, empty_count=ec
                ))
            else:
                ir["events"].append(make_event(
                    0, "assistant", "text", _canon_json(tx),
                    flags=["malformed:text-not-string"]
                ))
        elif bt == btypes["tool_use"]:
            raw_id = b.get(tu_f["id"])
            cid, id_sigs = _split_id_sig(raw_id)
            all_sigs = id_sigs + oob_sigs
            nm = b.get(tu_f["name"]) or ""
            inp_raw = b.get(tu_f["input"], {})
            args_str, afl = _canon_args(inp_raw)
            fl = list(afl)
            if isinstance(inp_raw, str):
                fl.append("malformed:input-is-string")
            if not isinstance(b.get(tu_f["name"]), str) or not nm:
                fl.append("malformed:no-name")
            if tr["tool_result_fields"]["is_error"] in b:
                fl.append("misplaced_is_error")
            if all_sigs:
                fl.append("sig")
            ir["events"].append(make_event(
                0, "assistant", "tool_call", f"{nm}|{args_str}",
                ref=cid, flags=fl, tokens=all_sigs, wire_id=raw_id, name=nm
            ))
        elif bt == btypes["thinking"]:
            tx = b.get(th_f["text"], "")
            sig = b.get(th_f["signature"])
            toks = ([sig] if sig else []) + oob_sigs
            ir["events"].append(make_event(
                0, "assistant", "thinking", tx,
                flags=["sig"] if toks else [], tokens=toks
            ))
        elif bt == btypes["redacted_thinking"]:
            dat = b.get(rth_f["data"])
            ir["events"].append(make_event(
                0, "assistant", "redacted_thinking", "",
                flags=["redacted"], tokens=[dat] if dat else []
            ))
        elif bt in (btypes["server_tool_use"], btypes["web_search_tool_result"], btypes["code_execution_tool_result"],
                    btypes["mcp_tool_use"], btypes["mcp_tool_result"]):
            ir["events"].append(make_event(0, "assistant", "server_tool", _canon_json(b), name=bt))
        else:
            ir["events"].append(make_event(0, "assistant", "other", _canon_json(b), name=bt))

    sr = msg.get(t["stop_key"])
    ir["stop_raw"] = sr
    ir["stop"] = t["stop_map"].get(sr, "other" if sr else None)

    u = msg.get(t["usage_key"])
    if isinstance(u, dict):
        uf = t["usage_fields"]
        ir["usage"]["input"] = u.get(uf["input"])
        ir["usage"]["output"] = u.get(uf["output"])
        ir["usage"]["cache_read"] = u.get(uf["cache_read"])
        ir["usage"]["cache_write"] = u.get(uf["cache_write"])
        extra_u = [k for k in u if k not in set(uf.values())]
        if extra_u:
            ir["envelope"]["extra_usage_keys"] = extra_u
    return ir


def _parse_openai_chat_response(r, disabled):
    t = C.table("openai_chat", "response")
    tr = C.table("openai_chat", "request")
    ir = _blank_ir("openai_chat", "response")
    if not isinstance(r, dict):
        return ir

    ir["has_model_key"] = t["model_key"] in r
    ir["model"] = r.get(t["model_key"])
    ir["envelope"]["id"] = r.get(t["id_key"])

    choices = r.get(t["choices_key"]) or [{}]
    ch = choices[0] if choices and isinstance(choices[0], dict) else {}
    m = ch.get(t["message_key"]) or {}
    rk = tr["assistant_reasoning_keys"]
    tbf = tr["thinking_block_fields"]
    tc_f = tr["tool_call_fields"]

    if m.get(rk["reasoning_content"]):
        ir["events"].append(make_event(0, "assistant", "thinking", m[rk["reasoning_content"]]))
    rd = m.get(rk["reasoning_details"])
    if isinstance(rd, list) and rd:
        txt = "".join(str(x.get(tbf["text"]) or x.get(tbf["summary"]) or "") for x in rd if isinstance(x, dict))
        enc_toks = [x.get(tbf["data"]) for x in rd if isinstance(x, dict) and (x.get(tbf["type"]) == tbf["encrypted_type"] or x.get(tbf["data"]))]
        if ir["events"] and ir["events"][-1]["kind"] == "thinking" and ir["events"][-1]["payload"] == txt:
            if enc_toks:
                ir["events"][-1]["flags"] = sorted(set(ir["events"][-1]["flags"]) | {"sig"})
                ir["events"][-1]["tokens"].extend(enc_toks)
        elif txt or enc_toks:
            ir["events"].append(make_event(0, "assistant", "thinking", txt, flags=["sig"] if enc_toks else [], tokens=enc_toks))
    elif isinstance(m.get(rk["reasoning"]), str) and m[rk["reasoning"]] and not m.get(rk["reasoning_content"]):
        ir["events"].append(make_event(0, "assistant", "thinking", m[rk["reasoning"]]))

    if m.get(rk["refusal"]):
        ir["events"].append(make_event(0, "assistant", "refusal", m[rk["refusal"]]))

    c = m.get(tr["content_key"])
    if isinstance(c, str):
        if c == "":
            if not m.get(tr["assistant_tool_calls_key"]):
                ir["empty_text_blocks"] += 1
        else:
            ir["events"].append(make_event(0, "assistant", "text", c))
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, dict) and p.get(tr["part_type_key"]) == tr["part_types"]["text"]:
                tx = p.get(tr["text_key"], "")
                ec = 1 if tx == "" else 0
                if ec:
                    ir["empty_text_blocks"] += 1
                ir["events"].append(make_event(0, "assistant", "text", tx, empty_count=ec))

    for tc in (m.get(tr["assistant_tool_calls_key"]) or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get(tc_f["function"]) or {}
        args_str, afl = _canon_args(fn.get(tc_f["arguments"], ""))
        raw_id = tc.get(tc_f["id"])
        cid, id_sigs = _split_id_sig(raw_id)
        oob_sigs = _extract_oob_sig_tokens(tc)
        all_sigs = id_sigs + oob_sigs
        nm = fn.get(tc_f["name"]) or ""
        fl = list(afl) + (["sig"] if all_sigs else [])
        ir["events"].append(make_event(
            0, "assistant", "tool_call", f"{nm}|{args_str}",
            ref=cid, flags=fl, tokens=all_sigs, wire_id=raw_id, name=nm
        ))

    fr = ch.get(t["stop_key"])
    ir["stop_raw"] = fr
    stop = t["stop_map"].get(fr, "other" if fr else None)
    if stop == "end_turn" and any(e["kind"] == "refusal" for e in ir["events"]):
        stop = "refusal"
    ir["stop"] = stop

    u = r.get(t["usage_key"])
    if isinstance(u, dict):
        uf = t["usage_fields"]
        ir["usage"]["input"] = u.get(uf["input"])
        ir["usage"]["output"] = u.get(uf["output"])
        ir["usage"]["total"] = u.get(uf["total"])
        ir["usage"]["cache_read"] = (u.get(uf["prompt_details"]) or {}).get(uf["cached"])
        ir["usage"]["reasoning"] = (u.get(uf["completion_details"]) or {}).get(uf["reasoning"])
    return ir


def _parse_openai_responses_response(r, disabled):
    t = C.table("openai_responses", "response")
    tr = C.table("openai_responses", "request")
    ir = _blank_ir("openai_responses", "response")
    if not isinstance(r, dict):
        return ir

    ir["has_model_key"] = t["model_key"] in r
    ir["model"] = r.get(t["model_key"])
    ir["envelope"]["id"] = r.get(t["id_key"])

    itypes = tr["item_types"]
    ptypes = tr["part_types"]
    it_key = tr["item_type_key"]
    fc_f = tr["function_call_fields"]
    rs_f = tr["reasoning_fields"]

    for it in (r.get(t["output_key"]) or []):
        if not isinstance(it, dict):
            continue
        itype = it.get(it_key)
        if itype == itypes["message"]:
            for p in (it.get(tr["content_key"]) or []):
                if not isinstance(p, dict):
                    continue
                pt = p.get(it_key)
                if pt in (ptypes["output_text"], ptypes["text"]):
                    tx = p.get(tr["text_key"], "")
                    ec = 1 if tx == "" else 0
                    if ec:
                        ir["empty_text_blocks"] += 1
                    ir["events"].append(make_event(0, "assistant", "text", tx, empty_count=ec))
                elif pt == ptypes["refusal"]:
                    ir["events"].append(make_event(0, "assistant", "refusal", p.get(tr["refusal_key"], "")))
        elif itype == itypes["function_call"]:
            raw_id = it.get(fc_f["call_id"]) or it.get(fc_f["id"])
            cid, id_sigs = _split_id_sig(raw_id)
            nm = it.get(fc_f["name"]) or ""
            args_str, afl = _canon_args(it.get(fc_f["arguments"], ""))
            ir["events"].append(make_event(
                0, "assistant", "tool_call", f"{nm}|{args_str}",
                ref=cid, flags=list(afl) + (["sig"] if id_sigs else []), tokens=id_sigs, wire_id=raw_id, name=nm
            ))
        elif itype == itypes["reasoning"]:
            sums = it.get(rs_f["summary"]) or []
            sum_txt = "".join(
                p.get(rs_f["text"], "") for p in sums
                if isinstance(p, dict) and p.get(it_key) in (rs_f["summary_text_type"], ptypes["text"])
            )
            enc = it.get(rs_f["encrypted_content"])
            toks = [enc] if enc else []
            if sum_txt:
                ir["events"].append(make_event(0, "assistant", "thinking", sum_txt, flags=["sig"] if toks else [], tokens=toks))
            elif enc:
                ir["events"].append(make_event(0, "assistant", "redacted_thinking", "", flags=["redacted"], tokens=toks))

    st = r.get(t["status_key"])
    ir["stop_raw"] = st
    stop = t["stop_map"].get(st, "other" if st else None)
    if st == "incomplete" and isinstance(r.get(t["incomplete_details_key"]), dict):
        rsn = r[t["incomplete_details_key"]].get(t["reason_key"])
        stop = t["incomplete_reason_map"].get(rsn, stop)
    if stop == "end_turn" and any(e["kind"] == "tool_call" for e in ir["events"]):
        stop = "tool_use"
    elif stop == "end_turn" and any(e["kind"] == "refusal" for e in ir["events"]):
        stop = "refusal"
    ir["stop"] = stop

    u = r.get(t["usage_key"])
    if isinstance(u, dict):
        uf = t["usage_fields"]
        ir["usage"]["input"] = u.get(uf["input"])
        ir["usage"]["output"] = u.get(uf["output"])
        ir["usage"]["total"] = u.get(uf["total"])
        ir["usage"]["cache_read"] = (u.get(uf["input_details"]) or {}).get(uf["cached"])
        ir["usage"]["reasoning"] = (u.get(uf["output_details"]) or {}).get(uf["reasoning"])
    return ir


def _parse_gemini_response(r, disabled):
    t = C.table("gemini_generatecontent", "response")
    tr = C.table("gemini_generatecontent", "request")
    ir = _blank_ir("gemini_generatecontent", "response")
    if not isinstance(r, dict):
        return ir

    mv = C.first_present(r, t["model_version_keys"])
    ir["has_model_key"] = mv is not None
    ir["model"] = mv

    cands = r.get(t["candidates_key"]) or [{}]
    cand = cands[0] if cands and isinstance(cands[0], dict) else {}
    content = cand.get(t["content_key"]) or {}
    parts = content.get(t["parts_key"]) or []
    pk = tr["part_keys"]
    fc_f = tr["function_call_fields"]

    for p in parts:
        if not isinstance(p, dict):
            continue
        sig = C.first_present(p, pk["thought_sigs"])
        toks = [sig] if sig else []
        fl = ["sig"] if toks else []
        if C.has_any_key(p, pk["function_calls"]):
            fc = C.first_present(p, pk["function_calls"]) or {}
            args_str, afl = _canon_args(fc.get(fc_f["args"], {}))
            raw_id = fc.get(fc_f["id"])
            cid, id_sigs = _split_id_sig(raw_id)
            nm = fc.get(fc_f["name"]) or ""
            ir["events"].append(make_event(
                0, "assistant", "tool_call", f"{nm}|{args_str}",
                ref=cid, flags=fl + afl + (["sig"] if id_sigs else []), tokens=toks + id_sigs, wire_id=raw_id, name=nm
            ))
        elif p.get(pk["thought"]) and p.get(pk["text"]) is not None:
            ir["events"].append(make_event(0, "assistant", "thinking", p[pk["text"]], flags=fl, tokens=toks))
        elif pk["text"] in p:
            tx = p[pk["text"]]
            ec = 1 if tx == "" else 0
            if ec and not toks:
                ir["empty_text_blocks"] += 1
            ir["events"].append(make_event(0, "assistant", "text", tx, flags=fl, tokens=toks, empty_count=ec))

    fr = C.first_present(cand, t["finish_reason_keys"])
    ir["stop_raw"] = fr
    ir["stop"] = t["stop_map"].get(fr, "other" if fr else None)

    u = C.first_present(r, t["usage_keys"])
    if isinstance(u, dict):
        uf = t["usage_fields"]
        ir["usage"]["input"] = C.first_present(u, uf["input"])
        ir["usage"]["output"] = C.first_present(u, uf["output"])
        ir["usage"]["total"] = C.first_present(u, uf["total"])
        ir["usage"]["cache_read"] = C.first_present(u, uf["cached"])
        ir["usage"]["reasoning"] = C.first_present(u, uf["reasoning"])
    return ir


# ----------------------------------------------------------------------
# STREAM PARSERS
# ----------------------------------------------------------------------

def split_sse_frames(raw_stream):
    if isinstance(raw_stream, bytes):
        raw_stream = raw_stream.decode("utf-8", "replace")
    if isinstance(raw_stream, list):
        out = []
        for item in raw_stream:
            if isinstance(item, dict):
                ev = item.get("event")
                dat = item.get("data")
                out.append((ev, dat if isinstance(dat, str) else json.dumps(dat)))
            elif isinstance(item, (tuple, list)) and len(item) == 2:
                out.append((item[0], item[1]))
        return out
    if not isinstance(raw_stream, str):
        return []
    sse_p = C.common()["sse_prefixes"]
    ev_pfx = sse_p["event"]
    da_pfx = sse_p["data"]
    out, ev, data = [], None, []
    for line in raw_stream.split("\n"):
        line = line.rstrip("\r")
        if line == "":
            if data:
                out.append((ev, "\n".join(data)))
            ev, data = None, []
        elif line.startswith(ev_pfx):
            ev = line[len(ev_pfx):].strip()
        elif line.startswith(da_pfx):
            data.append(line[len(da_pfx):].lstrip())
        elif line.startswith(":"):
            continue
    if data:
        out.append((ev, "\n".join(data)))
    return out


def _parse_anthropic_stream(raw_stream, disabled):
    ts = C.table("anthropic_messages", "stream")
    tr = C.table("anthropic_messages", "response")
    treq = C.table("anthropic_messages", "request")
    evs = ts["event_types"]
    dts = ts["delta_types"]
    f = ts["fields"]

    frames = split_sse_frames(raw_stream)
    msg = {
        tr["id_key"]: None,
        tr["type_key"]: tr["type_val"],
        tr["role_key"]: tr["role_val"],
        tr["model_key"]: None,
        tr["content_key"]: [],
        tr["stop_key"]: None,
        tr["stop_seq_key"]: None,
        tr["usage_key"]: {},
    }
    ledger = {
        "anomalies": [],
        "missing_nullable_events": [],
        "delta_is_error_events": [],
        "events_after_stop": [],
        "unpaired_blocks": [],
        "start_nonempty_tool_inputs": [],
        "done_in_anthropic": False,
        "non_json_frames": [],
        "message_start_foreign_keys": [],
        "empty_text_deltas": 0,
        "raw_events_count": len(frames),
    }

    blocks = {}
    partial = {}
    seen_start = False
    seen_message_delta = False
    seen_stop = False
    ledger["overwritten_start_tool_inputs"] = []
    ledger["usage_anomalies"] = []
    ledger["extra_event_keys"] = []

    for frame_idx, (ev_name, data_str) in enumerate(frames):
        if data_str.strip() == ts["done_sentinel"]:
            ledger["done_in_anthropic"] = True
            ledger["anomalies"].append("done_sentinel_in_anthropic_stream")
            continue
        try:
            d = json.loads(data_str)
        except Exception:
            ledger["non_json_frames"].append(data_str[:80])
            ledger["anomalies"].append(f"non_json_data:{data_str[:60]}")
            continue
        if not isinstance(d, dict):
            ledger["anomalies"].append("frame_not_object")
            continue

        t = d.get(f["type"]) or ev_name
        if seen_stop and t in (
            evs["message_start"], evs["content_block_start"],
            evs["content_block_delta"], evs["content_block_stop"], evs["message_delta"]
        ):
            ledger["events_after_stop"].append(t)
            ledger["anomalies"].append(f"{t}_after_message_stop")

        if t == evs["message_start"]:
            if seen_start:
                ledger["anomalies"].append("duplicate_message_start")
            seen_start = True
            for fk in ts["message_start_foreign_keys"]:
                if fk in d:
                    ledger["message_start_foreign_keys"].append(fk)
            m = d.get(f["message"]) or {}
            if isinstance(m, dict):
                if tr["id_key"] in m:
                    msg[tr["id_key"]] = m[tr["id_key"]]
                else:
                    msg.pop(tr["id_key"], None)
                if tr["model_key"] in m:
                    msg[tr["model_key"]] = m[tr["model_key"]]
                else:
                    msg.pop(tr["model_key"], None)
                if tr["role_key"] in m:
                    msg[tr["role_key"]] = m[tr["role_key"]]
                else:
                    msg.pop(tr["role_key"], None)
                if tr["type_key"] in m:
                    msg[tr["type_key"]] = m[tr["type_key"]]
                else:
                    msg.pop(tr["type_key"], None)
                if tr["stop_seq_key"] in m and m[tr["stop_seq_key"]] is not None:
                    msg[tr["stop_seq_key"]] = m[tr["stop_seq_key"]]
                    ledger["message_start_foreign_keys"].append(tr["stop_seq_key"])
                if m.get(tr["stop_key"]) is not None:
                    ledger["message_start_foreign_keys"].append(tr["stop_key"])
                u_init = m.get(tr["usage_key"])
                if isinstance(u_init, dict) and tr["usage_fields"]["output"] not in u_init:
                    ledger["usage_anomalies"].append("missing_initial_output_tokens")
                msg[tr["usage_key"]] = dict(u_init or {})
                for k, v in m.items():
                    if k not in msg and k not in (tr["id_key"], tr["model_key"], tr["role_key"], tr["type_key"]):
                        msg[k] = v
                for fk in ts["message_start_foreign_keys"]:
                    if fk in m:
                        ledger["message_start_foreign_keys"].append(fk)
                for nk in tr["nullable_required_keys"]:
                    if nk not in m:
                        ledger["missing_nullable_events"].append((evs["message_start"], nk))
                if m.get(tr["content_key"]):
                    ledger["anomalies"].append("message_start_carries_content")
        elif t == evs["content_block_start"]:
            if not seen_start:
                ledger["anomalies"].append("content_block_start_before_message_start")
            idx = d.get(f["index"])
            cb = dict(d.get(f["content_block"]) or {})
            if idx in blocks:
                ledger["anomalies"].append(f"duplicate_content_block_start:{idx}")
            btype = cb.get(treq["block_type_key"])
            if btype == treq["block_types"]["tool_use"]:
                inp_v = cb.get(treq["tool_use_fields"]["input"])
                partial[idx] = "" if not inp_v else None
                if inp_v not in (None, {}):
                    ledger["start_nonempty_tool_inputs"].append(idx)
            blocks[idx] = cb
        elif t == evs["content_block_delta"]:
            if not seen_start:
                ledger["anomalies"].append("content_block_delta_before_message_start")
            idx = d.get(f["index"])
            dl = d.get(f["delta"]) or {}
            if isinstance(dl, dict) and f["is_error"] in dl:
                ledger["delta_is_error_events"].append((idx, dl.get(f["is_error"])))
            if isinstance(dl, dict) and treq["tool_use_fields"]["input"] in dl:
                ledger["anomalies"].append("tool_input_on_delta_outside_input_json_delta")
            if blocks.get(idx, {}).get("_stopped"):
                ledger["anomalies"].append(f"delta_after_content_block_stop:{idx}")
            if idx not in blocks:
                ledger["unpaired_blocks"].append(f"delta_unknown_block:{idx}")
                dt_guess = {
                    dts["text"]: treq["block_types"]["text"],
                    dts["input_json"]: treq["block_types"]["tool_use"],
                    dts["thinking"]: treq["block_types"]["thinking"],
                }.get(dl.get(f["type"]), "unknown")
                blocks[idx] = {treq["block_type_key"]: dt_guess}
                if dt_guess == treq["block_types"]["tool_use"]:
                    partial[idx] = ""
            b = blocks[idx]
            dt = dl.get(f["type"])
            if dt == dts["text"]:
                tx = dl.get(f["text"], "")
                if tx == "":
                    ledger["empty_text_deltas"] += 1
                b[f["text"]] = b.get(f["text"], "") + tx
            elif dt == dts["input_json"]:
                if idx in ledger["start_nonempty_tool_inputs"] and idx not in ledger["overwritten_start_tool_inputs"]:
                    ledger["overwritten_start_tool_inputs"].append(idx)
                partial[idx] = (partial.get(idx) or "") + dl.get(f["partial_json"], "")
            elif dt == dts["thinking"]:
                b[f["thinking"]] = b.get(f["thinking"], "") + dl.get(f["thinking"], "")
            elif dt == dts["signature"]:
                b[f["signature"]] = b.get(f["signature"], "") + dl.get(f["signature"], "")
            elif dt != dts["citations"]:
                ledger["anomalies"].append(f"unknown_delta_type:{dt}")
        elif t == evs["content_block_stop"]:
            if not seen_start:
                ledger["anomalies"].append("content_block_stop_before_message_start")
            idx = d.get(f["index"])
            if seen_message_delta:
                ledger["anomalies"].append(f"content_block_stop_after_message_delta:{idx}")
            if idx not in blocks:
                ledger["unpaired_blocks"].append(f"stop_unknown_block:{idx}")
            elif blocks[idx].get("_stopped"):
                ledger["anomalies"].append(f"duplicate_content_block_stop:{idx}")
            blocks.setdefault(idx, {})["_stopped"] = True
        elif t == evs["message_delta"]:
            seen_message_delta = True
            dl = d.get(f["delta"]) or {}
            if isinstance(dl, dict):
                if tr["stop_key"] in dl:
                    msg[tr["stop_key"]] = dl[tr["stop_key"]]
                if tr["stop_seq_key"] in dl:
                    if dl[tr["stop_seq_key"]] is not None or msg.get(tr["stop_seq_key"]) is None:
                        msg[tr["stop_seq_key"]] = dl[tr["stop_seq_key"]]
                else:
                    ledger["missing_nullable_events"].append((evs["message_delta"], tr["stop_seq_key"]))
                if f["is_error"] in dl:
                    ledger["delta_is_error_events"].append((evs["message_delta"], dl.get(f["is_error"])))
            for k, v in (d.get(f["usage"]) or {}).items():
                if v is not None:
                    msg[tr["usage_key"]][k] = v
        elif t == evs["message_stop"]:
            seen_stop = True
            for ek in sorted(k for k in d if k != f["type"]):
                ledger["extra_event_keys"].append(f"{evs['message_stop']}.{ek}")
        elif t == evs["ping"]:
            pass
        elif t == evs["error"]:
            ledger["anomalies"].append("stream_error_event")
        else:
            ledger["anomalies"].append(f"unknown_event:{t}")

    if not seen_start:
        ledger["anomalies"].append("no_message_start")
    if not seen_stop:
        ledger["anomalies"].append("no_message_stop")

    for idx in sorted(k for k in blocks if isinstance(k, int)):
        b = dict(blocks[idx])
        stopped = b.pop("_stopped", False)
        if not stopped:
            ledger["unpaired_blocks"].append(f"block_never_stopped:{idx}")
        if b.get(treq["block_type_key"]) == treq["block_types"]["tool_use"]:
            pj = partial.get(idx)
            if pj is not None:
                if pj == "":
                    b[treq["tool_use_fields"]["input"]] = {}
                else:
                    try:
                        b[treq["tool_use_fields"]["input"]] = json.loads(pj)
                    except Exception:
                        b[treq["tool_use_fields"]["input"]] = pj
                        ledger["anomalies"].append(f"partial_json_unparsable:{idx}")
        msg[tr["content_key"]].append(b)

    ir = _parse_anthropic_response(msg, disabled)
    ir["phase"] = "stream"
    ir["empty_text_blocks"] += ledger["empty_text_deltas"]
    ir["stream_ledger"] = ledger
    ir["reassembled_body"] = msg
    return ir


def _parse_openai_chat_stream(raw_stream, disabled):
    ts = C.table("openai_chat", "stream")
    tr = C.table("openai_chat", "response")
    treq = C.table("openai_chat", "request")
    frames = split_sse_frames(raw_stream)
    text_parts = []
    tool_calls_by_idx = {}
    finish_reason = None
    model = None
    msg_id = None
    usage = None

    for _, data_str in frames:
        if data_str.strip() == ts["done_sentinel"]:
            continue
        try:
            d = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get(tr["model_key"]):
            model = d[tr["model_key"]]
        if d.get(tr["id_key"]):
            msg_id = d[tr["id_key"]]
        if d.get(ts["usage_key"]):
            usage = d[ts["usage_key"]]
        for ch in (d.get(ts["choices_key"]) or []):
            if not isinstance(ch, dict):
                continue
            if ch.get(ts["finish_reason_key"]):
                finish_reason = ch[ts["finish_reason_key"]]
            dl = ch.get(ts["delta_key"]) or {}
            if isinstance(dl.get(treq["content_key"]), str) and dl[treq["content_key"]]:
                text_parts.append(dl[treq["content_key"]])
            for tc in (dl.get(treq["assistant_tool_calls_key"]) or []):
                if not isinstance(tc, dict):
                    continue
                idx = tc.get("index", 0)
                cur = tool_calls_by_idx.setdefault(idx, {
                    treq["tool_call_fields"]["id"]: None,
                    treq["tool_call_fields"]["type"]: treq["tool_fields"]["function_val"],
                    treq["tool_call_fields"]["function"]: {treq["tool_call_fields"]["name"]: "", treq["tool_call_fields"]["arguments"]: ""}
                })
                if tc.get(treq["tool_call_fields"]["id"]):
                    cur[treq["tool_call_fields"]["id"]] = tc[treq["tool_call_fields"]["id"]]
                fn = tc.get(treq["tool_call_fields"]["function"]) or {}
                if fn.get(treq["tool_call_fields"]["name"]):
                    cur[treq["tool_call_fields"]["function"]][treq["tool_call_fields"]["name"]] += fn[treq["tool_call_fields"]["name"]]
                if fn.get(treq["tool_call_fields"]["arguments"]):
                    cur[treq["tool_call_fields"]["function"]][treq["tool_call_fields"]["arguments"]] += fn[treq["tool_call_fields"]["arguments"]]

    assembled = {
        tr["id_key"]: msg_id,
        tr["model_key"]: model,
        tr["choices_key"]: [{
            tr["message_key"]: {
                treq["role_key"]: treq["roles"]["assistant"],
                treq["content_key"]: "".join(text_parts) if text_parts else None,
                treq["assistant_tool_calls_key"]: [tool_calls_by_idx[k] for k in sorted(tool_calls_by_idx)] if tool_calls_by_idx else None,
            },
            tr["stop_key"]: finish_reason,
        }],
        tr["usage_key"]: usage,
    }
    ir = _parse_openai_chat_response(assembled, disabled)
    ir["phase"] = "stream"
    ir["stream_ledger"] = {"anomalies": [], "raw_events_count": len(frames)}
    return ir


def _parse_openai_responses_stream(raw_stream, disabled):
    ts = C.table("openai_responses", "stream")
    frames = split_sse_frames(raw_stream)
    final_resp = None
    for ev_name, data_str in frames:
        try:
            d = json.loads(data_str)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        t = d.get(ts["fields"]["type"]) or ev_name
        if t in (ts["events"]["completed"], ts["events"]["incomplete"], ts["events"]["failed"]):
            final_resp = d.get(ts["fields"]["response"]) or d
    if final_resp is None:
        final_resp = {}
    ir = _parse_openai_responses_response(final_resp, disabled)
    ir["phase"] = "stream"
    ir["stream_ledger"] = {"anomalies": [], "raw_events_count": len(frames)}
    return ir


def _parse_gemini_stream(raw_stream, disabled):
    frames = split_sse_frames(raw_stream)
    chunks = []
    for _, data_str in frames:
        try:
            d = json.loads(data_str)
            if isinstance(d, dict):
                chunks.append(d)
        except Exception:
            continue
    if not chunks:
        ir = _blank_ir("gemini_generatecontent", "stream")
        ir["stream_ledger"] = {"anomalies": [], "raw_events_count": 0}
        return ir
    # Merge parts across chunks
    tr = C.table("gemini_generatecontent", "response")
    treq = C.table("gemini_generatecontent", "request")
    all_parts = []
    finish_reason = None
    usage = None
    model_ver = None
    for ch in chunks:
        mv = C.first_present(ch, tr["model_version_keys"])
        if mv:
            model_ver = mv
        u = C.first_present(ch, tr["usage_keys"])
        if u:
            usage = u
        for cand in (ch.get(tr["candidates_key"]) or []):
            if not isinstance(cand, dict):
                continue
            fr = C.first_present(cand, tr["finish_reason_keys"])
            if fr:
                finish_reason = fr
            cnt = cand.get(tr["content_key"]) or {}
            all_parts.extend(cnt.get(tr["parts_key"]) or [])
    assembled = {
        tr["candidates_key"]: [{
            tr["content_key"]: {tr["parts_key"]: all_parts},
            tr["finish_reason_keys"][0]: finish_reason,
        }],
        tr["usage_keys"][0]: usage,
        tr["model_version_keys"][0]: model_ver,
    }
    ir = _parse_gemini_response(assembled, disabled)
    ir["phase"] = "stream"
    ir["stream_ledger"] = {"anomalies": [], "raw_events_count": len(frames)}
    return ir


# ----------------------------------------------------------------------
# PUBLIC ENTRY POINT
# ----------------------------------------------------------------------

_DISPATCH = {
    ("anthropic_messages", "request"): _parse_anthropic_request,
    ("openai_chat", "request"): _parse_openai_chat_request,
    ("openai_responses", "request"): _parse_openai_responses_request,
    ("gemini_generatecontent", "request"): _parse_gemini_request,
    ("anthropic_messages", "response"): _parse_anthropic_response,
    ("openai_chat", "response"): _parse_openai_chat_response,
    ("openai_responses", "response"): _parse_openai_responses_response,
    ("gemini_generatecontent", "response"): _parse_gemini_response,
    ("anthropic_messages", "stream"): _parse_anthropic_stream,
    ("openai_chat", "stream"): _parse_openai_chat_stream,
    ("openai_responses", "stream"): _parse_openai_responses_stream,
    ("gemini_generatecontent", "stream"): _parse_gemini_stream,
}


def parse_ir(body, dialect, phase, *, disabled=frozenset()):
    d = C.resolve_dialect(dialect)
    fn = _DISPATCH.get((d, phase))
    if fn is None:
        raise ValueError(f"Unsupported (dialect, phase): {(d, phase)!r}")
    return fn(body, disabled)
