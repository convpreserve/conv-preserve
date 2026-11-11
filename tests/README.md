# convpreserve tests

Run from the repository root: `python -m pytest tests` (under a minute). Tests that read the study's capture archive (Levels A and B, the v1.3 regression on committed captures) are reported as skipped when it is absent.

| File | What it checks |
|---|---|
| `test_cli.py` | exit codes, stdin, batch, `--oracle`, `--version`, evidence column |
| `test_vendoring.py` | `judge/` and `data/` equal the hashes in `VENDORED.md` (Oracle v1.0 is untouched) |
| `test_equivalence_v100.py`, `test_runofrecord_v100.py` | Levels A and B: equivalence with the frozen v1.0 pipeline; run of record for sealed sets 9-14 |
| `test_rule_coverage.py` | firing pair and clean control for S01, S11, D01, D06, R02, R06, R07 on v1.0, plus the pinned v1.0 stream limitation |
| `test_oracle_v13.py` | the three v1.3 changes, FP-4 regression on committed captures, `judge_v13/SHA256SUMS`, oracle selection |
| `check_gate_a.py` | Gate A and the 2026-10b gateway re-judge, with the v1.0 judge |
| `report_v13_vs_v10.py` | cells whose findings differ between v1.0 and v1.3 on committed captures |

## `convpreserve selftest`

`convpreserve selftest [--oracle v1.0|v1.3]` replays three bundled files and prints `PASS` only if all agree.

| File (`src/convpreserve/data/`) | Content | sha256 |
|---|---|---|
| `selftest_sample.json.gz` | 400 recorded calls with the recorded v1.0 findings (`tests/gen_selftest_sample.py`, `expected/MANIFEST.md`) | `27460b7d824b7839d07e5b916ed224742719d5f0024cd5174877181058405809` |
| `selftest_expected_v13.json.gz` | the v1.3 findings, evidence included, for the same 400 records (`tests/gen_selftest_expected_v13.py`) | `3c0c8f91b15485af8f30ff3492ddae8c2c0b7d482c29b7e8cc60e4d0b0028701` |
| `selftest_rule_cases.json.gz` | 174 per-rule cases with the expected rule set of each oracle (`tests/gen_rule_cases.py`) | `b1d206b5768c5fa774b9932422c11730493d8e5677870a447701ae5c3c3d2237` |

The 400-record sample is not regenerated: its hash underlies the throughput benchmark (figure G7) and the 400/400 v1.0 selftest. The per-rule cases are a separate file so that they can be extended without touching it.

## How the per-rule cases are generated (`gen_rule_cases.py`)

Target: at least three positive and three negative cases for each of the 27 rules S01-S13, D01-D06, R01-R08.

1. **Positives.** The sample records whose recorded v1.0 findings contain the rule, ordered by payload size, smallest taken, up to three. Where the sample has fewer than three (S01, S02, S07, S08, S11, D01, D02, D06, R02, R05, R06, R07), hand-written minimal pairs (in the script) fill up to three or more; each is one targeted edit of a clean payload, in the style of `test_rule_coverage.py`.
2. **Negatives.** For rules with hand-written pairs: documented-equivalent pairs (identity, same schema, in-order results, no injected control). For every rule: an identity pair built from the leading positive's source payload (skipped when an oracle reports a finding on it, which happens for a few stream fixtures in the sample that are not well-formed payloads of their side), then the smallest clean sample records (no v1.0 finding) on a side where the rule can fire, until there are three.
3. **Expected values.** The set of rule ids each oracle returns, recorded from the judges. The generator refuses to write a case if a positive does not fire its target rule or a negative does, under either oracle. The expectations pin what the oracles do; no verdict is chosen by hand.
4. **Reproducibility.** The output is deterministic (sorted keys, gzip mtime 0). `python tests/gen_rule_cases.py --check` regenerates in memory and compares with the committed file; `python tests/gen_selftest_expected_v13.py --check` does the same for the v1.3 expectations.

Counts per rule are printed by `python tests/gen_rule_cases.py`. In the current file v1.0 and v1.3 expect the same rule set in every case; the v1.3 differences are tested in `test_oracle_v13.py` (streams that carry reasoning).
