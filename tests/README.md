# Test Suite

Run from the repository root:

```bash
pip install -e ".[dev]"
pytest
```

## Overview

| Test module | Coverage |
|---|---|
| `test_cli.py` | Exit codes (`0`/`1`/`2`), `stdin` handling, `batch` manifests, `--oracle` selection, `--version`, Markdown summaries |
| `test_oracle_v13.py` | Streamed reasoning assembly, RFC 6901 JSON Pointer evidence resolution, `judge_v13/SHA256SUMS` integrity |
| `test_rule_coverage.py` | Positive and clean-control pairs across rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`) |
| `test_probe.py` | Loopback HTTP sink, gateway request capture, timeout handling, and per-shape JSON output |
| `test_explain.py` | Rule lookup by ID or name, `--list`, `--json`, and curated root-cause metadata |
| `test_baseline.py` | Fingerprint generation (`--write-baseline`), regression filtering (`--baseline`), and exit-code suppression |
| `test_report_github.py` | GitHub Step Summary Markdown formatting (`--format github`) and CI workflow templates |
| `test_context_cli.py` | `--context` key parsing and output stability |

## Bundled Reference Archives (`convpreserve selftest`)

| Archive (`src/convpreserve/data/`) | Description | SHA-256 |
|---|---|---|
| `selftest_sample.json.gz` | 400 stratified payload pairs across all sides and dialects with pinned `v1.0` verdicts | `27460b7d824b7839d07e5b916ed224742719d5f0024cd5174877181058405809` |
| `selftest_expected_v13.json.gz` | Expected `v1.3` verdicts and JSON Pointer evidence for the 400-pair sample | `3c0c8f91b15485af8f30ff3492ddae8c2c0b7d482c29b7e8cc60e4d0b0028701` |
| `selftest_rule_cases.json.gz` | 174 targeted positive and negative unit cases covering all 27 rules | `b1d206b5768c5fa774b9932422c11730493d8e5677870a447701ae5c3c3d2237` |
