#!/usr/bin/env python3
"""Generate src/convpreserve/data/selftest_rule_cases.json.gz: at least three positive and three negative
cases for each of the 27 rules (S01-S13, D01-D06, R01-R08).  Used by `convpreserve selftest`.

Method (see tests/README.md):
  positives  the smallest bundled-sample records (selftest_sample.json.gz) whose frozen v1.0 findings contain the rule,
             up to three; where the sample has fewer than three, hand-written minimal pairs (below) fill the gap.
  negatives  for rules with hand-written pairs: the documented-equivalent pairs below; for every rule: an identity pair
             built from the leading positive's source payload (skipped when the oracles report a finding on it, which happens
             for a few sample fixtures that are not well-formed payloads of their side), then the smallest clean sample
             records (no v1.0 finding) on a side where the rule can fire, until three are reached.
  expect     the full set of rule ids returned by each oracle on the case (v1.0 and v1.3), recorded from the judges; the
             generator refuses to write a case whose target rule is absent (positive) or present (negative).
Nothing here changes a verdict; `expect` pins what the oracles do.
usage: python tests/gen_rule_cases.py [--check]   (--check: regenerate in memory and compare with the committed file)
"""
import argparse
import copy
import gzip
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
sys.path.insert(0, str(SRC))
from convpreserve.oracles import get_judge  # noqa: E402

DATA = SRC / "convpreserve" / "data"
A, O, R, G = "anthropic_messages", "openai_chat", "openai_responses", "gemini_generatecontent"
RULES = [f"S{i:02d}" for i in range(1, 14)] + [f"D{i:02d}" for i in range(1, 7)] + [f"R{i:02d}" for i in range(1, 9)]
SIDES = {"S01": "request", "S02": "request", "S03": "request", "S04": "response", "S05": "request", "S06": "request",
         "S07": "request", "S08": "request", "S09": "request", "S10": "request", "S11": "request", "S12": "response",
         "S13": "stream", "D01": "request", "D02": "request", "D03": "request", "D04": "response", "D05": "response",
         "D06": "response", "R01": "response", "R02": "request", "R03": "request", "R04": "response", "R05": "response",
         "R06": "request", "R07": "request", "R08": "response"}


def um(t): return {"role": "user", "content": t}
def am(t): return {"role": "assistant", "content": t}
def case(src, sd, dst, dd, side="request", ctx=None): return dict(src=src, src_dialect=sd, dst=dst, dst_dialect=dd, side=side, context=ctx or {})


def call(i): return {"id": i, "type": "function", "function": {"name": "f", "arguments": "{}"}}
def ra(**kw):
    r = {"model": "m", "max_tokens": 64, "messages": [um("hi")]}
    r.update(kw)
    return r
def tool_a(props, req=("city",), desc="w"):
    return {"name": "get_weather", "description": desc, "input_schema": {"type": "object", "properties": props, "required": list(req)}}
def tool_o(props, req=("city",), desc="w"):
    return {"type": "function", "function": {"name": "get_weather", "description": desc,
            "parameters": {"type": "object", "properties": props, "required": list(req)}}}
def swap(msgs, i, j):
    m = copy.deepcopy(msgs)
    m[i], m[j] = m[j], m[i]
    return m
def two_a(n):
    return {"model": "m", "max_tokens": 64, "messages": [um("hi"),
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "f", "input": {}} for i in range(1, n + 1)]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ABC"[i - 1]} for i in range(1, n + 1)]}]}
def two_o(n, order=None):
    order = order or list(range(1, n + 1))
    return {"model": "m", "max_tokens": 64, "messages": [um("hi"),
            {"role": "assistant", "content": None, "tool_calls": [call(f"t{i}") for i in range(1, n + 1)]}]
            + [{"role": "tool", "tool_call_id": f"t{i}", "content": "ABC"[i - 1]} for i in order]}
def oo(content, turns=1):
    msgs = [um("hi"), {"role": "assistant", "content": content, "tool_calls": [call("c1")]}, {"role": "tool", "tool_call_id": "c1", "content": "A"}]
    if turns == 2:
        msgs += [am("done"), um("ok")]
    return {"model": "m", "messages": msgs}
ANTH_RESP = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": [{"type": "text", "text": "hi"}],
             "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 2}}
def with_(d, f):
    d = copy.deepcopy(d)
    f(d)
    return d


def synthetic():
    """rule -> (positives, negatives); each item is (id, case)."""
    S = {}
    s = {"model": "m", "messages": [um("alpha"), am("beta"), um("gamma")]}
    sa = {"model": "m", "max_tokens": 9, "messages": [um("one"), am("two"), um("three"), am("four"), um("five")]}
    S["S01"] = ([("merge_openai", case(s, O, {"model": "m", "messages": [um("alpha\nbeta\ngamma")]}, O)),
                 ("merge_anthropic_five_turns", case(sa, A, {**sa, "messages": [um("one two three"), am("four"), um("five")]}, A)),
                 ("merge_anthropic_three_turns", case({**sa, "messages": [um("x"), am("y"), um("z")]}, A, {**sa, "messages": [um("x y z")]}, A))],
                [("identity_openai", case(s, O, copy.deepcopy(s), O)),
                 ("identity_anthropic", case(sa, A, copy.deepcopy(sa), A)),
                 ("alternation_kept_anthropic_to_openai", case({**sa, "messages": [um("x"), am("y"), um("z")]}, A, {"model": "m", "max_tokens": 9, "messages": [um("x"), am("y"), um("z")]}, O))])
    S["S02"] = ([("role_flip_openai", case({"model": "m", "messages": [um("hi"), am("hello"), um("weather in Paris?")]}, O,
                                           {"model": "m", "messages": [um("hi"), am("hello"), am("weather in Paris?")]}, O)),
                 ("role_flip_anthropic", case({"model": "m", "max_tokens": 9, "messages": [um("hi"), am("hello"), um("continue")]}, A,
                                              {"model": "m", "max_tokens": 9, "messages": [um("hi"), am("hello"), am("continue")]}, A))], [])
    base = [um("run it"), {"role": "assistant", "content": None, "tool_calls": [call("t1")]},
            {"role": "tool", "tool_call_id": "t1", "content": "result one"}, {"role": "developer", "content": "[harness] shell disabled from now on"}]
    base_sys = copy.deepcopy(base)
    base_sys[3] = {"role": "system", "content": "[harness] policy changed to read only"}
    base_usr = copy.deepcopy(base)
    base_usr[3] = um("[harness] network disabled from now on")
    S["S07"] = ([("developer_notice_moved_before_result", case({"model": "m", "messages": base}, O, {"model": "m", "messages": swap(base, 2, 3)}, O)),
                 ("system_notice_moved_before_result", case({"model": "m", "messages": base_sys}, O, {"model": "m", "messages": swap(base_sys, 2, 3)}, O)),
                 ("user_notice_moved_before_result", case({"model": "m", "messages": base_usr}, O, {"model": "m", "messages": swap(base_usr, 2, 3)}, O))], [])
    S["S08"] = ([("temperature_dropped_anthropic", case(ra(temperature=0.2), A, ra(), A)),
                 ("stop_sequences_dropped_anthropic", case(ra(stop_sequences=["END"]), A, ra(), A)),
                 ("temperature_dropped_openai", case({"model": "m", "temperature": 0.2, "messages": [um("hi")]}, O, {"model": "m", "messages": [um("hi")]}, O))], [])
    S["D02"] = ([("developer_lowered_to_user", case({"model": "m", "messages": [{"role": "developer", "content": "Answer briefly."}, um("hi")]}, O,
                                                     {"model": "m", "messages": [um("Answer briefly."), um("hi")]}, O)),
                 ("system_lowered_to_user", case({"model": "m", "messages": [{"role": "system", "content": "Answer briefly."}, um("hi")]}, O,
                                                  {"model": "m", "messages": [um("Answer briefly."), um("hi")]}, O))], [])
    ar2 = with_(ANTH_RESP, lambda d: d.__setitem__("content", [{"type": "text", "text": ""}] + d["content"]))
    ar3 = with_(ANTH_RESP, lambda d: d.__setitem__("content", d["content"] + [{"type": "text", "text": ""}]))
    S["R05"] = ([("empty_block_prepended", case(ANTH_RESP, A, ar2, A, "response")),
                 ("empty_block_appended", case(ANTH_RESP, A, ar3, A, "response"))], [])
    src = ra(tools=[tool_a({"city": {"type": "string"}})])
    ok = ra(tools=[tool_o({"city": {"type": "string"}})])
    S["S11"] = ([("string_to_integer", case(src, A, ra(tools=[tool_o({"city": {"type": "integer"}})]), O)),
                 ("second_property_string", case(ra(tools=[tool_a({"city": {"type": "string"}, "days": {"type": "integer"}})]), A,
                                                 ra(tools=[tool_o({"city": {"type": "string"}, "days": {"type": "string"}})]), O)),
                 ("enum_value_changed", case(ra(tools=[tool_a({"city": {"type": "string", "enum": ["a", "b"]}})]), A,
                                             ra(tools=[tool_o({"city": {"type": "string", "enum": ["a", "c"]}})]), O))],
                [("same_schema", case(src, A, ok, O)),
                 ("same_schema_two_properties", case(ra(tools=[tool_a({"city": {"type": "string"}, "days": {"type": "integer"}})]), A,
                                                     ra(tools=[tool_o({"city": {"type": "string"}, "days": {"type": "integer"}})]), O)),
                 ("same_enum", case(ra(tools=[tool_a({"city": {"type": "string", "enum": ["a", "b"]}})]), A,
                                    ra(tools=[tool_o({"city": {"type": "string", "enum": ["a", "b"]}})]), O))])
    S["D01"] = ([(f"injected_{k}", case(ra(), A, ra(**{k: v}), O)) for k, v in (("seed", 7), ("top_k", 40), ("parallel_tool_calls", True))],
                [("nothing_injected", case(ra(), A, ra(), O)),
                 ("nothing_injected_with_temperature", case(ra(temperature=0.2), A, {"model": "m", "max_tokens": 64, "temperature": 0.2, "messages": [um("hi")]}, O)),
                 ("identity_anthropic", case(ra(), A, ra(), A))])
    S["D06"] = ([("usage_stop_reason", case(ANTH_RESP, A, with_(ANTH_RESP, lambda d: d["usage"].__setitem__("stop_reason", "end_turn")), A, "response")),
                 ("foreign_envelope_object", case(ANTH_RESP, A, with_(ANTH_RESP, lambda d: d.__setitem__("object", "chat.completion")), A, "response")),
                 ("foreign_envelope_system_fingerprint", case(ANTH_RESP, A, with_(ANTH_RESP, lambda d: d.__setitem__("system_fingerprint", "fp_1")), A, "response"))],
                [("identity_response", case(ANTH_RESP, A, copy.deepcopy(ANTH_RESP), A, "response")),
                 ("identity_response_with_stop_sequence", case(with_(ANTH_RESP, lambda d: d.__setitem__("stop_sequence", "END")), A,
                                                              with_(ANTH_RESP, lambda d: d.__setitem__("stop_sequence", "END")), A, "response")),
                 ("identity_response_longer_text", case(with_(ANTH_RESP, lambda d: d["content"][0].__setitem__("text", "a longer answer")), A,
                                                       with_(ANTH_RESP, lambda d: d["content"][0].__setitem__("text", "a longer answer")), A, "response"))])
    S["R02"] = ([("null_to_empty_string", case(oo(None), O, oo(""), O)),
                 ("empty_string_to_null", case(oo(""), O, oo(None), O)),
                 ("null_to_empty_string_two_turns", case(oo(None, 2), O, oo("", 2), O))],
                [("identity_null", case(oo(None), O, oo(None), O)),
                 ("identity_empty", case(oo(""), O, oo(""), O)),
                 ("empty_without_tool_call", case({"model": "m", "messages": [um("hi"), am(""), um("more")]}, O,
                                                  {"model": "m", "messages": [um("hi"), {"role": "assistant", "content": None}, um("more")]}, O))])
    S["R06"] = ([("description_changed", case(ra(tools=[tool_a({"city": {"type": "string", "description": "City name"}})]), A,
                                              ra(tools=[tool_o({"city": {"type": "string", "description": "the city"}})]), O)),
                 ("description_dropped", case(ra(tools=[tool_a({"city": {"type": "string", "description": "x"}})]), A,
                                              ra(tools=[tool_o({"city": {"type": "string"}})]), O)),
                 ("default_changed", case(ra(tools=[tool_a({"city": {"type": "string", "default": "Paris"}})]), A,
                                          ra(tools=[tool_o({"city": {"type": "string", "default": "Rome"}})]), O))],
                [("same_description", case(ra(tools=[tool_a({"city": {"type": "string", "description": "City name"}})]), A,
                                           ra(tools=[tool_o({"city": {"type": "string", "description": "City name"}})]), O)),
                 ("same_default", case(ra(tools=[tool_a({"city": {"type": "string", "default": "Paris"}})]), A,
                                       ra(tools=[tool_o({"city": {"type": "string", "default": "Paris"}})]), O)),
                 ("no_annotation", case(src, A, ok, O))])
    S["R07"] = ([("two_results_swapped", case(two_a(2), A, two_o(2, [2, 1]), O)),
                 ("three_results_reversed", case(two_a(3), A, two_o(3, [3, 2, 1]), O)),
                 ("three_results_last_two_swapped", case(two_a(3), A, two_o(3, [1, 3, 2]), O))],
                [("two_results_in_order", case(two_a(2), A, two_o(2), O)),
                 ("three_results_in_order", case(two_a(3), A, two_o(3), O)),
                 ("openai_identity", case(two_o(2), O, two_o(2), O))])
    return S


def blob_len(r):
    return len(json.dumps([r["src_body"], r["dst_body"]], sort_keys=True))


def run(version, c):
    judge = get_judge(version)
    return sorted({f.rule_id for f in judge.compare(c["src"], c["src_dialect"], c["dst"], c["dst_dialect"], c["side"], context=c["context"])})


def build():
    recs = json.load(gzip.open(DATA / "selftest_sample.json.gz", "rt", encoding="utf-8"))
    syn = synthetic()
    clean = sorted((r for r in recs if not r["findings"]), key=lambda r: (blob_len(r), json.dumps(r["src_body"], sort_keys=True)))
    out, problems = [], []
    for ri, rule in enumerate(RULES):
        # positives
        pos = []
        hits = sorted((r for r in recs if any(f["rule_id"] == rule for f in r["findings"])),
                      key=lambda r: (blob_len(r), json.dumps(r["src_body"], sort_keys=True)))
        for r in hits[:3]:
            pos.append(("sample_record", dict(src=r["src_body"], src_dialect=r["src_dialect"], dst=r["dst_body"], dst_dialect=r["dst_dialect"],
                                              side=r["side"], context=r.get("context") or {})))
        syn_pos, syn_neg = syn.get(rule, ([], []))
        for name, c in syn_pos:
            if len(pos) < 3 or rule in ("S02", "S07", "S08", "D02", "R05"):
                pos.append((name, c))
        # negatives
        neg = list(syn_neg)
        lead = pos[0][1]
        neg_side = lead["side"]
        ident = dict(src=lead["src"], src_dialect=lead["src_dialect"], dst=copy.deepcopy(lead["src"]),
                     dst_dialect=lead["src_dialect"], side=lead["side"], context=lead["context"])
        if not run("v1.0", ident) and not run("v1.3", ident):  # skipped when the source is not a well-formed payload of its side
            neg.append(("identity_of_leading_positive_source", ident))
        k = 0
        pool = [r for r in clean if r["side"] == neg_side]
        while len(neg) < 3 and k < len(pool):
            r = pool[(ri * 5 + k) % len(pool)]
            neg.append(("clean_sample_record", dict(src=r["src_body"], src_dialect=r["src_dialect"], dst=r["dst_body"], dst_dialect=r["dst_dialect"],
                                                    side=r["side"], context=r.get("context") or {})))
            k += 1
        for kind, items in (("positive", pos), ("negative", neg)):
            for n, (name, c) in enumerate(items, 1):
                expect = {v: run(v, c) for v in ("v1.0", "v1.3")}
                for v, got in expect.items():
                    if (rule in got) != (kind == "positive"):
                        problems.append(f"{rule} {kind} {name}: oracle {v} gave {got}")
                e = {"all": expect["v1.0"]} if expect["v1.0"] == expect["v1.3"] else expect
                out.append({"id": f"{rule}/{kind[:3]}{n}/{name}", "rule": rule, "kind": kind, **c, "expect": e})
    return out, problems


def dumps(cases):
    return json.dumps(cases, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    cases, problems = build()
    if problems:
        print("\n".join(problems))
        return 1
    path = DATA / "selftest_rule_cases.json.gz"
    raw = dumps(cases)
    counts = {}
    for c in cases:
        counts.setdefault(c["rule"], {"positive": 0, "negative": 0})[c["kind"]] += 1
    if a.check:
        old = gzip.open(path, "rb").read()
        print("identical" if old == raw else "DIFFERENT")
        return 0 if old == raw else 1
    with open(path, "wb") as f:
        with gzip.GzipFile(fileobj=f, mode="wb", mtime=0) as g:
            g.write(raw)
    print(len(cases), "cases", "sha256", hashlib.sha256(path.read_bytes()).hexdigest())
    for r in RULES:
        print(r, counts[r])
    v = [c for c in cases if "all" not in c["expect"]]
    print("cases where v1.0 and v1.3 expectations differ:", len(v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
