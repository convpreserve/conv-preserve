# ConvPreserve

**Catch silent semantic changes when gateways, proxies, and SDKs translate between LLM APIs.**

API gateways and SDK adapters let applications switch between OpenAI, Anthropic, and Gemini—but a translated request or response can pass schema validation (`HTTP 200`) while silently dropping `system` prompts, stripping tool `is_error` flags, truncating tool names, injecting reasoning budgets, or corrupting SSE streams.

`convpreserve` compares payloads before and after translation across **27 deterministic rules**—offline in milliseconds, with zero runtime dependencies, no API keys, and no LLM calls.

[Website](https://convpreserve.github.io) · [Quickstart](#quickstart) · [Test a Live Gateway](#testing-a-live-gateway) · [CI & Baselines](#ci--regression-gating) · [CLI Reference](#cli-reference) · [Paper](#paper--reproducibility)

---

## Installation

Requires **Python 3.11+**.

```bash
pip install git+https://github.com/convpreserve/conv-preserve.git
```

For local development:

```bash
git clone https://github.com/convpreserve/conv-preserve.git
cd conv-preserve
pip install -e ".[dev]"
pytest
```

---

## Quickstart

### 1. Compare a payload before and after translation

Pass the original payload (`src`) and the translated payload (`dst`). In the bundled quickstart example, an OpenAI Chat request without reasoning settings was translated into an Anthropic request with an unrequested `thinking` budget injected:

```bash
$ convpreserve check --side request \
    --src-dialect openai --dst-dialect anthropic \
    examples/quickstart/src.json examples/quickstart/dst_thinking_injected.json
S08	T1		unlicensed control thinking injected: {'type': 'enabled', 'budget_tokens': 1024}	dst:/thinking
$ echo $?
1
```

- **Exit codes**: `0` when preserved, `1` when a finding meets or exceeds `--fail-on` (`T1` by default), `2` on invalid input.
- **Clean pass**: comparing against `examples/quickstart/dst_clean.json` prints nothing and exits with `0`.
- **JSON output**: add `--json` for structured output including RFC 6901 JSON Pointer field locations:

```json
{
  "findings": [
    {
      "rule_id": "S08",
      "tier": "T1",
      "object_id": "",
      "detail": "unlicensed control thinking injected: {'type': 'enabled', 'budget_tokens': 1024}",
      "evidence": {
        "side": "dst",
        "path": "/thinking",
        "precision": "exact"
      }
    }
  ],
  "summary": {"T1": 1, "T2": 0, "T3": 0},
  "oracle": "v1.3",
  "convpreserve": "0.1.0",
  "carriers_sha256": "17d293c5bebdbd4463cd5a2056ea3879e0bd097185e3b6b7654a2bca024dabb0"
}
```

### 2. Look up any rule and known translator bug patterns

```bash
$ convpreserve explain S08
S08  CONTROL  (T1)

Summary: generation or tool-choice parameter set by the caller was dropped or altered

Known translator bug patterns:

- [control_max_tokens_dropped] (claude-code-router -> gemini)
  generationConfig is built with thinkingConfig only; request.max_tokens is never copied into it.

- [control_temperature_dropped] (claude-code-router -> gemini)
  generationConfig never receives request.temperature.
...
```

Run `convpreserve explain --list` to list all 27 rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`).

---

## Testing a Live Gateway

`convpreserve probe` tests a running local proxy or gateway end-to-end **without provider API keys**:

1. Point your gateway's upstream provider base URL to ConvPreserve's local mock server (`http://127.0.0.1:18600` by default, e.g. `OPENAI_BASE_URL=http://127.0.0.1:18600/v1`).
2. Run `probe` against your gateway's Anthropic `/v1/messages` endpoint:

```bash
convpreserve probe --gateway http://127.0.0.1:8080 --target openai --out captured.jsonl
```

ConvPreserve starts the mock upstream server on `127.0.0.1:18600`, sends 51 built-in Anthropic requests (covering system prompts, multi-turn tool calls, `is_error` tool results, thinking blocks, and sampling parameters) to `--gateway`, captures each translated request forwarded by the gateway, and checks every pair.

| Option | Default | Description |
|---|---|---|
| `--gateway URL` | *(required)* | Gateway URL accepting Anthropic Messages requests (`/v1/messages` appended if omitted). |
| `--target FORMAT` | *(required)* | Upstream format the gateway translates to: `openai`, `responses`, `gemini`, or `anthropic`. |
| `--sink-port PORT` | `18600` | Local port for the mock upstream server (`0` chooses a random free port). |
| `--model NAME` | *(per-case)* | Override the `model` field sent over HTTP while checking against the original request. |
| `--header 'K: V'` | dummy auth | Extra HTTP request header sent to the gateway (repeatable). |
| `--shapes bundled\|FILE` | `bundled` | Use the 51 built-in test requests or a custom JSON file. |
| `--out FILE.jsonl` | — | Save captured upstream requests and per-request findings to JSONL. |

---

## Supported Formats & Finding Tiers

### API formats (`--src-dialect` / `--dst-dialect`)

| Short name | Full identifier | Endpoint format |
|---|---|---|
| `openai` | `openai_chat` | OpenAI Chat Completions (`/v1/chat/completions`) |
| `responses` | `openai_responses` | OpenAI Responses API (`/v1/responses`) |
| `anthropic` | `anthropic_messages` | Anthropic Messages API (`/v1/messages`) |
| `gemini` | `gemini_generatecontent` | Google Gemini `generateContent` / `streamGenerateContent` |

### Payload types (`--side`)

| `--side` | `src` (before translation) | `dst` (after translation) |
|---|---|---|
| `request` | Client request JSON sent to the translator | Upstream request JSON emitted by the translator |
| `response` | Upstream provider response JSON | Translated response JSON returned to the client |
| `stream` | Reference response JSON or upstream SSE text | Translated SSE stream text (`.sse`) delivered to the client |

### Finding tiers

| Tier | Rules | Meaning | Default exit |
|---|---|---|---|
| **`T1`** | `S01`–`S13` | **Silent semantic loss** — avoidable change to prompts, roles, tool schemas/calls/errors, generation parameters, or stop reasons. | `1` |
| **`T2`** | `D01`–`D06` | **Target API limitation / metadata drift** — target API lacks a direct equivalent, translator injects a default parameter, or cache/usage metadata changes. | `0` (`1` with `--fail-on T2`) |
| **`T3`** | `R01`–`R08` | **Equivalent format difference** — different JSON representation with identical conversation meaning (e.g., `""` vs. `null`). | `0` (`1` with `--fail-on T3`) |

---

## CI & Regression Gating

### Batch manifests (`convpreserve batch`)

Evaluate a `.jsonl` manifest of payload pairs in one command (see [`examples/manifest.jsonl`](examples/manifest.jsonl)):

```json
{"id": "req-1", "side": "request", "src_dialect": "openai", "dst_dialect": "anthropic", "src_file": "quickstart/src.json", "dst_file": "quickstart/dst_thinking_injected.json"}
```

```bash
convpreserve batch examples/manifest.jsonl --out findings.jsonl --summary-md summary.md
```

### GitHub Actions

Use this repository directly as a composite GitHub Action (see [`examples/ci/github-workflow.yml`](examples/ci/github-workflow.yml)):

```yaml
- uses: convpreserve/conv-preserve@main
  with:
    mode: batch
    manifest: tests/fixtures/manifest.jsonl
    fail-on: T1
```

Passing `--format github` writes a Markdown report suitable for `$GITHUB_STEP_SUMMARY` or pull-request comments. `pre-commit` hooks (`convpreserve-batch`, `convpreserve-selftest`) are also provided in [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml).

### Baseline files (`--write-baseline` / `--baseline`)

Adopt `convpreserve` incrementally on existing gateways by recording known findings and failing CI only on new regressions:

```bash
# Record existing findings without failing
convpreserve batch pairs.jsonl --write-baseline convpreserve-baseline.json --fail-on none

# Fail only on newly introduced findings
convpreserve batch pairs.jsonl --baseline convpreserve-baseline.json
```

---

## CLI Reference

| Command | Purpose |
|---|---|
| `convpreserve check` | Compare one `(src, dst)` pair (`-` reads `stdin`; `--format text\|json\|github`; `--fail-on T1\|T2\|T3\|none`). |
| `convpreserve batch` | Check every pair in a `.jsonl` manifest and optionally write `--out` JSONL and `--summary-md`. |
| `convpreserve probe` | Run 51 built-in Anthropic test requests through a live gateway into a local mock server. |
| `convpreserve explain` | Show a rule's summary and known open-source translator bug patterns (`explain <RULE>` or `explain --list`). |
| `convpreserve dialects` | List supported API format names and short aliases. |
| `convpreserve selftest` | Verify the installation against 574 bundled reference cases. |
| `convpreserve version` | Print package version, ruleset versions, and the `carriers.json` SHA-256 digest. |

---

## Paper & Reproducibility

- **Ruleset versions**: `v1.3` (default) adds streamed OpenAI reasoning delta assembly and RFC 6901 JSON Pointer field locations on top of the frozen `v1.0` ruleset used for the paper's request-side tables ([`CHANGES_FROM_V100.md`](src/convpreserve/judge_v13/CHANGES_FROM_V100.md)). Pass `--oracle v1.0` (or `CONVPRESERVE_ORACLE=v1.0`) to run the frozen paper baseline.
- **Self-test & benchmarks**: Run `convpreserve selftest` (and `convpreserve selftest --oracle v1.0`) to verify all bundled reference cases, or `python bench/throughput.py` to measure single-core throughput.

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
