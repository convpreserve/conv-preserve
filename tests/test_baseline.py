"""`--baseline` and `--write-baseline` of `check` and `batch`.

Everything runs in process against the fixtures in examples/ (skipped when they are not part of the tree under
test); nothing opens a socket.
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from convpreserve import __version__, baseline, cli, report

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.normpath(os.path.join(HERE, "..", "examples"))
QS = os.path.join(EX, "quickstart")
MANIFEST = os.path.join(EX, "manifest.jsonl")
CHECK = ["check", "--side", "request", "--src-dialect", "openai_chat", "--dst-dialect", "anthropic_messages"]


def run(argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    old = dict(os.environ)
    if env is not None:
        os.environ.update(env)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as e:
                code = e.code
    finally:
        os.environ.clear()
        os.environ.update(old)
    return code, out.getvalue(), err.getvalue()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class Tmp(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="convpreserve-baseline-")
        self.addCleanup(self._td.cleanup)
        self.tmp = self._td.name

    def p(self, name):
        return os.path.join(self.tmp, name)


@unittest.skipUnless(os.path.isdir(QS), "examples/ not present")
class TestCheckBaseline(Tmp):
    PAIR = [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"]

    def test_roundtrip_and_match(self):
        bl = self.p("bl.json")
        code, out, err = run(CHECK + ["--write-baseline", bl] + self.PAIR)
        self.assertEqual(code, 1)                                  # writing does not change the exit code
        self.assertIn("wrote baseline bl.json: 1 fingerprint(s)", err)
        doc = json.loads(_read(bl))
        self.assertEqual(doc["convpreserve_baseline"], 1)
        self.assertEqual(doc["oracle"], "v1.3")
        self.assertEqual(doc["convpreserve"], __version__)
        self.assertEqual([(e["rule_id"], e["side"], e["where"], e["pair"]) for e in doc["entries"]],
                         [("S08", "request", "dst:/thinking", "src.json::dst_thinking_injected.json")])
        code, out2, _ = run(CHECK + ["--baseline", bl] + self.PAIR)
        self.assertEqual(code, 0)
        self.assertTrue(out2.startswith("S08\tT1\t"))
        self.assertTrue(out2.rstrip("\n").endswith("dst:/thinking\t[baselined]"))
        self.assertEqual(out2.replace("\t[baselined]", ""), out)   # the finding is still printed unchanged

    def test_write_is_deterministic_and_does_not_change_stdout(self):
        a, b = self.p("a.json"), self.p("b.json")
        _, out_plain, _ = run(CHECK + self.PAIR)
        _, out_a, _ = run(CHECK + ["--write-baseline", a] + self.PAIR)
        run(CHECK + ["--write-baseline", b] + self.PAIR)
        self.assertEqual(out_plain, out_a)
        self.assertEqual(_read(a), _read(b))
        loaded = baseline.load(a)
        self.assertEqual(len(loaded.keys), 1)

    def test_partial_mismatch_still_fails(self):
        bl = self.p("bl.json")
        run(CHECK + ["--write-baseline", bl] + self.PAIR)
        base = json.loads(_read(bl))
        for field, value in (("rule_id", "S09"), ("where", "dst:/other"), ("pair", "src.json::other.json"),
                             ("side", "response"), ("src_dialect", "gemini_generatecontent"),
                             ("dst_dialect", "openai_chat")):
            mod = json.loads(json.dumps(base))
            mod["entries"][0][field] = value
            path = self.p(f"mod_{field}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(mod, f)
            code, out, _ = run(CHECK + ["--baseline", path] + self.PAIR)
            self.assertEqual(code, 1, field)
            self.assertNotIn("[baselined]", out, field)

    def test_empty_baseline_changes_nothing_but_marks_json(self):
        bl = self.p("empty.json")
        baseline.write(bl, [], "v1.3", __version__)
        code, out, _ = run(CHECK + ["--baseline", bl, "--format", "json"] + self.PAIR)
        self.assertEqual(code, 1)
        d = json.loads(out)
        self.assertEqual((d["summary"]["T1"], d["baselined"], d["baseline"]), (1, 0, "empty.json"))
        self.assertIs(d["findings"][0]["baselined"], False)

    def test_json_counts_only_new(self):
        bl = self.p("bl.json")
        run(CHECK + ["--write-baseline", bl] + self.PAIR)
        code, out, _ = run(CHECK + ["--baseline", bl, "--format", "json"] + self.PAIR)
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["summary"], {"T1": 0, "T2": 0, "T3": 0})
        self.assertEqual(d["baselined"], 1)
        self.assertIs(d["findings"][0]["baselined"], True)
        self.assertEqual(d["findings"][0]["evidence"]["path"], "/thinking")

    def test_fail_on_applies_to_new_findings_only(self):
        bl = self.p("bl.json")
        run(CHECK + ["--write-baseline", bl] + self.PAIR)
        self.assertEqual(run(CHECK + ["--baseline", bl, "--fail-on", "T3"] + self.PAIR)[0], 0)
        self.assertEqual(run(CHECK + ["--fail-on", "T3"] + self.PAIR)[0], 1)

    def test_github_states_baselined_count(self):
        bl = self.p("bl.json")
        run(CHECK + ["--write-baseline", bl] + self.PAIR)
        code, out, _ = run(CHECK + ["--baseline", bl, "--format", "github"] + self.PAIR)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### convpreserve check: T1 0, T2 0, T3 0 (oracle v1.3)"))
        self.assertIn("1 finding(s) baselined by `bl.json`", out)
        self.assertIn("No new findings.", out)
        self.assertIn("1 baselined finding(s), not counted", out)
        self.assertIn("| `S08` | T1 | dst:/thinking |", out)

    def test_github_without_baseline_unchanged(self):
        _, out, _ = run(CHECK + ["--format", "github"] + self.PAIR)
        self.assertNotIn("baselined", out)
        self.assertTrue(out.startswith("### convpreserve check: T1 1, T2 0, T3 0 (oracle v1.3)"))

    def test_byte_identical_without_flag(self):
        findings = cli.get_judge("v1.3").compare(json.loads(_read(self.PAIR[0])), "openai_chat",
                                                 json.loads(_read(self.PAIR[1])), "anthropic_messages", "request",
                                                 context={})
        _, out, _ = run(CHECK + self.PAIR)
        self.assertEqual(out, report.render_check_text(findings))
        _, out, _ = run(CHECK + ["--format", "json"] + self.PAIR)
        self.assertEqual(out, report.render_check_json(findings, "v1.3", __version__, cli._get_carriers_sha()) + "\n")
        self.assertNotIn("baselined", out)

    def test_v10_findings_use_object_id(self):
        bl = self.p("v10.json")
        code, _, _ = run(CHECK + ["--oracle", "v1.0", "--write-baseline", bl] + self.PAIR)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(_read(bl))["oracle"], "v1.0")
        code, out, err = run(CHECK + ["--oracle", "v1.0", "--baseline", bl] + self.PAIR)
        self.assertEqual(code, 0, err)
        self.assertIn("[baselined]", out)
        code, out, err = run(CHECK + ["--oracle", "v1.3", "--baseline", bl] + self.PAIR)   # other oracle: no match
        self.assertEqual(code, 1)
        self.assertIn("was written with oracle v1.0, this run uses v1.3", err)

    def test_environment_variable(self):
        bl = self.p("bl.json")
        run(CHECK + ["--write-baseline", bl] + self.PAIR)
        code, out, _ = run(CHECK + self.PAIR, env={"CONVPRESERVE_BASELINE": bl})
        self.assertEqual(code, 0)
        self.assertIn("[baselined]", out)

    def test_bad_baseline_is_exit_2(self):
        missing = self.p("missing.json")
        code, _, err = run(CHECK + ["--baseline", missing] + self.PAIR)
        self.assertEqual(code, 2)
        self.assertIn("cannot read baseline", err)
        notjson = self.p("nj.json")
        with open(notjson, "w") as f:
            f.write("{")
        self.assertEqual(run(CHECK + ["--baseline", notjson] + self.PAIR)[0], 2)
        wrong = self.p("wrong.json")
        with open(wrong, "w") as f:
            json.dump({"findings": []}, f)
        code, _, err = run(CHECK + ["--baseline", wrong] + self.PAIR)
        self.assertEqual(code, 2)
        self.assertIn("is not an convpreserve baseline", err)
        noentry = self.p("noentry.json")
        with open(noentry, "w") as f:
            json.dump({"convpreserve_baseline": 1, "entries": [{"rule_id": "S08"}]}, f)
        self.assertEqual(run(CHECK + ["--baseline", noentry] + self.PAIR)[0], 2)


@unittest.skipUnless(os.path.isfile(MANIFEST), "examples/manifest.jsonl not present")
class TestBatchBaseline(Tmp):
    def test_batch_roundtrip(self):
        code, plain, _ = run(["batch", MANIFEST])
        self.assertEqual(code, 1)
        self.assertTrue(plain.startswith("pairs="))
        bl = self.p("bl.json")
        code, out, err = run(["batch", MANIFEST, "--write-baseline", bl])
        self.assertEqual((code, out), (1, plain))                   # stdout unchanged by --write-baseline
        n_entries = len(json.loads(_read(bl))["entries"])
        self.assertGreater(n_entries, 0)
        code, out, _ = run(["batch", MANIFEST, "--baseline", bl, "--format", "json", "--fail-on", "T3"])
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["summary"], {"T1": 0, "T2": 0, "T3": 0})
        self.assertEqual((d["breaching_pairs"], d["baseline"]), (0, "bl.json"))
        _, all_json, _ = run(["batch", MANIFEST, "--format", "json"])
        total = sum(json.loads(all_json)["summary"].values())
        self.assertEqual(d["baselined"], total)
        self.assertEqual(json.loads(all_json).get("baselined"), None)

    def test_batch_partial_baseline(self):
        bl = self.p("bl.json")
        run(["batch", MANIFEST, "--write-baseline", bl])
        doc = json.loads(_read(bl))
        t1_rules = {e["rule_id"] for e in doc["entries"] if e["tier"] == "T1"}
        drop = sorted(t1_rules)[0]
        doc["entries"] = [e for e in doc["entries"] if e["rule_id"] != drop]
        part = self.p("part.json")
        with open(part, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        code, out, _ = run(["batch", MANIFEST, "--baseline", part, "--format", "json"])
        self.assertEqual(code, 1)
        d = json.loads(out)
        self.assertEqual([r["rule_id"] for r in d["rules"] if r["rule_id"] == drop][:1], [drop])
        self.assertGreater(d["baselined"], 0)
        self.assertEqual(sorted({r["rule_id"] for r in d["rules"] if r["tier"] == "T1"}), [drop])

    def test_batch_text_github_out_and_summary(self):
        bl = self.p("bl.json")
        run(["batch", MANIFEST, "--write-baseline", bl])
        out_jsonl, md = self.p("out.jsonl"), self.p("summary.md")
        code, out, _ = run(["batch", MANIFEST, "--baseline", bl, "--out", out_jsonl, "--summary-md", md])
        self.assertEqual(code, 0)
        self.assertRegex(out.splitlines()[0], r"^pairs=\d+ errors=0 T1=0 T2=0 T3=0 pairs_with_T1=0 baselined=\d+$")
        lines = [json.loads(x) for x in _read(out_jsonl).splitlines()]
        flagged = [f for rec in lines for f in rec["findings"]]
        self.assertTrue(flagged and all(f["baselined"] for f in flagged))
        self.assertTrue(all(rec["summary"] == {"T1": 0, "T2": 0, "T3": 0} for rec in lines))
        self.assertIn("baseline `bl.json`", _read(md))
        code, out, _ = run(["batch", MANIFEST, "--baseline", bl, "--format", "github"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### convpreserve batch: T1 0, T2 0, T3 0"))
        self.assertIn("baselined by `bl.json`", out)
        self.assertIn("baselined finding(s), not counted", out)

    def test_batch_without_flag_has_no_baseline_keys(self):
        out_jsonl = self.p("plain.jsonl")
        run(["batch", MANIFEST, "--out", out_jsonl])
        for line in _read(out_jsonl).splitlines():
            rec = json.loads(line)
            for f in rec["findings"]:
                self.assertNotIn("baselined", f)
        _, text, _ = run(["batch", MANIFEST])
        self.assertNotIn("baselined", text)


class TestModule(Tmp):
    def test_write_dedupes_and_sorts(self):
        class F:
            rule_id, tier, object_id, detail = "S01", "T1", "x", "d"
        e1 = baseline.entry_of(F, "request", "a", "b", "p2")
        e2 = baseline.entry_of(F, "request", "a", "b", "p1")
        path = self.p("b.json")
        n = baseline.write(path, [e1, e2, e1], "v1.0", "0.1.0")
        self.assertEqual(n, 2)
        self.assertEqual([e["pair"] for e in json.loads(_read(path))["entries"]], ["p1", "p2"])
        b = baseline.load(path)
        self.assertTrue(b.known(F, "request", "a", "b", "p1"))
        self.assertFalse(b.known(F, "request", "a", "b", "p3"))
        self.assertEqual(baseline.entry_of(F, "request", "a", "b", "p1")["where"], "x")


if __name__ == "__main__":
    unittest.main()
