"""Evidence locator (Oracle v1.3).

Maps a finding to a JSON Pointer (RFC 6901) into the source or destination payload that was judged.
It runs after the verdicts are computed and never changes them: the rule logic, tiers and detail strings
come from the judge, and this module only searches the two payloads for what the finding talks about.

Evidence record: {"side": "src" | "dst", "path": "<JSON Pointer>", "precision": "exact" | "container" | "payload"}
  exact      the pointer resolves to the offending value (a control, a key, a text, a tool declaration, a stream frame).
  container  the pointer resolves to the object that holds it (a message, the messages array, a tools array,
             the payload root for an absent key; then "missing_key" names the key).
  payload    nothing narrower was found; the pointer is "" (the whole payload).
Stream payloads are addressed as the list of their `data:` frames (0-based, in order, `[DONE]` included):
"/3/delta" is the `delta` object of the 4th frame.  Request and response payloads are addressed as parsed.
"""
import ast
import json
import re

from . import carriers as C
from .ir import split_sse_frames

_QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_ID = re.compile(r"\b(?:toolu|call|fc|tc|chatcmpl|msg)_[A-Za-z0-9_\-]+")
_STREAM_EVENTS = ("content_block_delta", "content_block_stop", "content_block_start", "message_start",
                  "message_delta", "message_stop", "error")


def _esc(k):
    return str(k).replace("~", "~0").replace("/", "~1")


def resolve_pointer(doc, ptr):
    """Return the value at JSON Pointer `ptr`; raises KeyError/IndexError/TypeError when it does not exist."""
    if ptr == "":
        return doc
    node = doc
    for part in ptr.split("/")[1:]:
        part = part.replace("~1", "/").replace("~0", "~")
        node = node[int(part)] if isinstance(node, list) else node[part]
    return node


def _doc(body, phase):
    if phase != "stream":
        return body
    out = []
    for _ev, data in split_sse_frames(body):
        try:
            out.append(json.loads(data))
        except Exception:  # noqa: BLE001 - '[DONE]' and other non-JSON data frames
            out.append(None)
    return out


def _walk(node, ptr="", depth=0, max_depth=8):
    yield ptr, None, node
    if depth >= max_depth:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{ptr}/{_esc(k)}"
            yield p, k, v
            yield from _walk_children(v, p, depth + 1, max_depth)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            p = f"{ptr}/{i}"
            yield p, None, v
            yield from _walk_children(v, p, depth + 1, max_depth)


def _walk_children(node, ptr, depth, max_depth):
    if depth >= max_depth:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{ptr}/{_esc(k)}"
            yield p, k, v
            yield from _walk_children(v, p, depth + 1, max_depth)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            p = f"{ptr}/{i}"
            yield p, None, v
            yield from _walk_children(v, p, depth + 1, max_depth)


def _find_key(doc, names, max_depth=4):
    """Pointer of the earliest key in `names` (document order, shallowest levels earliest in walk order)."""
    names = set(names)
    best = None
    for p, k, _v in _walk(doc, "", 0, max_depth):
        if k in names:
            depth = p.count("/")
            if best is None or depth < best[0]:
                best = (depth, p)
    return best[1] if best else None


def _norm(s):
    return re.sub(r"\s+", "", s)


def _find_text(doc, needle):
    needle = _norm(needle)
    if len(needle) < 2:
        return None
    for p, k, v in _walk(doc, "", 0, 9):
        if isinstance(v, str):
            nv = _norm(v)
            if needle == nv or needle in nv:
                return p
    return None


def _quotes(detail):
    out = []
    for m in _QUOTED.finditer(detail):
        lit = m.group(0)
        try:
            val = ast.literal_eval(lit)
        except Exception:  # noqa: BLE001
            val = lit[1:-1]
        if isinstance(val, str):
            val = val[:-3] if val.endswith("...") else val
            if len(_norm(val)) >= 2:
                out.append(val)
    return out


def _tables(dialect, phase):
    try:
        return C.table(dialect, phase)
    except Exception:  # noqa: BLE001
        return {}


class _Ctx:
    def __init__(self, detail, src, sd, dst, dd, side):
        self.detail, self.side = detail, side
        self.docs = {"src": _doc(src, side), "dst": _doc(dst, side)}
        self.dialects = {"src": sd, "dst": dd}
        self.prefer = ("src", "dst") if re.search(r"\b(lost|dropped)\b", detail) else ("dst", "src")

    def phase(self):
        return self.side

    def tab(self, which):
        return _tables(self.dialects[which], self.side)

    def find_key(self, names, which_order=None, max_depth=4):
        for w in which_order or self.prefer:
            p = _find_key(self.docs[w], names, max_depth)
            if p is not None:
                return w, p
        return None


def _hit(side, path, precision, **extra):
    d = {"side": side, "path": path, "precision": precision}
    d.update(extra)
    return d


def _usage(c):
    d = c.detail
    m = re.search(r"usage\.(\w+)", d)
    names = set()
    for w in ("src", "dst"):
        t = _tables(c.dialects[w], "response")
        if t.get("usage_key"):
            names.add(t["usage_key"])
    names |= {"usage", "usageMetadata"}
    if "stop_reason duplicated" in d:
        stop = _tables("anthropic_messages", "response").get("stop_key", "stop_reason")
        r = c.find_key({stop}, ("dst",), 6)
        for p, k, _v in _walk(c.docs["dst"], "", 0, 6):
            if k == stop and "/usage/" in p + "/":
                return _hit("dst", p, "exact")
    if "keys leaked in usage" in d:
        ks = re.findall(r"'([^']+)'", d)
        for k in ks:
            r = c.find_key({k}, ("dst",), 6)
            if r:
                return _hit(r[0], r[1], "exact")
    if m or "usage anomaly" in d:
        uf = m.group(1) if m else None
        for w in c.prefer:
            t = _tables(c.dialects[w], "response")
            wire = (t.get("usage_fields") or {}).get(uf) if uf else None
            if wire:
                r = c.find_key({wire}, (w,), 6)
                if r:
                    return _hit(w, r[1], "exact")
        r = c.find_key(names, None, 6)
        if r:
            return _hit(r[0], r[1], "container")
    return None


def _control(c):
    m = re.search(r"control (\w+)", c.detail)
    if not m:
        return None
    ir = m.group(1)
    for w in c.prefer:
        t = _tables(c.dialects[w], "request")
        wire = (t.get("controls") or {}).get(ir)
        names = [wire] if isinstance(wire, str) else list(wire or [ir])
        r = c.find_key(set(names), (w,), 3)
        if r:
            return _hit(r[0], r[1], "exact")
    return None


def _tool(c):
    m = re.match(r"tool ([^\s:/]+)", c.detail)
    if not m:
        return None
    name = m.group(1)
    for w in c.prefer:
        t = _tables(c.dialects[w], "request")
        tools_key = t.get("tools_key", "tools")
        base = _find_key(c.docs[w], {tools_key}, 1)
        if base is None:
            continue
        for p, k, v in _walk(resolve_pointer(c.docs[w], base), base, 0, 5):
            if isinstance(v, dict) and v.get("name") == name:
                return _hit(w, p, "exact")
    return None


def _tools_array(c):
    if c.side != "request" or not (c.detail.startswith("declared tool names") or "tool declarations" in c.detail):
        return None
    for w in c.prefer:
        t = _tables(c.dialects[w], "request")
        p = _find_key(c.docs[w], {t.get("tools_key", "tools")}, 1)
        if p is not None:
            return _hit(w, p, "container")
    return None


def _envelope_key(c):
    d = c.detail
    if not re.search(r"(leaked on envelope|unknown custom key|absent nullable required key)", d):
        return None
    ks = re.findall(r"'([^']+)'", d)
    if not ks:
        return None
    k = ks[0]
    if "absent nullable" in d:
        for w in ("dst",):
            return _hit(w, "", "container", missing_key=k)
    r = c.find_key({k}, ("dst",), 4)
    return _hit(r[0], r[1], "exact") if r else None


def _model_or_id(c):
    d = c.detail
    if "model" in d and re.search(r"model (changed|key missing)", d):
        names = {"model", "modelVersion"}
    elif d.startswith("envelope id"):
        names = {"id", "responseId"}
    else:
        return None
    if "missing" in d:
        return _hit("dst", "", "container", missing_key=sorted(names)[0])
    r = c.find_key(names, ("dst", "src"), 4)
    return _hit(r[0], r[1], "exact") if r else None


def _stop(c):
    if not c.detail.startswith("stop/finish reason"):
        return None
    names = {"stop_reason", "finish_reason", "finishReason", "status", "incomplete_details"}
    for w in ("dst", "src"):
        t = _tables(c.dialects[w], "response")
        if t.get("stop_key"):
            names.add(t["stop_key"])
    r = c.find_key(names, ("dst", "src"), 6)
    return _hit(r[0], r[1], "exact") if r else None


def _cache(c):
    if not c.detail.startswith("cache_control"):
        return None
    for w in c.prefer:
        key = _tables(c.dialects[w], "request").get("cache_hint_key", "cache_control")
        r = c.find_key({key}, (w,), 8)
        if r:
            return _hit(r[0], r[1], "exact")
    return None


def _empty_text(c):
    if "empty text block" not in c.detail:
        return None
    for p, k, v in _walk(c.docs["dst"], "", 0, 9):
        if k == "text" and v == "":
            return _hit("dst", p, "exact")
    return None


def _stream_event(c):
    if c.side != "stream":
        return None
    d = c.detail
    ev = [e for e in _STREAM_EVENTS if e in d]
    frames = c.docs["dst"]
    if not isinstance(frames, list):
        return None
    stop_idx = next((i for i, f in enumerate(frames) if isinstance(f, dict) and f.get("type") == "message_stop"), None)
    if "after message_stop" in d or "after_message_stop" in d:
        if stop_idx is not None:
            for i in range(stop_idx + 1, len(frames)):
                if isinstance(frames[i], dict) and (not ev or frames[i].get("type") == ev[0]):
                    return _hit("dst", f"/{i}", "exact")
    if "stream_error_event" in d:
        for i, f in enumerate(frames):
            if isinstance(f, dict) and (f.get("type") == "error" or "error" in f):
                return _hit("dst", f"/{i}", "exact")
    if ev:
        for i, f in enumerate(frames):
            if isinstance(f, dict) and f.get("type") == ev[0]:
                return _hit("dst", f"/{i}", "exact")
    return None


def _snippet(c):
    qs = _quotes(c.detail)
    ids = _ID.findall(c.detail)
    for w in c.prefer:
        for q in qs:
            p = _find_text(c.docs[w], q)
            if p is not None:
                return _hit(w, p, "exact")
        for i in ids:
            for p, k, v in _walk(c.docs[w], "", 0, 9):
                if isinstance(v, str) and v == i:
                    return _hit(w, p, "exact")
    return None


def _turn(c):
    m = re.search(r"\bturn (\d+)", c.detail)
    for w in c.prefer:
        t = _tables(c.dialects[w], "request" if c.side == "request" else c.side)
        key = None
        if c.side == "request":
            key = t.get("messages_key") or t.get("input_key") or t.get("contents_key")
        if not key:
            continue
        base = _find_key(c.docs[w], {key}, 1)
        if base is None:
            continue
        arr = resolve_pointer(c.docs[w], base)
        if m and isinstance(arr, list) and int(m.group(1)) < len(arr):
            return _hit(w, f"{base}/{m.group(1)}", "container")
        if re.search(r"(turn|tool_call|tool_result|parallel group|pairing|order)", c.detail):
            return _hit(w, base, "container")
    return None


_STRATS = (_usage, _control, _tool, _tools_array, _envelope_key, _model_or_id, _stop, _cache, _empty_text,
           _stream_event, _snippet, _turn)


def locate(rule_id, detail, src, src_dialect, dst, dst_dialect, side):
    """Return the evidence record for one finding (never None: falls back to the whole payload)."""
    c = _Ctx(detail, src, src_dialect, dst, dst_dialect, side)
    for strat in _STRATS:
        try:
            hit = strat(c)
        except Exception:  # noqa: BLE001 - a locator error must never change or drop a verdict
            hit = None
        if hit is not None:
            return hit
    return _hit(c.prefer[0], "", "payload")
