"""`--format github` and the CI templates (GitHub Action, pre-commit).

The action's `run:` step and the pre-commit hook entries are executed locally in a shell against the fixtures in
examples/; nothing here opens a socket or needs GitHub.  Tests that read action.yml, .pre-commit-hooks.yaml or
examples/ci are skipped when those files are not part of the tree under test (for example an sdist).
"""
import contextlib
import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

from convpreserve import cli, report

try:
    import yaml
except ImportError:                                  # pragma: no cover
    yaml = None

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
EX = os.path.join(ROOT, "examples")
QS = os.path.join(EX, "quickstart")
SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
ACTION = os.path.join(ROOT, "action.yml")
HOOKS = os.path.join(ROOT, ".pre-commit-hooks.yaml")
CI = os.path.join(EX, "ci")
CHECK = ["check", "--side", "request", "--src-dialect", "openai_chat", "--dst-dialect", "anthropic_messages"]
HAVE_BASH = shutil.which("bash") is not None


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@unittest.skipUnless(os.path.isdir(QS), "examples/ not present")
class TestCheckGithub(unittest.TestCase):
    def test_t1_pair(self):
        code, out, err = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json", "--format", "github"])
        self.assertEqual(code, 1, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "### convpreserve check: T1 1, T2 0, T3 0 (oracle v1.3)")
        self.assertEqual(sum(1 for ln in lines if ln.startswith("| Rule |")), 1)
        rows = [ln for ln in lines if ln.startswith("| `")]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("| `S08` | T1 | dst:/thinking | "))
        self.assertIn("generation or tool-choice parameter set by the caller was dropped or altered", rows[0])
        self.assertNotIn("<details>", out)

    def test_clean_pair(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_clean.json", "--format", "github"])
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("### convpreserve check: T1 0, T2 0, T3 0 (oracle v1.3)"))
        self.assertIn("No findings.", out)
        self.assertNotIn("| Rule |", out)

    def test_oracle_version_in_heading(self):
        _, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_clean.json", "--format", "github", "--oracle", "v1.0"])
        self.assertIn("(oracle v1.0)", out.splitlines()[0])

    def test_fail_on_none_exit_0(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json", "--format", "github",
                                     "--fail-on", "none"])
        self.assertEqual(code, 0)
        self.assertIn("`S08`", out)

    def test_json_alias(self):
        a = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json", "--json"])
        b = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json", "--format", "json"])
        self.assertEqual(a, b)
        self.assertEqual(json.loads(a[1])["summary"], {"T1": 1, "T2": 0, "T3": 0})

    def test_default_is_text_and_unchanged(self):
        code, out, _ = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json"])
        self.assertEqual(code, 1)
        self.assertEqual(out, "S08\tT1\t\tunlicensed control thinking injected: "
                              "{'type': 'enabled', 'budget_tokens': 1024}\tdst:/thinking\n")
        self.assertEqual(run(CHECK + [f"{QS}/src.json", f"{QS}/dst_thinking_injected.json", "--format", "text"]),
                         (code, out, ""))

    def test_json_and_format_conflict_exit_2(self):
        code, out, err = run(CHECK + [f"{QS}/src.json", f"{QS}/dst_clean.json", "--json", "--format", "github"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("conflict", err)

    def test_bad_format_exit_2(self):
        self.assertEqual(run(CHECK + [f"{QS}/src.json", f"{QS}/dst_clean.json", "--format", "html"])[0], 2)


@unittest.skipUnless(os.path.isdir(QS), "examples/ not present")
class TestBatchFormats(unittest.TestCase):
    MANIFEST = os.path.join(EX, "manifest.jsonl")

    def test_github(self):
        code, out, err = run(["batch", self.MANIFEST, "--format", "github"])
        self.assertEqual(code, 1, err)
        self.assertEqual(out.splitlines()[0], "### convpreserve batch: T1 26, T2 5, T3 7 (oracle v1.3)")
        rows = [ln for ln in out.splitlines() if ln.startswith("| `")]
        self.assertEqual(len(rows), 38)
        self.assertNotIn("<details>", out)                   # 38 rows: below the fold limit
        self.assertIn("request:anthropic_messages->openai_chat: ", out)

    def test_json(self):
        code, out, err = run(["batch", self.MANIFEST, "--format", "json"])
        self.assertEqual(code, 1, err)
        d = json.loads(out)
        self.assertEqual(d["pairs"], 14)
        self.assertEqual(d["summary"], {"T1": 26, "T2": 5, "T3": 7})
        self.assertEqual(d["errors"], 0)
        self.assertEqual(d["oracle"], "v1.3")
        self.assertEqual(len(d["by_pair"]), 14)

    def test_text_default_unchanged(self):
        code, out, _ = run(["batch", self.MANIFEST])
        self.assertEqual(code, 1)
        self.assertTrue(out.startswith("pairs=14 errors=0 T1=26 T2=5 T3=7 pairs_with_T1=14\n"))
        self.assertIn("\nD03\tT2\t1\n", out)


class TestRenderGithub(unittest.TestCase):
    @staticmethod
    def _rows(n, **kw):
        return [{"rule_id": "S08", "tier": "T1", "where": f"dst:/p{i}", "detail": "d", **kw} for i in range(n)]

    def test_fold_limit(self):
        counts = {"T1": 50, "T2": 0, "T3": 0}
        out = report.render_github(self._rows(50), counts, "v1.3", "0.1.0")
        self.assertNotIn("<details>", out)
        counts = {"T1": 51, "T2": 0, "T3": 0}
        out = report.render_github(self._rows(51), counts, "v1.3", "0.1.0")
        self.assertEqual(out.count("<details>"), 1)
        self.assertEqual(out.count("</details>"), 1)
        self.assertIn("51 findings", out)
        self.assertEqual(out.count("| `S08` |"), 51)
        self.assertEqual(out.splitlines()[0], "### convpreserve check: T1 51, T2 0, T3 0 (oracle v1.3)")
        # the table sits inside the fold; the heading stays outside
        self.assertLess(out.index("### "), out.index("<details>"))
        self.assertLess(out.index("<details>"), out.index("| Rule |"))
        self.assertLess(out.index("| Rule |"), out.index("</details>"))

    def test_cells_cannot_break_the_table(self):
        rows = self._rows(1, detail="a | b <script>x</script>\n`tick` & more", where="x|y")
        out = report.render_github(rows, {"T1": 1, "T2": 0, "T3": 0}, "v1.3", "0.1.0")
        row = [ln for ln in out.splitlines() if ln.startswith("| `S08`")]
        self.assertEqual(len(row), 1)                        # no raw newline inside a cell
        self.assertNotIn("<script>", row[0])
        self.assertIn("a \\| b &lt;script>x&lt;/script> 'tick' &amp; more", row[0])
        self.assertIn("x\\|y", row[0])
        self.assertEqual(len(re.findall(r"(?<!\\)\|", row[0])), 6)   # 5 cells -> 6 unescaped bars

    def test_long_detail_is_truncated(self):
        out = report.render_github(self._rows(1, detail="z" * 1000), {"T1": 1, "T2": 0, "T3": 0}, "v1.3", "0.1.0")
        self.assertIn("...", out)
        self.assertLess(max(len(ln) for ln in out.splitlines()), 800)

    def test_errors_listed(self):
        out = report.render_github([], {"T1": 0, "T2": 0, "T3": 0}, "v1.3", "0.1.0", errors=["line 3: bad json"])
        self.assertIn("No findings.", out)
        self.assertIn("- line 3: bad json", out)

    def test_every_catalog_rule_has_a_meaning(self):
        from convpreserve import explain
        m = explain.rule_meanings()
        self.assertEqual(len(m), 27)
        self.assertTrue(all(m.values()))


# ---------------------------------------------------------------------------------------------------------------
# CI templates, executed locally
# ---------------------------------------------------------------------------------------------------------------

def _wrapper_dir():
    """A directory holding an `convpreserve` executable that runs the package of this source tree."""
    d = tempfile.mkdtemp(prefix="convpreserve-wrap-")
    p = os.path.join(d, "convpreserve")
    with open(p, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\nPYTHONPATH=%s PYTHONDONTWRITEBYTECODE=1 exec %s -m convpreserve \"$@\"\n"
                % (shlex.quote(SRC_DIR), shlex.quote(sys.executable)))
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)
    return d


def _env(wrap, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("INPUT_")}
    env["PATH"] = wrap + os.pathsep + env.get("PATH", "")
    env.update(extra)
    return env


@unittest.skipUnless(yaml and os.path.isfile(ACTION), "PyYAML or action.yml not available")
class TestActionYaml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.action = yaml.safe_load(_read(ACTION))
        cls.wrap = _wrapper_dir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wrap, ignore_errors=True)

    def test_structure(self):
        a = self.action
        self.assertEqual(a["runs"]["using"], "composite")
        self.assertEqual(set(a["inputs"]), {"mode", "src", "dst", "side", "src-dialect", "dst-dialect", "manifest",
                                            "oracle", "fail-on"})
        self.assertEqual(a["inputs"]["fail-on"]["default"], "T1")
        self.assertEqual(a["inputs"]["mode"]["default"], "check")
        self.assertEqual(set(a["outputs"]), {"exit-code", "t1", "t2", "t3"})
        for o in a["outputs"].values():
            self.assertRegex(o["value"], r"\$\{\{ steps\.run\.outputs\.")
        steps = a["runs"]["steps"]
        self.assertEqual([s.get("id") for s in steps], [None, "run"])
        self.assertTrue(all(s["shell"] == "bash" for s in steps))
        self.assertIn("pip install", steps[0]["run"])
        self.assertIn("github.action_path", steps[0]["env"]["ACTION_PATH"])
        self.assertIn("GITHUB_STEP_SUMMARY", steps[1]["run"])
        self.assertIn("--format github", steps[1]["run"])
        self.assertIn("--fail-on", steps[1]["run"])

    def _run_step(self, **inputs):
        """Execute the action's run step the way the runner does (bash -eo pipefail script); return rc and files."""
        step = self.action["runs"]["steps"][1]
        tmp = tempfile.mkdtemp(prefix="convpreserve-action-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        summary, outputs, script = (os.path.join(tmp, n) for n in ("summary.md", "output.txt", "step.sh"))
        for p in (summary, outputs):
            open(p, "w").close()
        with open(script, "w", encoding="utf-8") as f:
            f.write(step["run"])
        env = _env(self.wrap, GITHUB_STEP_SUMMARY=summary, GITHUB_OUTPUT=outputs)
        for key, expr in step["env"].items():                # `${{ inputs.<name> }}` -> the value given in the test
            name = re.fullmatch(r"\$\{\{ inputs\.([a-z-]+) \}\}", expr).group(1)
            env[key] = inputs.get(name.replace("-", "_"), self.action["inputs"][name]["default"])
        r = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", script], env=env, cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        out = dict(ln.split("=", 1) for ln in _read(outputs).splitlines())
        return r, _read(summary), out

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_check_t1_fails_step(self):
        r, summary, out = self._run_step(src=f"{QS}/src.json", dst=f"{QS}/dst_thinking_injected.json",
                                         src_dialect="openai_chat", dst_dialect="anthropic_messages")
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue(summary.startswith("### convpreserve check: T1 1, T2 0, T3 0 (oracle v1.3)"))
        self.assertIn("`S08`", summary)
        self.assertEqual(out, {"t1": "1", "t2": "0", "t3": "0", "exit-code": "1"})

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_check_clean_passes(self):
        r, summary, out = self._run_step(src=f"{QS}/src.json", dst=f"{QS}/dst_clean.json",
                                         src_dialect="openai_chat", dst_dialect="anthropic_messages", oracle="v1.0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("No findings.", summary)
        self.assertIn("(oracle v1.0)", summary)
        self.assertEqual(out, {"t1": "0", "t2": "0", "t3": "0", "exit-code": "0"})

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_fail_on_decides_exit_code(self):
        args = dict(src=f"{QS}/src.json", dst=f"{QS}/dst_thinking_injected.json",
                    src_dialect="openai_chat", dst_dialect="anthropic_messages")
        r, summary, out = self._run_step(fail_on="none", **args)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out["exit-code"], out["t1"]), ("0", "1"))       # findings still reported
        self.assertIn("`S08`", summary)

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_batch(self):
        r, summary, out = self._run_step(mode="batch", manifest=os.path.join(EX, "manifest.jsonl"))
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertTrue(summary.startswith("### convpreserve batch: T1 26, T2 5, T3 7"))
        self.assertEqual(out, {"t1": "26", "t2": "5", "t3": "7", "exit-code": "1"})
        r, _, out = self._run_step(mode="batch", manifest=os.path.join(EX, "manifest.jsonl"), fail_on="none")
        self.assertEqual(r.returncode, 0, r.stderr)

    @unittest.skipUnless(HAVE_BASH, "bash missing")
    def test_input_errors_exit_2(self):
        for kw in ({"mode": "nope"}, {"mode": "check"}, {"mode": "batch"}):
            r, _, out = self._run_step(**kw)
            self.assertEqual(r.returncode, 2, kw)
            self.assertEqual(out, {"exit-code": "2"})
            self.assertIn("::error::", r.stdout)

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_unreadable_file_exit_2(self):
        r, _, out = self._run_step(src="/nonexistent/src.json", dst=f"{QS}/dst_clean.json",
                                   src_dialect="openai_chat", dst_dialect="anthropic_messages")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(out, {"exit-code": "2"})


@unittest.skipUnless(yaml and os.path.isfile(HOOKS), "PyYAML or .pre-commit-hooks.yaml not available")
class TestPreCommitHooks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hooks = {h["id"]: h for h in yaml.safe_load(_read(HOOKS))}
        cls.wrap = _wrapper_dir()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.wrap, ignore_errors=True)

    def _entry(self, hook_id, files=()):
        h = self.hooks[hook_id]
        cmd = shlex.split(h["entry"]) + (list(files) if h.get("pass_filenames", True) else [])
        return subprocess.run(cmd, env=_env(self.wrap), cwd=ROOT, capture_output=True, text=True, timeout=300)

    def test_structure(self):
        self.assertEqual(set(self.hooks), {"convpreserve-batch", "convpreserve-selftest"})
        for h in self.hooks.values():
            self.assertEqual(h["language"], "python")
            self.assertTrue(h["name"] and h["description"] and h["entry"])
        self.assertTrue(re.search(self.hooks["convpreserve-batch"]["files"], "ci/convpreserve_pairs.jsonl"))
        self.assertFalse(re.search(self.hooks["convpreserve-batch"]["files"], "src/app.py"))
        self.assertIs(self.hooks["convpreserve-selftest"]["pass_filenames"], False)
        self.assertIs(self.hooks["convpreserve-selftest"]["always_run"], True)

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_batch_entry_fails_on_t1(self):
        r = self._entry("convpreserve-batch", [os.path.join(EX, "manifest.jsonl")])
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("T1=26", r.stdout)

    @unittest.skipUnless(HAVE_BASH and os.path.isdir(QS), "bash or examples/ missing")
    def test_batch_entry_passes_on_clean_manifest_and_checks_every_file(self):
        with tempfile.TemporaryDirectory() as d:
            clean = os.path.join(d, "convpreserve_clean.jsonl")
            with open(clean, "w", encoding="utf-8") as f:
                f.write(json.dumps({"id": "clean", "side": "request", "src_dialect": "openai_chat",
                                    "dst_dialect": "anthropic_messages", "src_file": f"{QS}/src.json",
                                    "dst_file": f"{QS}/dst_clean.json"}) + "\n")
            self.assertEqual(self._entry("convpreserve-batch", [clean]).returncode, 0)
            # a failing manifest after a passing one must still fail the hook
            self.assertEqual(self._entry("convpreserve-batch", [clean, os.path.join(EX, "manifest.jsonl")]).returncode, 1)
            # no files -> nothing to check -> pass
            self.assertEqual(self._entry("convpreserve-batch", []).returncode, 0)

    @unittest.skipUnless(HAVE_BASH, "bash missing")
    def test_selftest_entry(self):
        r = self._entry("convpreserve-selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stdout.count("PASS"), 2)


@unittest.skipUnless(yaml and os.path.isdir(CI), "PyYAML or examples/ci not available")
class TestCiTemplates(unittest.TestCase):
    def test_workflow_template(self):
        w = yaml.safe_load(_read(os.path.join(CI, "github-workflow.yml")))
        self.assertEqual(w["permissions"], {"contents": "read"})
        steps = w["jobs"]["convpreserve"]["steps"]
        uses = [s["uses"] for s in steps if "uses" in s]
        self.assertIn("actions/checkout@v4", uses)
        ours = [s for s in steps if s.get("uses", "").startswith("convpreserve/conv-preserve@")]
        self.assertEqual([s["with"]["mode"] for s in ours], ["check", "batch"])
        with open(ACTION, encoding="utf-8") as f:
            inputs = set(yaml.safe_load(f)["inputs"])
        for s in ours:
            self.assertTrue(set(s["with"]) <= inputs)

    def test_pre_commit_template(self):
        c = yaml.safe_load(_read(os.path.join(CI, "pre-commit-config.yaml")))
        (repo,) = c["repos"]
        self.assertEqual(repo["rev"], "main")
        hooks = {h["id"] for h in yaml.safe_load(_read(HOOKS))}
        self.assertTrue({h["id"] for h in repo["hooks"]} <= hooks)


if __name__ == "__main__":
    unittest.main()
