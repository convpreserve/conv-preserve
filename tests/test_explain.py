"""`convpreserve explain`: rule lookup, unknown rules, --list, --json, and the curated root-cause notes."""
import contextlib
import io
import json
import os
import re
import unittest

from convpreserve import cli, explain

DATA = os.path.join(os.path.dirname(os.path.abspath(explain.__file__)), "data")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            cli.main(argv)
            code = 0
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


def _catalog_ids():
    with open(os.path.join(DATA, "catalog.json"), encoding="utf-8") as f:
        return [r["id"] for r in json.load(f)["rules"]]


class TestExplain(unittest.TestCase):
    def test_rule_with_notes(self):
        code, out, err = run(["explain", "S08"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("S08  CONTROL  (T1)"))
        self.assertIn("Meaning: a control the source SET is dropped or changed", out)
        self.assertIn("Common root causes", out)
        self.assertIn("Source: ", out)

    def test_rule_without_notes(self):
        code, out, err = run(["explain", "D03"])
        self.assertEqual(code, 0, err)
        self.assertIn("Meaning: cache_control / service_tier / prompt-cache hints dropped or mis-mapped", out)
        self.assertIn("no curated notes yet", out)

    def test_case_insensitive_and_name(self):
        self.assertEqual(run(["explain", "s08"]), run(["explain", "S08"]))
        self.assertEqual(run(["explain", "control"]), run(["explain", "S08"]))

    def test_unknown_rule_exit_2_lists_similar(self):
        code, out, err = run(["explain", "S88"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("unknown rule 'S88'", err)
        self.assertIn("S08", err)
        self.assertIn("convpreserve explain --list", err)

    def test_unknown_rule_without_similar_still_exit_2(self):
        code, _, err = run(["explain", "ZZZZ-no-such-rule"])
        self.assertEqual(code, 2)
        self.assertIn("unknown rule", err)

    def test_no_argument_exit_2(self):
        code, out, err = run(["explain"])
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("--list", err)

    def test_list_is_catalog_order_one_id_per_line(self):
        code, out, _ = run(["explain", "--list"])
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines(), _catalog_ids())
        self.assertEqual(len(_catalog_ids()), 27)

    def test_list_json(self):
        code, out, _ = run(["explain", "--list", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual([r["id"] for r in json.loads(out)], _catalog_ids())

    def test_json_with_and_without_notes(self):
        code, out, _ = run(["explain", "S08", "--json"])
        self.assertEqual(code, 0)
        d = json.loads(out)
        self.assertEqual(d["id"], "S08")
        self.assertTrue(d["curated_notes"])
        self.assertTrue(all(n["source"] and n["cause"] for n in d["root_causes"]))
        d = json.loads(run(["explain", "D03", "--json"])[1])
        self.assertFalse(d["curated_notes"])
        self.assertEqual(d["root_causes"], [])
        self.assertEqual(d["tier"], "T2")

    def test_catalog_fields_are_not_invented(self):
        with open(os.path.join(DATA, "catalog.json"), encoding="utf-8") as f:
            rule = {r["id"]: r for r in json.load(f)["rules"]}["S08"]
        d = json.loads(run(["explain", "S08", "--json"])[1])
        for k, v in rule.items():
            self.assertEqual(d[k], v)
        self.assertEqual(set(d) - set(rule), {"root_causes", "curated_notes"})


class TestNotesFile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(DATA, "explain_notes.json"), encoding="utf-8") as f:
            cls.raw = f.read()
        cls.notes = json.loads(cls.raw)

    def test_rules_exist_in_catalog(self):
        self.assertTrue(set(self.notes["rules"]) <= set(_catalog_ids()))

    def test_covered_rules(self):
        self.assertEqual(sorted(self.notes["rules"]), ["S03", "S04", "S05", "S06", "S07", "S08", "S09"])
        notes = [n for v in self.notes["rules"].values() for n in v]
        self.assertEqual(len(notes), 21)                                  # a mechanism may explain several rules
        self.assertEqual(len({n["mechanism"] for n in notes}), 19)

    def test_every_note_has_a_source(self):
        for rid, items in self.notes["rules"].items():
            for n in items:
                for k in ("mechanism", "kind", "cause", "source"):
                    self.assertTrue(n.get(k), f"{rid}: {n.get('mechanism')} lacks {k}")
                self.assertIn(n["mechanism"], n["source"])

    def test_no_banned_words_or_paths(self):
        self.assertNotIn("/" + "home" + "/", self.raw)
        self.assertNotIn("github.com", self.raw)


if __name__ == "__main__":
    unittest.main()
