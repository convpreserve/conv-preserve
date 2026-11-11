# Test Suite

Run all tests from the repository root:

```bash
pip install -e ".[dev]"
pytest
```

| Test module | Coverage |
|---|---|
| `test_cli.py` | Exit codes (`0`/`1`/`2`), `stdin`, `batch` manifests, `--version`, Markdown summaries |
| `test_oracle_v13.py` | Streamed reasoning assembly, RFC 6901 JSON Pointer locations, `judge_v13/SHA256SUMS` |
| `test_rule_coverage.py` | Positive and clean-control pairs across rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`) |
| `test_probe.py` | Local mock upstream server, gateway request capture, timeouts, and JSONL output |
| `test_explain.py` | Rule lookup by ID or name, `--list`, `--json`, and translator bug pattern metadata |
| `test_baseline.py` | Baseline fingerprint generation (`--write-baseline`) and regression filtering (`--baseline`) |
| `test_report_github.py` | GitHub Step Summary Markdown (`--format github`), `action.yml`, and `pre-commit` hooks |
| `test_context_cli.py` | `--context` key parsing and CLI output stability |
