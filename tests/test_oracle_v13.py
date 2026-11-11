"""Oracle v1.3 (convpreserve.judge_v13) against the frozen Oracle v1.0 (convpreserve.judge).

v1.3 = v1.0 plus (a) streamed OpenAI Chat `reasoning_content` / `reasoning` deltas are assembled like the
non-stream message fields, (b) every finding carries an evidence pointer, (c) the S10 ablation text is reworded.
The tests below pin each of the three, pin the judge_v13 sources (judge_v13/SHA256SUMS), and run the FP-4
regression on committed reverse-census captures when the repository checkout is present.
"""
import copy
import gzip
import hashlib
import importlib
import json
import os
import pathlib
import subprocess
import sys
import unittest

from convpreserve.judge import judge as J10
from convpreserve.judge_v13 import carriers as C13
from convpreserve.judge_v13 import evidence as EV
from convpreserve.judge_v13 import judge as J13
from convpreserve.judge_v13.ir import parse_ir

HERE = pathlib.Path(__file__).resolve().parent
PKG = HERE.parent / "src" / "convpreserve"
PACK = HERE.parent.parent.parent
A, O, G = "anthropic_messages", "openai_chat", "gemini_generatecontent"


def keys(findings):
    return sorted((f.rule_id, f.tier, f.detail) for f in findings)


def chunk(delta, fin=None):
    return "data: " + json.dumps({"id": "c1", "object": "chat.completion.chunk", "model": "m",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}) + "\n\n"


def openai_stream(reasoning, key="reasoning_content", fragments=1, content="hi"):
    """OpenAI Chat SSE with `reasoning` streamed under `key` in `fragments`-sized pieces, then `content`."""
    parts = [reasoning[i:i + fragments] for i in range(0, len(reasoning), fragments)] if reasoning else []
    out = chunk({"role": "assistant"})
    for p in parts:
        out += chunk({key: p})
    out += chunk({"content": content}) + chunk({}, "stop") + "data: [DONE]\n\n"
    return out


def anthropic_stream(thinking, text="hi"):
    def ev(typ, d):
        return "event: %s\ndata: %s\n\n" % (typ, json.dumps(d))
    s = ev("message_start", {"type": "message_start", "message": {
        "id": "c1", "type": "message", "role": "assistant", "content": [], "model": "m", "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    i = 0
    if thinking is not None:
        s += ev("content_block_start", {"type": "content_block_start", "index": i,
                                        "content_block": {"type": "thinking", "thinking": "", "signature": ""}})
        s += ev("content_block_delta", {"type": "content_block_delta", "index": i,
                                        "delta": {"type": "thinking_delta", "thinking": thinking}})
        s += ev("content_block_stop", {"type": "content_block_stop", "index": i})
        i += 1
    s += ev("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}})
    s += ev("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": text}})
    s += ev("content_block_stop", {"type": "content_block_stop", "index": i})
    s += ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                              "usage": {"output_tokens": 2}})
    s += ev("message_stop", {"type": "message_stop"})
    return s


def s05(findings):
    return [f for f in findings if f.rule_id == "S05"]


class TestStreamReasoning(unittest.TestCase):
    """Change (a): the stream parser reads the same reasoning carriers as the non-stream parser."""

    def test_forward_openai_stream_to_anthropic(self):
        up, dn = openai_stream("let me think"), anthropic_stream("let me think")
        old = J10.compare(up, O, dn, A, "stream", context={})
        new = J13.compare(up, O, dn, A, "stream", context={})
        self.assertTrue(any("invented assistant thinking" in f.detail for f in s05(old)))
        self.assertEqual(s05(new), [])
        self.assertEqual(keys([f for f in old if f.rule_id != "S05"]), keys(new))

    def test_reverse_anthropic_stream_to_openai(self):
        up, dn = anthropic_stream("let me think"), openai_stream("let me think")
        old = J10.compare(up, A, dn, O, "stream", context={})
        new = J13.compare(up, A, dn, O, "stream", context={})
        self.assertTrue(any("lost assistant thinking" in f.detail for f in s05(old)))
        self.assertEqual(s05(new), [])

    def test_reasoning_key_is_read_like_reasoning_content(self):
        up, dn = anthropic_stream("let me think"), openai_stream("let me think", key="reasoning")
        self.assertEqual(s05(J13.compare(up, A, dn, O, "stream", context={})), [])
        self.assertNotEqual(s05(J10.compare(up, A, dn, O, "stream", context={})), [])

    def test_fragmentation_does_not_matter(self):
        up = anthropic_stream("let me think about 6*7")
        ref = keys(J13.compare(up, A, openai_stream("let me think about 6*7"), O, "stream", context={}))
        for n in (1, 2, 5):
            for key in ("reasoning_content", "reasoning"):
                with self.subTest(fragment=n, key=key):
                    dn = openai_stream("let me think about 6*7", key=key, fragments=n)
                    self.assertEqual(keys(J13.compare(up, A, dn, O, "stream", context={})), ref)

    def test_whitespace_is_collapsed_in_the_comparison(self):
        up = anthropic_stream("let me\nthink  about it")
        dn = openai_stream("let me think about it")
        self.assertEqual(s05(J13.compare(up, A, dn, O, "stream", context={})), [])

    def test_event_order_thinking_then_text_then_tool_call(self):
        tc = {"index": 0, "id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
        # reasoning arrives after the content in the wire order; the assembled message still lists thinking before the text
        s = (chunk({"role": "assistant", "content": "hi"}) + chunk({"reasoning_content": "t1 "}) + chunk({"tool_calls": [tc]})
             + chunk({"reasoning_content": "t2"}) + chunk({}, "tool_calls") + "data: [DONE]\n\n")
        kinds = [(e["kind"], e.get("payload")) for e in parse_ir(s, O, "stream")["events"]]
        self.assertEqual([k for k, _ in kinds], ["thinking", "text", "tool_call"])
        self.assertEqual(kinds[0][1], "t1 t2")
        resp = {"id": "c1", "object": "chat.completion", "model": "m", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "hi", "reasoning_content": "t1 t2",
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]}}]}
        self.assertEqual([k for k, _ in [(e["kind"], 0) for e in parse_ir(resp, O, "response")["events"]]],
                         ["thinking", "text", "tool_call"])

    def test_v10_ignores_the_carriers(self):
        s = openai_stream("let me think")
        ev = [e["kind"] for e in importlib.import_module("convpreserve.judge.ir").parse_ir(s, O, "stream")["events"]]
        self.assertNotIn("thinking", ev)
        self.assertIn("thinking", [e["kind"] for e in parse_ir(s, O, "stream")["events"]])

    def test_reasoning_text_that_differs_is_still_s05(self):
        up, dn = anthropic_stream("the answer is forty-two"), openai_stream("the answer is seventeen")
        new = J13.compare(up, A, dn, O, "stream", context={})
        self.assertEqual(sorted(f.detail for f in s05(new)),
                         ["invented assistant thinking 'theanswerisseventeen'", "lost assistant thinking 'theanswerisforty-two'"])
        self.assertEqual({f.tier for f in s05(new)}, {"T1"})

    def test_reasoning_lost_in_the_stream_is_still_s05(self):
        up, dn = anthropic_stream("let me think"), openai_stream("")
        new = J13.compare(up, A, dn, O, "stream", context={})
        old = J10.compare(up, A, dn, O, "stream", context={})
        self.assertEqual(len(s05(new)), 1)
        self.assertIn("lost assistant thinking", s05(new)[0].detail)
        self.assertEqual(keys(s05(new)), keys(s05(old)))

    def test_reasoning_invented_in_the_stream_is_still_s05(self):
        up, dn = anthropic_stream(None), openai_stream("a thought that was never upstream")
        new = J13.compare(up, A, dn, O, "stream", context={})
        self.assertEqual(len(s05(new)), 1)
        self.assertIn("invented", s05(new)[0].detail)


# ----------------------------------------------------------------------------- FP-4 regression on committed captures

FP4 = PACK / "artifacts/g4/oracle_v100/fp_classes/fp4_reasoning_stream/out/fp4_cells.json"
REV = PACK / "artifacts/g4/reverse_response_census"


def reverse_cell(cell_id):
    """(src SSE, upstream dialect, dst SSE) of '<family>/<dialect>/stream/<shape>' from the reverse-response census captures."""
    fam, dialect, _side, sid = cell_id.split("/")
    cap = json.loads((REV / "out" / f"captures_{fam}_{dialect}.json").read_text(encoding="utf-8"))
    shapes = json.loads((REV / cap["shapes_file"]).read_text(encoding="utf-8"))["shapes"]
    return shapes[sid]["stream_sse"], dialect, cap["results"][sid]["stream"]["raw_sse"]


@unittest.skipUnless(FP4.exists() and REV.exists(), "needs a checkout of the research repository (artifacts/g4)")
class TestFp4Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fp4 = [c["cell_id"] for c in json.loads(FP4.read_text(encoding="utf-8")) if c["class"] == "FP-4"]

    def judge(self, mod, cell_id):
        src, dialect, dst = reverse_cell(cell_id)
        return mod.compare(src, dialect, dst, O, "stream", context={"object_id": cell_id})

    def test_fp4_cells_json_has_21_entries_on_17_cells(self):
        """The four RA20 cells appear twice: once for the thinking text and once for `redacted_thinking`, which the scan
        also matched on the word 'thinking'.  Only the initial one is the FP-4 pattern."""
        self.assertEqual(len(self.fp4), 21)
        self.assertEqual(len(set(self.fp4)), 17)

    def test_three_fp4_cells(self):
        """v1.0 reports S05 on the cell, v1.3 does not.  The other findings of v1.0 stay; v1.3 adds S03 (signature carrier lost on
        surviving thinking) because the thinking now exists on both sides, as v1.0 already reports on the non-stream side."""
        picks = [next(c for c in self.fp4 if c.startswith(fam + "/")) for fam in ("litellm", "rosetta", "newapi")]
        for cell in picks:
            with self.subTest(cell=cell):
                old, new = self.judge(J10, cell), self.judge(J13, cell)
                self.assertEqual([f.rule_id for f in s05(old)], ["S05"])
                self.assertEqual(s05(new), [])
                rest_old = [k for k in keys(old) if k[0] != "S05"]
                rest_new = [k for k in keys(new) if k[0] != "S03"]
                self.assertEqual(rest_new, rest_old)
                self.assertEqual([f.rule_id for f in new if f.rule_id == "S03"], ["S03"])

    def test_every_fp4_cell_loses_the_thinking_s05(self):
        for cell in sorted(set(self.fp4)):
            with self.subTest(cell=cell):
                def thinking_s05(mod):
                    return [f for f in s05(self.judge(mod, cell)) if f.detail.split(" ", 2)[2].startswith("thinking")]
                self.assertEqual(len(thinking_s05(J10)), 1)
                self.assertEqual(thinking_s05(J13), [])

    def test_redacted_thinking_loss_is_kept(self):
        """`redacted_thinking` has no OpenAI Chat carrier; v1.3 still reports it (RA20 cells)."""
        cell = "litellm/anthropic_messages/stream/RA20"
        for mod in (J10, J13):
            self.assertIn("lost assistant redacted_thinking ''", [f.detail for f in s05(self.judge(mod, cell))])

    def test_gemini_upstream_cell_goes_clean(self):
        cell = "litellm/gemini_generatecontent/stream/RG14"
        old, new = self.judge(J10, cell), self.judge(J13, cell)
        self.assertEqual([f.rule_id for f in s05(old)], ["S05"])
        self.assertEqual(keys(new), [k for k in keys(old) if k[0] != "S05"])
        self.assertEqual([f for f in new if f.tier == "T1"], [])


# ----------------------------------------------------------------------------- change (b): evidence


class TestEvidence(unittest.TestCase):
    def test_v10_findings_have_no_evidence_field(self):
        self.assertEqual(J10.Finding._fields, ("rule_id", "tier", "object_id", "detail"))
        self.assertEqual(J13.Finding._fields, ("rule_id", "tier", "object_id", "detail", "evidence"))

    def test_control_dropped_points_into_the_source(self):
        src = {"model": "m", "max_tokens": 9, "temperature": 0.2, "messages": [{"role": "user", "content": "hi"}]}
        dst = {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]}
        (f,) = J13.compare(src, A, dst, A, "request", context={})
        self.assertEqual((f.rule_id, f.evidence), ("S08", {"side": "src", "path": "/temperature", "precision": "exact"}))

    def test_injected_default_points_into_the_destination(self):
        src = {"model": "m", "max_tokens": 9, "messages": [{"role": "user", "content": "hi"}]}
        dst = dict(src, seed=7)
        (f,) = J13.compare(src, A, dst, O, "request", context={})
        self.assertEqual((f.rule_id, f.evidence), ("D01", {"side": "dst", "path": "/seed", "precision": "exact"}))

    def test_stream_frames_are_addressed_by_index(self):
        up, dn = anthropic_stream("let me think"), openai_stream("some other thought")
        f = [x for x in J13.compare(up, A, dn, O, "stream", context={}) if x.rule_id == "S05"][0]
        body = up if f.evidence["side"] == "src" else dn
        node = EV.resolve_pointer(EV._doc(body, "stream"), f.evidence["path"])
        self.assertIsNotNone(node)
        self.assertIn(f.evidence["precision"], ("exact", "container", "payload"))

    def test_evidence_does_not_change_verdicts_on_the_sample(self):
        recs = json.load(gzip.open(PKG / "data" / "selftest_sample.json.gz", "rt", encoding="utf-8"))
        n = 0
        for r in recs:
            a = J10.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
            b = J13.compare(r["src_body"], r["src_dialect"], r["dst_body"], r["dst_dialect"], r["side"], context=r.get("context"))
            self.assertEqual(keys(a), keys(b))
            for f in b:
                n += 1
                ev = f.evidence
                self.assertEqual(set(ev) - {"missing_key"}, {"side", "path", "precision"})
                body = r["src_body"] if ev["side"] == "src" else r["dst_body"]
                EV.resolve_pointer(EV._doc(body, r["side"]), ev["path"])  # raises if the pointer does not resolve
        self.assertEqual(n, 323)  # v1.0 findings in the sample


# ----------------------------------------------------------------------------- change (c): S10 text


class TestS10Wording(unittest.TestCase):
    BANNED = "by" + "pass"

    def test_ablation_text_is_reworded_and_logic_is_unchanged(self):
        key = next(k for k in C13.common() if k.startswith("known_") and k.endswith("_sentinels"))
        sentinel = sorted(C13.common()[key])[0]
        src = {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "end_turn", "stop_sequence": None,
               "content": [{"type": "thinking", "thinking": "t", "signature": "sigreal"}, {"type": "text", "text": "hi"}],
               "usage": {"input_tokens": 1, "output_tokens": 2}}
        dst = copy.deepcopy(src)
        dst["content"][0]["signature"] = sentinel
        ctx = {"disabled": ["P4"]}
        old, new = J10.compare(src, A, dst, A, "response", context=ctx), J13.compare(src, A, dst, A, "response", context=ctx)
        self.assertTrue(old and new)
        self.assertIn(self.BANNED, old[0].detail)
        self.assertIn("carries known sentinel value", new[0].detail)
        self.assertEqual([(f.rule_id, f.tier) for f in old], [(f.rule_id, f.tier) for f in new])
        # the default path is the same text in both oracles
        self.assertEqual(keys(J10.compare(src, A, dst, A, "response", context={})), keys(J13.compare(src, A, dst, A, "response", context={})))

    def test_no_judge_v13_source_file_contains_the_word(self):
        for p in sorted((PKG / "judge_v13").glob("*.py")):
            self.assertNotIn(self.BANNED, p.read_text(encoding="utf-8").lower(), p.name)


# ----------------------------------------------------------------------------- pinning and selection


class TestPinning(unittest.TestCase):
    def test_judge_v13_sources_match_sha256sums(self):
        sums = (PKG / "judge_v13" / "SHA256SUMS").read_text(encoding="utf-8").split("\n")
        table = dict(reversed(line.split("  ", 1)) for line in sums if line.strip())
        files = sorted(p.name for p in (PKG / "judge_v13").glob("*.py"))
        self.assertEqual(sorted(table), files)
        for name in files:
            self.assertEqual(hashlib.sha256((PKG / "judge_v13" / name).read_bytes()).hexdigest(), table[name], name)

    def test_only_documented_files_differ_from_v10(self):
        same = [p.name for p in sorted((PKG / "judge").glob("*.py")) if (PKG / "judge_v13" / p.name).exists()
                and p.read_bytes() == (PKG / "judge_v13" / p.name).read_bytes()]
        self.assertEqual(same, ["schema.py", "schemacanon.py"])  # unchanged files; the rest differ only as in CHANGES_FROM_V100.md
        self.assertEqual(sorted(p.name for p in (PKG / "judge_v13").glob("*.py") if not (PKG / "judge" / p.name).exists()),
                         ["evidence.py"])

    def test_v10_package_is_not_touched(self):
        """judge/ is the frozen v1.0; test_vendoring.py pins its hashes.  Here: it has no evidence and no reasoning assembly."""
        text = (PKG / "judge" / "ir.py").read_text(encoding="utf-8")
        self.assertNotIn("reasoning_parts", text)
        self.assertNotIn("evidence", (PKG / "judge" / "judge.py").read_text(encoding="utf-8"))


class TestSelection(unittest.TestCase):
    def run_py(self, code, env=None):
        e = dict(os.environ, PYTHONPATH=str(PKG.parent))
        e.pop("CONVPRESERVE_ORACLE", None)
        e.update(env or {})
        return subprocess.run([sys.executable, "-c", code], env=e, capture_output=True, text=True)

    def test_default_and_env(self):
        code = "from convpreserve.oracles import default_oracle, ORACLES; print(default_oracle(), sorted(ORACLES))"
        self.assertEqual(self.run_py(code).stdout.split()[0], "v1.3")
        self.assertEqual(self.run_py(code, {"CONVPRESERVE_ORACLE": "v1.0"}).stdout.split()[0], "v1.0")
        self.assertNotEqual(self.run_py(code, {"CONVPRESERVE_ORACLE": "v9"}).returncode, 0)

    def test_get_judge_returns_the_two_packages(self):
        from convpreserve.oracles import get_judge
        self.assertIs(get_judge("v1.0"), J10)
        self.assertIs(get_judge("v1.3"), J13)
        with self.assertRaises(ValueError):
            get_judge("v1.1")

    def test_python_dash_m_version_names_both(self):
        r = self.run_py("import sys, runpy; sys.argv=['convpreserve','--version']; runpy.run_module('convpreserve', run_name='__main__')")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("oracles v1.0, v1.3; default v1.3", r.stdout)


if __name__ == "__main__":
    unittest.main()
