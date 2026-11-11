"""`--context K=V` on the command line.

`vendor_tokens` and `disabled` are lists for the judge; on the command line they are written comma-separated and must
reach the judge as lists (a plain string would be iterated one character at a time). Everything runs in process on small
payloads written to a temporary directory and on the fixtures in examples/; nothing opens a socket.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest

from convpreserve import cli

HERE = os.path.dirname(os.path.abspath(__file__))
EX = os.path.normpath(os.path.join(HERE, "..", "examples"))
QS = os.path.join(EX, "quickstart")
MANIFEST = os.path.join(EX, "manifest.jsonl")

SRC = {"model": "m", "max_tokens": 256, "messages": [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "SIG_SRC"},
                                      {"type": "text", "text": "hello"}]},
    {"role": "user", "content": "again"}]}
DST_TOKEN = "SIG_GW"
ANTHROPIC = ["--side", "request", "--src-dialect", "anthropic_messages", "--dst-dialect", "anthropic_messages"]


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class TestParseContext(unittest.TestCase):
    def test_list_keys_are_split_on_commas(self):
        self.assertEqual(cli._parse_context(["vendor_tokens=a,b"]), {"vendor_tokens": ["a", "b"]})
        self.assertEqual(cli._parse_context(["disabled=P1, P4"]), {"disabled": ["P1", "P4"]})

    def test_whitespace_and_empty_items_are_dropped(self):
        self.assertEqual(cli._parse_context(["vendor_tokens= a ,, b,"])["vendor_tokens"], ["a", "b"])
        self.assertEqual(cli._parse_context(["vendor_tokens="])["vendor_tokens"], [])

    def test_other_keys_stay_strings(self):
        self.assertEqual(cli._parse_context(["object_id=G051", "other=x,y"]), {"object_id": "G051", "other": "x,y"})

    def test_value_may_contain_equals_sign(self):
        self.assertEqual(cli._parse_context(["vendor_tokens=a=b,c"])["vendor_tokens"], ["a=b", "c"])

    def test_missing_equals_is_an_error(self):
        with self.assertRaises(ValueError):
            cli._parse_context(["vendor_tokens"])

    def test_non_stream_body_is_manifest_only(self):
        with self.assertRaises(ValueError):
            cli._parse_context(["non_stream_body=x"])

    def test_none_and_empty(self):
        self.assertEqual(cli._parse_context(None), {})
        self.assertEqual(cli._parse_context([]), {})


class TestVendorTokensOnCommandLine(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="convpreserve-context-")
        self.addCleanup(self._td.cleanup)
        self.src = self._write("src.json", SRC)
        dst = json.loads(json.dumps(SRC))
        dst["messages"][1]["content"][0]["signature"] = DST_TOKEN
        self.dst = self._write("dst.json", dst)

    def _write(self, name, obj):
        path = os.path.join(self._td.name, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def _rules(self, *extra):
        code, out, err = run(["check", *ANTHROPIC, "--format", "json", self.src, self.dst, *extra])
        self.assertIn(code, (0, 1), err)
        return code, [f["rule_id"] for f in json.loads(out)["findings"]]

    def test_s10_fires_without_context(self):
        code, rules = self._rules()
        self.assertEqual((code, rules), (1, ["S10"]))

    def test_vendor_tokens_on_command_line_removes_s10(self):
        code, rules = self._rules("--context", f"vendor_tokens={DST_TOKEN}")
        self.assertEqual((code, rules), (0, []))

    def test_vendor_tokens_list_with_other_entries(self):
        code, rules = self._rules("--context", f"vendor_tokens=other,{DST_TOKEN}")
        self.assertEqual((code, rules), (0, []))

    def test_other_token_does_not_help(self):
        code, rules = self._rules("--context", "vendor_tokens=other")
        self.assertEqual((code, rules), (1, ["S10"]))

    def test_same_effect_as_manifest_list(self):
        line = {"id": "a", "side": "request", "src_dialect": "anthropic_messages", "dst_dialect": "anthropic_messages",
                "src_file": self.src, "dst_file": self.dst, "context": {"vendor_tokens": [DST_TOKEN]}}
        manifest = self._write("m.jsonl", line)
        _, cli_rules = self._rules("--context", f"vendor_tokens={DST_TOKEN}")
        code, out, err = run(["batch", manifest, "--format", "json"])
        self.assertEqual(code, 0, err)
        self.assertEqual([r["rule_id"] for r in json.loads(out)["rules"]], cli_rules)

    def test_disabled_p4_on_command_line_reaches_the_judge(self):
        # Ablation of P4 falls back to literal sentinel matching, so the unknown token is no longer reported.
        code, rules = self._rules("--context", "disabled=P4")
        self.assertEqual((code, rules), (0, []))

    def test_object_id_label(self):
        code, out, _ = run(["check", *ANTHROPIC, "--format", "json", self.src, self.dst, "--context", "object_id=O1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["findings"][0]["object_id"], "O1")

    def test_non_stream_body_on_command_line_is_a_usage_error(self):
        code, out, err = run(["check", *ANTHROPIC, self.src, self.dst, "--context", "non_stream_body=x"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("non_stream_body", err)

    def test_help_lists_the_keys(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["check", "--help"])
        text = " ".join(buf.getvalue().split())
        for key in ("object_id", "vendor_tokens", "disabled", "non_stream_body"):
            self.assertIn(key, text)


# SHA-256 of json.dumps([exit code, stdout, stderr]) of the runs below, taken from the sources before `--context` list
# parsing was added. The directory of examples/ is replaced by "<EX>" because the github format prints the file paths,
# and the SHA-256 of carriers.json by "<CARRIERS>" because the JSON output carries it and a copy of the package may
# have another metadata string in that file.
GOLDEN = {
    "check_text": "942b224f97e3f39a8ea8b71ada4c46c9d94419e18c0eea9323c447a8af91f7a9",
    "batch_text": "efb7e506830c15706e3deebb1a377865555aa43c0a0b3b5b2e2d88582e315c52",
    "check_json": "cfc86a2d59eb67ce51ded0af1d5cf0d201e02c9b72838a20a67bfe18dde14789",
    "batch_json": "2ceead95c2aecac04d03c9a3eb44381c2f660437a58ea627d99667732f15ee55",
    "check_github": "fc3b94771986818b066d9ed5fc2afd81021aaba26a98687093a2969f6c54c56c",
    "batch_github": "bccb7600bcac8a85bc74c5d1d032e2efadea20b7d1c432c298d4516a8f7df0bc",
}


@unittest.skipUnless(os.path.isdir(QS) and os.path.isfile(MANIFEST), "examples/ not present")
class TestNoContextOutputUnchanged(unittest.TestCase):
    CHK = ["check", "--side", "request", "--src-dialect", "openai_chat", "--dst-dialect", "anthropic_messages"]

    @staticmethod
    def _norm(text):
        path = os.path.join(os.path.dirname(os.path.abspath(cli.__file__)), "data", "carriers.json")
        with open(path, "rb") as f:
            carriers = hashlib.sha256(f.read()).hexdigest()
        return text.replace(EX, "<EX>").replace(carriers, "<CARRIERS>")

    def _digest(self, argv):
        code, out, err = run(argv)
        norm = [code, self._norm(out), self._norm(err)]
        return hashlib.sha256(json.dumps(norm).encode()).hexdigest()

    def test_check_and_batch_output_without_context_is_byte_identical(self):
        pair = [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"]
        got = {}
        for fmt in ("text", "json", "github"):
            got[f"check_{fmt}"] = self._digest(self.CHK + ["--format", fmt, *pair])
            got[f"batch_{fmt}"] = self._digest(["batch", MANIFEST, "--format", fmt])
        self.assertEqual(got, GOLDEN)


if __name__ == "__main__":
    unittest.main()
