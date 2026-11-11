"""Positive / negative coverage for the seven rules that the bundled selftest sample
(`convpreserve selftest`, 400 records) never fires: S01, S11, D01, D06, R02, R06, R07.

Each rule has a minimal pair that fires it and a clean control (identity or documented-equivalent
pair) that must not.  The expected values pin the vendored oracle (v1.0) as it is; nothing here
changes a verdict.  `TestKnownLimitation` documents one behaviour of the frozen oracle that a
reviewer should know about (stream-side `reasoning_content` is not assembled).
"""
import copy
import unittest

from convpreserve.judge import judge

A, O = "anthropic_messages", "openai_chat"


def rules(src, sd, dst, dd, side="request"):
    return {(f.rule_id, f.tier) for f in judge.compare(src, sd, dst, dd, side, context={})}


def tool_a(props):
    return {"name": "get_weather", "description": "w",
            "input_schema": {"type": "object", "properties": props, "required": ["city"]}}


def tool_o(props):
    return {"type": "function", "function": {"name": "get_weather", "description": "w",
            "parameters": {"type": "object", "properties": props, "required": ["city"]}}}


def req_a(**kw):
    r = {"model": "m", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    r.update(kw)
    return r


def req_o(**kw):
    r = {"model": "m", "max_tokens": 64, "messages": [{"role": "user", "content": "hi"}]}
    r.update(kw)
    return r


def two_tool_calls_a():
    return {"model": "m", "max_tokens": 64, "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "f", "input": {}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "A"},
            {"type": "tool_result", "tool_use_id": "t2", "content": "B"}]}]}


def two_tool_calls_o():
    call = lambda i: {"id": i, "type": "function", "function": {"name": "f", "arguments": "{}"}}
    return {"model": "m", "max_tokens": 64, "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": [call("t1"), call("t2")]},
        {"role": "tool", "tool_call_id": "t1", "content": "A"},
        {"role": "tool", "tool_call_id": "t2", "content": "B"}]}


def anth_response():
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "m",
            "content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 2}}


class TestRuleCoverage(unittest.TestCase):
    def test_s01_merge(self):
        s = {"model": "m", "messages": [{"role": "user", "content": "alpha"},
                                        {"role": "assistant", "content": "beta"},
                                        {"role": "user", "content": "gamma"}]}
        merged = {"model": "m", "messages": [{"role": "user", "content": "alpha\nbeta\ngamma"}]}
        self.assertIn(("S01", "T1"), rules(s, O, merged, O))
        self.assertEqual(rules(s, O, copy.deepcopy(s), O), set())

    def test_s11_schema_type_changed(self):
        src = req_a(tools=[tool_a({"city": {"type": "string"}})])
        bad = req_o(tools=[tool_o({"city": {"type": "integer"}})])
        ok = req_o(tools=[tool_o({"city": {"type": "string"}})])
        self.assertIn(("S11", "T1"), rules(src, A, bad, O))
        self.assertEqual(rules(src, A, ok, O), set())

    def test_d01_adapter_default_injected(self):
        src = req_a()
        for key, val in (("seed", 7), ("top_k", 40), ("parallel_tool_calls", True)):
            with self.subTest(control=key):
                self.assertIn(("D01", "T2"), rules(src, A, req_o(**{key: val}), O))
        self.assertEqual(rules(src, A, req_o(), O), set())

    def test_d06_envelope_leak(self):
        r = anth_response()
        usage_dup = copy.deepcopy(r)
        usage_dup["usage"]["stop_reason"] = "end_turn"
        foreign = copy.deepcopy(r)
        foreign["object"] = "chat.completion"
        self.assertIn(("D06", "T2"), rules(r, A, usage_dup, A, "response"))
        self.assertIn(("D06", "T2"), rules(r, A, foreign, A, "response"))
        self.assertEqual(rules(r, A, copy.deepcopy(r), A, "response"), set())

    def test_r02_null_vs_empty_content(self):
        call = {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        s = {"model": "m", "messages": [{"role": "user", "content": "hi"},
                                        {"role": "assistant", "content": None, "tool_calls": [call]},
                                        {"role": "tool", "tool_call_id": "c1", "content": "A"}]}
        d = copy.deepcopy(s)
        d["messages"][1]["content"] = ""
        self.assertIn(("R02", "T3"), rules(s, O, d, O))
        self.assertEqual(rules(s, O, copy.deepcopy(s), O), set())

    def test_r06_schema_annotation_changed(self):
        src = req_a(tools=[tool_a({"city": {"type": "string", "description": "City name"}})])
        bad = req_o(tools=[tool_o({"city": {"type": "string", "description": "the city"}})])
        ok = req_o(tools=[tool_o({"city": {"type": "string", "description": "City name"}})])
        self.assertIn(("R06", "T3"), rules(src, A, bad, O))
        self.assertEqual(rules(src, A, ok, O), set())

    def test_r07_parallel_result_order(self):
        s, ok = two_tool_calls_a(), two_tool_calls_o()
        swapped = copy.deepcopy(ok)
        swapped["messages"][2], swapped["messages"][3] = swapped["messages"][3], swapped["messages"][2]
        self.assertIn(("R07", "T3"), rules(s, A, swapped, O))
        self.assertEqual(rules(s, A, ok, O), set())


class TestKnownLimitation(unittest.TestCase):
    def test_stream_reasoning_content_not_assembled(self):
        """`_parse_openai_chat_stream` assembles only `delta.content` and `delta.tool_calls`.
        An upstream OpenAI-chat stream that carries `delta.reasoning_content` therefore yields no
        thinking event on the source side, and a thinking block on the destination side is
        reported as S05 (invented).  The non-stream path (`message.reasoning_content`) is
        handled and is clean.  This test pins the behaviour of oracle v1.0 so a future oracle
        change shows up here; see AUDIT_2026-10-15.md, item S05.  Oracle v1.3 assembles the stream
        carriers; tests/test_oracle_v13.py pins that."""
        chunk = lambda d, fin=None: ("data: " + __import__("json").dumps(
            {"id": "c1", "object": "chat.completion.chunk", "model": "m",
             "choices": [{"index": 0, "delta": d, "finish_reason": fin}]}) + "\n\n")
        up = (chunk({"role": "assistant", "reasoning_content": "let me think"})
              + chunk({"content": "hi"}) + chunk({}, "stop") + "data: [DONE]\n\n")

        def ev(i, typ, d):
            return "event: %s\ndata: %s\n\n" % (typ, __import__("json").dumps(d))
        dn = (ev(0, "message_start", {"type": "message_start", "message": {
                  "id": "c1", "type": "message", "role": "assistant", "content": [], "model": "m",
                  "stop_reason": None, "stop_sequence": None,
                  "usage": {"input_tokens": 0, "output_tokens": 0}}})
              + ev(0, "content_block_start", {"type": "content_block_start", "index": 0,
                   "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
              + ev(0, "content_block_delta", {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "thinking_delta", "thinking": "let me think"}})
              + ev(0, "content_block_stop", {"type": "content_block_stop", "index": 0})
              + ev(0, "content_block_start", {"type": "content_block_start", "index": 1,
                   "content_block": {"type": "text", "text": ""}})
              + ev(0, "content_block_delta", {"type": "content_block_delta", "index": 1,
                   "delta": {"type": "text_delta", "text": "hi"}})
              + ev(0, "content_block_stop", {"type": "content_block_stop", "index": 1})
              + ev(0, "message_delta", {"type": "message_delta",
                   "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                   "usage": {"output_tokens": 2}})
              + ev(0, "message_stop", {"type": "message_stop"}))
        got = rules(up, O, dn, A, "stream")
        self.assertIn(("S05", "T1"), got)


if __name__ == "__main__":
    unittest.main()
