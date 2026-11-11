"""`convpreserve probe`: loopback sink, shape posting, request-side verdicts, exit codes.

Everything runs on 127.0.0.1 against in-process toy gateways; no external network and no repository checkout
are needed (the 51 shapes ship inside the package).  The toy gateway accepts an Anthropic Messages POST and
forwards a deliberately lossy OpenAI Chat body to the sink (text only; system prompt, stop sequences, tools
and sampling controls are dropped), so the expected verdicts are deterministic.
"""
import contextlib
import io
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import request as urlreq

from convpreserve import cli, probe

ANTH_REPLY = {"id": "m", "type": "message", "role": "assistant", "model": "m",
              "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
              "usage": {"input_tokens": 1, "output_tokens": 1}}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _text(content):
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content


class ToyGateway:
    """mode: 'openai' (forward lossy chat body), 'gemini' (forward a generateContent body), 'mute' (answer, forward nothing)."""

    def __init__(self, sink_port, mode="openai"):
        self.sink = f"http://127.0.0.1:{sink_port}"
        self.mode = mode
        self.requests = []     # (path, lowercase headers, body) as received from the probe
        gw = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n))
                gw.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                msgs = [{"role": m["role"], "content": _text(m["content"])} for m in body.get("messages", [])]
                if gw.mode == "openai":
                    out = {"model": body["model"], "messages": msgs, "max_tokens": body.get("max_tokens")}
                    gw._forward("/v1/chat/completions", out)
                elif gw.mode == "gemini":
                    out = {"contents": [{"role": "user" if m["role"] == "user" else "model",
                                         "parts": [{"text": m["content"]}]} for m in msgs]}
                    gw._forward("/v1beta/models/g:generateContent", out)
                data = json.dumps(ANTH_REPLY).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _forward(self, path, body):
        req = urlreq.Request(self.sink + path, json.dumps(body).encode(), {"content-type": "application/json"})
        urlreq.build_opener(urlreq.ProxyHandler({})).open(req, timeout=5).read()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
            rc = 0
        except SystemExit as e:
            rc = e.code
    return rc, out.getvalue(), err.getvalue()


SIMPLE = {"model": "claude-3-5-sonnet-20241022", "max_tokens": 50,
          "messages": [{"role": "user", "content": "hello"}]}
WITH_SYSTEM = {"model": "claude-3-5-sonnet-20241022", "max_tokens": 50, "system": "Be brief.",
               "temperature": 0.2, "messages": [{"role": "user", "content": "hello"}]}


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.port = _free_port()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def gateway(self, mode="openai"):
        gw = ToyGateway(self.port, mode)
        self.addCleanup(gw.close)
        return gw

    def shapes_file(self, shapes):
        path = os.path.join(self.tmp.name, "shapes.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(shapes, f)
        return path

    def probe_args(self, gw, *extra, target="openai", oracle="v1.0"):
        return ["probe", "--gateway", gw.url, "--target", target, "--sink-port", str(self.port),
                "--oracle", oracle, *extra]

    # ---- bundled shapes

    def test_bundled_51_lossy_gateway(self):
        for oracle, t1 in (("v1.0", 51), ("v1.3", 51)):
            with self.subTest(oracle=oracle):
                self.port = _free_port()
                gw = self.gateway()
                rc, out, err = run(self.probe_args(gw, "--json", oracle=oracle))
                self.assertEqual(rc, 1, err)
                d = json.loads(out)
                self.assertEqual((d["shapes"], d["reached"], d["judged"], d["no_wire"]), (51, 51, 51, 0))
                self.assertEqual(d["t1_cells"], t1)
                self.assertEqual(len(d["per_shape"]), 51)
                self.assertEqual(d["oracle"], oracle)
                self.assertEqual(d["target"], "openai")
                self.assertEqual(d["source_shapes_sha256"],
                                 "af6a4d7597846287e9bfcae12c1ea04362f1e865f9260cc1f4000cdd19ae0678")
                self.assertEqual(d["sink_port"], self.port)
                self.assertTrue(all(r["http_status"] == 200 for r in d["per_shape"]))
                rules = {f["rule_id"] for r in d["per_shape"] for f in r["findings"]}
                self.assertTrue({"S05", "S08", "S09"} <= rules, rules)

    def test_text_output_and_summary_line(self):
        gw = self.gateway()
        rc, out, err = run(self.probe_args(gw))
        self.assertEqual(rc, 1)
        lines = out.splitlines()
        self.assertEqual(lines[-1], "T1 cells: 51/51 (judged 51/51, no wire 0)")
        row = lines[0].split("\t")
        self.assertGreaterEqual(len(row), 5)
        self.assertIn(row[2], ("T1", "T2", "T3"))
        self.assertEqual(row[0], row[3])      # shape id is also the object id
        self.assertEqual(err, "")

    def test_out_jsonl_and_model_override(self):
        gw = self.gateway()
        outp = os.path.join(self.tmp.name, "findings.jsonl")
        rc, out, err = run(self.probe_args(gw, "--out", outp, "--model", "openaichat/gpt-4o", "--json"))
        self.assertEqual(rc, 1, err)
        with open(outp, encoding="utf-8") as f:
            recs = [json.loads(line) for line in f]
        self.assertEqual(len(recs), 51)
        self.assertEqual({r["upstream_body"]["model"] for r in recs}, {"openaichat/gpt-4o"})
        self.assertEqual({b["model"] for _, _, b in gw.requests}, {"openaichat/gpt-4o"})
        self.assertEqual(recs[0]["shape_id"], json.loads(out)["per_shape"][0]["shape_id"])

    # ---- custom shapes, rule-level behaviour

    def test_clean_pass_exit_0(self):
        gw = self.gateway()
        path = self.shapes_file({"S1": SIMPLE})
        rc, out, err = run(self.probe_args(gw, "--shapes", path))
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(out.strip(), "T1 cells: 0/1 (judged 1/1, no wire 0)")

    def test_dropped_controls_are_reported_and_fail_on_levels(self):
        gw = self.gateway()
        path = self.shapes_file({"S2": WITH_SYSTEM})
        rc, out, err = run(self.probe_args(gw, "--shapes", path, "--json"))
        d = json.loads(out)
        rules = sorted((f["rule_id"], f["tier"]) for f in d["per_shape"][0]["findings"])
        self.assertIn(("S05", "T1"), rules)           # system text lost
        self.assertIn(("S08", "T1"), rules)           # temperature dropped
        self.assertEqual((rc, d["t1_cells"], d["t2_cells"], d["t3_cells"]), (1, 1, 0, 0))
        rc, _, _ = run(self.probe_args(gw, "--shapes", path, "--fail-on", "none"))
        self.assertEqual(rc, 0)

    def test_fail_on_t3_clean_run(self):
        gw = self.gateway()
        path = self.shapes_file({"S1": SIMPLE})
        rc, _, _ = run(self.probe_args(gw, "--shapes", path, "--fail-on", "T3"))
        self.assertEqual(rc, 0)

    def test_judge_sees_original_shape_not_overridden_model(self):
        gw = self.gateway()
        path = self.shapes_file({"S1": SIMPLE})
        rc, out, err = run(self.probe_args(gw, "--shapes", path, "--model", "other-model", "--json"))
        self.assertEqual(json.loads(out)["per_shape"][0]["findings"], [])
        self.assertEqual(rc, 0)

    def test_stream_keys_dropped_from_posted_body(self):
        gw = self.gateway()
        path = self.shapes_file({"S1": dict(SIMPLE, stream=True)})
        rc, out, err = run(self.probe_args(gw, "--shapes", path))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("stream", gw.requests[0][2])

    # ---- targets and routing

    def test_gemini_target(self):
        gw = self.gateway("gemini")
        path = self.shapes_file({"S1": SIMPLE})
        rc, out, err = run(self.probe_args(gw, "--shapes", path, "--json", target="gemini"))
        d = json.loads(out)
        self.assertEqual((d["reached"], d["judged"]), (1, 1))
        self.assertTrue(d["per_shape"][0]["upstream_path"].endswith(":generateContent"))

    def test_wrong_target_means_no_wire(self):
        gw = self.gateway("openai")
        path = self.shapes_file({"S1": SIMPLE})
        rc, out, err = run(self.probe_args(gw, "--shapes", path, "--json", target="gemini"))
        d = json.loads(out)
        self.assertEqual((d["reached"], d["judged"], d["no_wire"], rc), (0, 0, 1, 0))

    def test_mute_gateway_no_wire(self):
        gw = self.gateway("mute")
        rc, out, err = run(self.probe_args(gw))
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "T1 cells: 0/51 (judged 0/51, no wire 51)")
        self.assertEqual(len([l for l in err.splitlines() if l.startswith("no wire: ")]), 51)

    # ---- request construction

    def test_default_and_custom_headers(self):
        gw = self.gateway()
        path = self.shapes_file({"S1": SIMPLE})
        run(self.probe_args(gw, "--shapes", path, "--header", "X-Api-Key: sk-test", "--header", "x-extra:  1"))
        _, h, _ = gw.requests[0]
        self.assertEqual(h["x-api-key"], "sk-test")
        self.assertEqual(h["x-extra"], "1")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(h["authorization"], "Bearer probe")
        self.assertEqual(h["content-type"], "application/json")

    def test_inbound_path_from_gateway_url(self):
        self.assertEqual(probe._inbound_url("http://127.0.0.1:1"), "http://127.0.0.1:1/v1/messages")
        self.assertEqual(probe._inbound_url("http://127.0.0.1:1/"), "http://127.0.0.1:1/v1/messages")
        self.assertEqual(probe._inbound_url("http://127.0.0.1:1/anthropic"), "http://127.0.0.1:1/anthropic/v1/messages")
        self.assertEqual(probe._inbound_url("http://h/anthropic/v1/messages"), "http://h/anthropic/v1/messages")
        gw = self.gateway()
        path = self.shapes_file({"S1": SIMPLE})
        run(["probe", "--gateway", gw.url + "/anthropic", "--target", "openai", "--sink-port", str(self.port),
             "--shapes", path])
        self.assertEqual(gw.requests[0][0], "/anthropic/v1/messages")

    def test_sink_is_loopback_only(self):
        sink = probe.Sink(0)
        try:
            self.assertEqual(sink._server.server_address[0], "127.0.0.1")
        finally:
            sink.close()

    # ---- errors

    def test_gateway_unreachable_exit_2(self):
        rc, out, err = run(["probe", "--gateway", f"http://127.0.0.1:{_free_port()}", "--target", "openai",
                            "--sink-port", str(self.port)])
        self.assertEqual(rc, 2)
        self.assertIn("unreachable", err)
        self.assertEqual(out, "")

    def test_sink_port_in_use_exit_2(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        rc, out, err = run(["probe", "--gateway", "http://127.0.0.1:9", "--target", "openai",
                            "--sink-port", str(s.getsockname()[1])])
        self.assertEqual(rc, 2)
        self.assertIn("cannot listen", err)

    def test_usage_errors_exit_2(self):
        gw = self.gateway()
        base = self.probe_args(gw)
        for extra in (["--header", "no-colon"], ["--shapes", os.path.join(self.tmp.name, "missing.json")],
                      ["--timeout", "0"]):
            with self.subTest(extra=extra):
                rc, out, err = run(base + extra)
                self.assertEqual(rc, 2)
                self.assertTrue(err.startswith("Error: "))
        rc, _, err = run(["probe", "--gateway", "ftp://x", "--target", "openai"])
        self.assertEqual(rc, 2)

    def test_listed_in_help(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cli.main(["--help"])
        self.assertIn("probe", out.getvalue())


if __name__ == "__main__":
    unittest.main()
