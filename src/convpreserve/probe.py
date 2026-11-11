"""`convpreserve probe`: send the bundled request shapes through a running gateway and judge what it sends upstream.

The judge itself opens no sockets; `probe` is the one sub-command that does.  It listens on a loopback
port (the *sink*) that stands in for the upstream provider, POSTs each of the 51 bundled Anthropic Messages
request shapes to the gateway under test, captures the request body the gateway forwards to the sink, and
judges (source shape, forwarded body) on the request side.  Only requests are examined; the sink returns a
minimal valid non-streaming reply.  Standard library only.

The sink replies follow the mock upstream of tooling/leaderboard/drivers/driver_gateways.py (MockRecorder,
ANTH_OK, GEM_OK, OAI_OK); the Responses reply follows
artifacts/g4/release_recheck_2026-10-11/src/verify_fixed_items.py.

Exit codes: 0 no finding at or above --fail-on, 1 at least one such finding, 2 usage, bind or connection error.
"""
import hashlib
import http.client
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .oracles import ORACLES, default_oracle, get_judge

TIERS = ("T1", "T2", "T3")
_TIER_RANK = {"T1": 1, "T2": 2, "T3": 3, "none": 0}
GRACE_S = 0.3          # how long to wait for an upstream request after the gateway has answered
DEFAULT_SINK_PORT = 18600
DEFAULT_HEADERS = (
    ("content-type", "application/json"),
    ("anthropic-version", "2023-06-01"),
    ("x-api-key", "probe"),
    ("authorization", "Bearer probe"),
)
# --target -> (substring identifying the upstream request path, canonical destination dialect)
TARGETS = {
    "openai": ("chat/completions", "openai_chat"),
    "responses": ("/responses", "openai_responses"),
    "gemini": ("generateContent", "gemini_generatecontent"),
    "anthropic": ("/messages", "anthropic_messages"),
}
BUNDLED_SHAPES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "probe_shapes_anthropic_51.json")
_STREAM_KEYS = ("stream", "stream_options")

# ---------------------------------------------------------------- sink replies (minimal valid, non-streaming)
ANTH_OK = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "m",
    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
GEM_OK = {
    "candidates": [{"content": {"role": "model", "parts": [{"text": "ok"}]}, "finishReason": "STOP", "index": 0}],
    "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
}
OAI_OK = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": "m",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
RESP_OK = {
    "id": "resp_1", "object": "response", "created_at": 0, "status": "completed", "model": "gpt-4o",
    "output": [{"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": "ok", "annotations": []}]}],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}


class _UsageError(Exception):
    """Bad command line or unusable input: exit 2."""


class Sink:
    """Loopback upstream stand-in: records every POST (path, parsed JSON body) and answers with a minimal reply."""

    def __init__(self, port):
        self.hits = []
        self._cond = threading.Condition()
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send_json(self, obj, code=200):
                data = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_sse(self, events):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for ev, d in events:
                    chunk = (f"event: {ev}\n" if ev else "") + f"data: {json.dumps(d)}\n\n"
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()

            def do_GET(self):
                self._send_json({"object": "list", "data": [{"id": "gpt-4o", "object": "model"}], "models": []})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw) if raw else {}
                except ValueError:
                    body = {"_raw": raw.decode("utf-8", "replace")}
                path = re.sub(r"key=[^&]*", "key=<redacted>", self.path)
                sink._record(path, body)
                stream = bool(isinstance(body, dict) and body.get("stream")) \
                    or "streamGenerateContent" in self.path or "alt=sse" in self.path
                if "/messages" in self.path:
                    if stream:
                        self._send_sse([
                            ("message_start", {"type": "message_start", "message": {
                                "id": "msg_1", "type": "message", "role": "assistant", "content": [], "model": "m",
                                "stop_reason": None, "stop_sequence": None,
                                "usage": {"input_tokens": 1, "output_tokens": 0}}}),
                            ("content_block_start", {"type": "content_block_start", "index": 0,
                                                     "content_block": {"type": "text", "text": ""}}),
                            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                     "delta": {"type": "text_delta", "text": "ok"}}),
                            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                            ("message_delta", {"type": "message_delta",
                                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                               "usage": {"output_tokens": 1}}),
                            ("message_stop", {"type": "message_stop"}),
                        ])
                    else:
                        self._send_json(ANTH_OK)
                elif "generateContent" in self.path:
                    if stream:
                        self._send_sse([(None, GEM_OK)])
                    else:
                        self._send_json(GEM_OK)
                elif "/responses" in self.path:
                    self._send_json(RESP_OK)
                else:
                    if stream:
                        self._send_sse([
                            (None, {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"},
                                                 "finish_reason": None}]}),
                            (None, {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m",
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                        ])
                        self.wfile.write(b"data: [DONE]\n\n")
                    else:
                        self._send_json(OAI_OK)

        class _Server(ThreadingHTTPServer):
            allow_reuse_address = True
            daemon_threads = True

            def handle_error(self, request, client_address):
                pass

        self._server = _Server(("127.0.0.1", port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _record(self, path, body):
        with self._cond:
            self.hits.append({"path": path, "body": body})
            self._cond.notify_all()

    def find(self, start, hint, wait_s):
        """Last hit at index >= start whose path contains `hint`; waits up to wait_s for one to arrive."""
        deadline = time.monotonic() + wait_s
        with self._cond:
            while True:
                for h in reversed(self.hits[start:]):
                    if hint in h["path"]:
                        return h
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cond.wait(left)

    def count(self):
        with self._cond:
            return len(self.hits)

    def close(self):
        self._server.shutdown()
        self._server.server_close()


# ---------------------------------------------------------------- helpers

def _evidence_col(f):
    e = getattr(f, "evidence", None)
    if not e:
        return None
    return f"{e['side']}:{e['path'] or '(root)'}"


def _finding_dict(f):
    d = {"rule_id": f.rule_id, "tier": f.tier, "object_id": f.object_id, "detail": f.detail}
    if hasattr(f, "evidence"):  # oracle v1.3 only
        d["evidence"] = f.evidence
    return d


def _parse_headers(items):
    hdrs = {k: v for k, v in DEFAULT_HEADERS}
    for item in items or []:
        if ":" not in item:
            raise _UsageError(f"--header expects 'Name: value', got {item!r}")
        name, value = item.split(":", 1)
        name = name.strip().lower()
        if not name:
            raise _UsageError(f"--header has an empty name: {item!r}")
        hdrs[name] = value.strip()
    return hdrs


def _inbound_url(gateway):
    """Anthropic inbound endpoint derived from --gateway: no path -> /v1/messages; a path ending in /messages
    is used as is; any other path gets /v1/messages appended (e.g. http://127.0.0.1:8080/anthropic)."""
    u = urllib.parse.urlsplit(gateway)
    if u.scheme not in ("http", "https") or not u.netloc:
        raise _UsageError(f"--gateway must be an http(s) URL, got {gateway!r}")
    path = u.path.rstrip("/")
    if not path:
        path = "/v1/messages"
    elif not path.endswith("/messages"):
        path += "/v1/messages"
    return urllib.parse.urlunsplit((u.scheme, u.netloc, path, u.query, ""))


def _load_shapes(spec):
    """Return (ordered {id: shape}, provenance dict).  `spec` is 'bundled' or a JSON file: either the bundled
    format ({"shapes": {id: shape}}), a plain {id: shape} object, or a list of shape objects carrying an 'id'."""
    path = BUNDLED_SHAPES if spec == "bundled" else spec
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        data = json.loads(raw)
    except (OSError, ValueError) as e:
        raise _UsageError(f"cannot read shapes {spec!r}: {e}") from e
    prov = {"shapes_source": "bundled" if spec == "bundled" else os.path.basename(path),
            "shapes_file_sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(data, dict) and isinstance(data.get("shapes"), dict):
        for k in ("source_shapes_sha256", "source_validity_sha256"):
            if k in data:
                prov[k] = data[k]
        shapes = data["shapes"]
    elif isinstance(data, dict):
        shapes = data
    elif isinstance(data, list) and all(isinstance(s, dict) and "id" in s for s in data):
        shapes = {str(s["id"]): {k: v for k, v in s.items() if k != "id"} for s in data}
    else:
        raise _UsageError(f"{spec!r}: expected an object of shapes or a list of shapes with an 'id'")
    if not shapes or not all(isinstance(v, dict) for v in shapes.values()):
        raise _UsageError(f"{spec!r}: no usable shapes")
    return {str(k): v for k, v in shapes.items()}, prov


def _post(url, headers, body, timeout):
    """POST JSON; returns (status or None, error string or None).  Never raises for HTTP error statuses."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read()
            return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            e.read()
        except OSError:
            pass
        return e.code, None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        reason = getattr(e, "reason", e)
        timed_out = isinstance(reason, (socket.timeout, TimeoutError))
        return None, ("timeout" if timed_out else f"connection error: {reason}")


def _breaches(findings, fail_on):
    rank = _TIER_RANK[fail_on]
    if rank == 0:
        return False
    return any(_TIER_RANK.get(f.tier, 9) <= rank for f in findings)


# ---------------------------------------------------------------- driver

def probe(gateway, target, shapes, oracle, headers, model=None, timeout=10.0, sink_port=DEFAULT_SINK_PORT,
          keep_sink=False, log=None):
    """Run the probe.  Returns (per_shape list of dicts, findings by shape id, sink port).
    Raises _UsageError on bind or initial-request connection failure."""
    judge = get_judge(oracle)
    hint, dst_dialect = TARGETS[target]
    url = _inbound_url(gateway)
    try:
        sink = Sink(sink_port)
    except OSError as e:
        raise _UsageError(f"cannot listen on 127.0.0.1:{sink_port}: {e}") from e
    per_shape, all_findings = [], {}
    try:
        for i, (sid, shape) in enumerate(shapes.items()):
            src = {k: v for k, v in shape.items() if k not in _STREAM_KEYS and k != "id"}
            body = json.loads(json.dumps(src))
            if model:
                body["model"] = model
            start = sink.count()
            status, err = _post(url, headers, body, timeout)
            if i == 0 and err is not None and err != "timeout":
                raise _UsageError(f"gateway unreachable at {url}: {err}")
            hit = sink.find(start, hint, GRACE_S)
            rec = {"shape_id": sid, "http_status": status, "reached": hit is not None, "judged": False,
                   "error": err, "upstream_path": hit["path"] if hit else None, "findings": []}
            findings = []
            if hit is not None:
                try:
                    findings = list(judge.compare(src, "anthropic_messages", hit["body"], dst_dialect, "request",
                                                  context={"object_id": sid}))
                    rec["judged"] = True
                except Exception as e:  # noqa: BLE001 - one bad wire body must not stop the run
                    rec["error"] = f"judge error: {e}"
                rec["upstream_body"] = hit["body"]
            rec["findings"] = [_finding_dict(f) for f in findings]
            rec["tiers"] = {t: sum(1 for f in findings if f.tier == t) for t in TIERS}
            per_shape.append(rec)
            all_findings[sid] = findings
            if log:
                log(rec)
        if keep_sink:
            print(f"sink stays up on 127.0.0.1:{sink.port} (Ctrl-C to stop)", file=sys.stderr)
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
    finally:
        sink.close()
    return per_shape, all_findings, sink.port


def summarise(per_shape):
    n = len(per_shape)
    reached = sum(1 for r in per_shape if r["reached"])
    judged = sum(1 for r in per_shape if r["judged"])
    cells = {t: sum(1 for r in per_shape if r["tiers"][t] > 0) for t in TIERS}
    return {"shapes": n, "reached": reached, "judged": judged, "no_wire": n - reached,
            "t1_cells": cells["T1"], "t2_cells": cells["T2"], "t3_cells": cells["T3"]}


def add_arguments(p):
    p.add_argument("--gateway", required=True, metavar="URL",
                   help="gateway base URL; the Anthropic inbound path is derived from it (no path: /v1/messages; "
                        "a path ending in /messages is used as is; otherwise /v1/messages is appended)")
    p.add_argument("--target", choices=sorted(TARGETS), required=True,
                   help="upstream dialect the gateway is configured to translate to")
    p.add_argument("--src-dialect", choices=["anthropic"], default="anthropic",
                   help="inbound dialect of the shapes (only anthropic)")
    p.add_argument("--shapes", default="bundled", metavar="bundled|PATH",
                   help="request shapes: the 51 bundled Anthropic Messages shapes, or a JSON file")
    p.add_argument("--sink-port", type=int, default=DEFAULT_SINK_PORT,
                   help="loopback port of the upstream sink (default %(default)s; 0 = any free port); "
                        "point the gateway's upstream base URL at http://127.0.0.1:<port>")
    p.add_argument("--model", help="replace the posted model name (the judge still sees the original shape)")
    p.add_argument("--header", action="append", metavar="'Name: value'",
                   help="extra request header for the gateway (repeatable; replaces a default of the same name; "
                        "defaults: content-type, anthropic-version 2023-06-01, x-api-key and authorization = probe)")
    p.add_argument("--oracle", choices=sorted(ORACLES), default=default_oracle(),
                   help="judge version (default %(default)s; env CONVPRESERVE_ORACLE)")
    p.add_argument("--fail-on", choices=["T1", "T2", "T3", "none"], default="T1")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--out", metavar="FILE", help="write one JSONL line per shape (verdicts and captured upstream body)")
    p.add_argument("--timeout", type=float, default=10.0, help="per-request timeout in seconds (default %(default)s)")
    p.add_argument("--keep-sink", action="store_true", help="keep the sink listening after the run until Ctrl-C")


def run(args):
    try:
        if args.timeout <= 0:
            raise _UsageError("--timeout must be positive")
        if not 0 <= args.sink_port <= 65535:
            raise _UsageError("--sink-port must be in 0..65535")
        headers = _parse_headers(args.header)
        shapes, prov = _load_shapes(args.shapes)
        _inbound_url(args.gateway)
        quiet = args.json
        per_shape, findings, port = probe(
            args.gateway, args.target, shapes, args.oracle, headers, model=args.model, timeout=args.timeout,
            sink_port=args.sink_port, keep_sink=args.keep_sink)
        s = summarise(per_shape)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                for r in per_shape:
                    fh.write(json.dumps(r, sort_keys=True) + "\n")
    except _UsageError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2
    breach = any(_breaches(f, args.fail_on) for f in findings.values())
    if args.json:
        out = {"gateway": args.gateway, "inbound_url": _inbound_url(args.gateway), "target": args.target,
               "oracle": args.oracle, "convpreserve": __version__, "sink_port": port, "fail_on": args.fail_on,
               "model": args.model}
        out.update(prov)
        out.update(s)
        out["per_shape"] = [{k: v for k, v in r.items() if k != "upstream_body"} for r in per_shape]
        print(json.dumps(out, indent=2))
    else:
        for r in per_shape:
            if not r["reached"]:
                note = r["error"] or (f"HTTP {r['http_status']}" if r["http_status"] is not None else "no answer")
                print(f"no wire: {r['shape_id']} ({note})", file=sys.stderr)
            elif r["error"]:
                print(f"error: {r['shape_id']} ({r['error']})", file=sys.stderr)
        for r in per_shape:
            for f in findings[r["shape_id"]]:
                col = _evidence_col(f)
                print(f"{r['shape_id']}\t{f.rule_id}\t{f.tier}\t{f.object_id}\t{f.detail}" + (f"\t{col}" if col else ""))
        print(f"T1 cells: {s['t1_cells']}/{s['shapes']} (judged {s['judged']}/{s['shapes']}, no wire {s['no_wire']})")
    return 1 if breach else 0
