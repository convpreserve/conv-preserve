"""CLI behaviour tests: exit codes, stdin, batch manifest, Markdown summary.

Runs against the installed or source-tree `convpreserve` package (no repository checkout
needed beyond the examples/ directory shipped in the sdist).
"""
import contextlib
import io
import json
import os
import tempfile
import unittest

from convpreserve import cli

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.join(HERE, "..", "examples")
QS = os.path.join(EX, "quickstart")
CHECK = ["check", "--side", "request", "--src-dialect", "openai_chat",
         "--dst-dialect", "anthropic_messages"]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def run(argv, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = None
    if stdin is not None:
        import sys
        old_stdin = sys.stdin
        sys.stdin = io.TextIOWrapper(io.BytesIO(stdin.encode("utf-8")), encoding="utf-8")
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(argv)
                code = 0
            except SystemExit as e:
                code = e.code
    finally:
        if old_stdin is not None:
            import sys
            sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


@unittest.skipUnless(os.path.isdir(QS), "examples/ not present")
class TestCheck(unittest.TestCase):
    def test_t1_exit_1(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        self.assertEqual(code, 1)
        self.assertIn("S08\tT1", out)

    def test_clean_exit_0(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_clean.json"])
        self.assertEqual((code, out), (0, ""))

    def test_fail_on_none(self):
        code, _, _ = run(CHECK + ["--fail-on", "none", f"{QS}/src.json",
                                  f"{QS}/dst_thinking_injected.json"])
        self.assertEqual(code, 0)

    def test_json_output_schema(self):
        code, out, _ = run(CHECK + ["--json", f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        d = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(d["summary"], {"T1": 1, "T2": 0, "T3": 0})
        self.assertEqual(d["oracle"], "v1.3")  # default oracle
        self.assertEqual(len(d["carriers_sha256"]), 64)
        self.assertEqual(set(d["findings"][0]), {"rule_id", "tier", "object_id", "detail", "evidence"})
        self.assertEqual(set(d["findings"][0]["evidence"]), {"side", "path", "precision"})

    def test_oracle_v10_json_has_no_evidence(self):
        code, out, _ = run(CHECK + ["--json", "--oracle", "v1.0", f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        d = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(d["oracle"], "v1.0")
        self.assertEqual(set(d["findings"][0]), {"rule_id", "tier", "object_id", "detail"})

    def test_oracle_v13_text_has_evidence_column(self):
        _, out13, _ = run(CHECK + ["--oracle", "v1.3", f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        _, out10, _ = run(CHECK + ["--oracle", "v1.0", f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        self.assertEqual(len(out10.splitlines()[0].split("\t")), 4)
        self.assertEqual(len(out13.splitlines()[0].split("\t")), 5)
        self.assertEqual(out13.splitlines()[0].split("\t")[:4], out10.splitlines()[0].split("\t"))

    def test_env_selects_default_oracle(self):
        old = os.environ.get("CONVPRESERVE_ORACLE")
        os.environ["CONVPRESERVE_ORACLE"] = "v1.0"
        try:
            _, out, _ = run(CHECK + ["--json", f"{QS}/src.json", f"{QS}/dst_clean.json"])
        finally:
            if old is None:
                del os.environ["CONVPRESERVE_ORACLE"]
            else:
                os.environ["CONVPRESERVE_ORACLE"] = old
        self.assertEqual(json.loads(out)["oracle"], "v1.0")

    def test_unknown_oracle_exit_2(self):
        code, _, err = run(CHECK + ["--oracle", "v9.9", f"{QS}/src.json", f"{QS}/dst_clean.json"])
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)

    def test_stdin(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", "-"], stdin=_read(f"{QS}/dst_clean.json"))
        self.assertEqual((code, out), (0, ""))

    def test_missing_file_exit_2(self):
        code, _, err = run(CHECK + [f"{QS}/src.json", f"{QS}/nope.json"])
        self.assertEqual(code, 2)
        self.assertIn("Error", err)

    def test_both_stdin_exit_2(self):
        code, _, _ = run(CHECK + ["-", "-"], stdin="{}")
        self.assertEqual(code, 2)

    def test_no_command_exit_2(self):
        code, _, _ = run([])
        self.assertEqual(code, 2)


@unittest.skipUnless(os.path.isdir(QS), "examples/ not present")
class TestBatch(unittest.TestCase):
    def _manifest(self, d, lines):
        p = os.path.join(d, "m.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        return p

    def test_inline_and_file_refs(self):
        src = _load(f"{QS}/src.json")
        bad = _load(f"{QS}/dst_thinking_injected.json")
        base = {"side": "request", "src_dialect": "openai_chat", "dst_dialect": "anthropic_messages"}
        with tempfile.TemporaryDirectory() as d:
            m = self._manifest(d, [
                dict(base, id="a", src=src, dst=bad),
                dict(base, id="b", src_file=os.path.join(QS, "src.json"),
                     dst_file=os.path.join(QS, "dst_clean.json")),
            ])
            out, md = os.path.join(d, "o.jsonl"), os.path.join(d, "s.md")
            code, stdout, _ = run(["batch", m, "--out", out, "--summary-md", md])
            self.assertEqual(code, 1)
            self.assertIn("pairs=2 errors=0 T1=1 T2=0 T3=0 pairs_with_T1=1", stdout)
            rows = [json.loads(x) for x in _read(out).splitlines()]
            self.assertEqual([r["summary"]["T1"] for r in rows], [1, 0])
            text = _read(md)
            self.assertIn("| T1 | 1 | 1 |", text)
            self.assertIn("| S08 | T1 | 1 |", text)

    def test_example_manifest_all_t1(self):
        code, stdout, _ = run(["batch", os.path.join(EX, "manifest.jsonl")])
        self.assertEqual(code, 1)
        self.assertIn("errors=0", stdout)

    def test_bad_line_exit_2_and_continues(self):
        with tempfile.TemporaryDirectory() as d:
            m = os.path.join(d, "m.jsonl")
            with open(m, "w", encoding="utf-8") as f:
                f.write('{"side": "request"}\n')
                f.write(json.dumps({"side": "request", "src_dialect": "openai_chat",
                                    "dst_dialect": "anthropic_messages",
                                    "src_file": os.path.join(QS, "src.json"),
                                    "dst_file": os.path.join(QS, "dst_clean.json")}) + "\n")
            code, stdout, err = run(["batch", m])
            self.assertEqual(code, 2)
            self.assertIn("pairs=2 errors=1", stdout)
            self.assertIn("line 1", err)


class TestMisc(unittest.TestCase):
    def test_dialects(self):
        code, out, _ = run(["dialects"])
        self.assertEqual(code, 0)
        self.assertIn("anthropic_messages", out.split())

    def test_version_flag(self):
        code, out, _ = run(["version"])
        self.assertEqual(code, 0)
        self.assertIn("oracle v1.0", out)
        self.assertIn("oracle v1.3", out)
        self.assertEqual(out.count("[default]"), 1)

    def test_version_option_names_both_oracles(self):
        code, out, _ = run(["--version"])
        self.assertEqual(code, 0)
        self.assertIn("oracles v1.0, v1.3", out)

    def test_selftest_passes(self):
        for oracle in ("v1.0", "v1.3"):
            with self.subTest(oracle=oracle):
                code, out, _ = run(["selftest", "--oracle", oracle])
                self.assertEqual(code, 0)
                self.assertIn("400 identical / 400 total", out)
                self.assertIn("rule cases: 174 identical / 174 total", out)
                self.assertIn("PASS", out)
        code, out, _ = run(["selftest"])
        self.assertEqual((code, "PASS" in out), (0, True))


if __name__ == "__main__":
    unittest.main()
