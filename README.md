# ConvPreserve

**Deterministic test oracle for silent semantic changes across LLM API translators.**

LLM gateways and SDK adapters translate requests, responses, and SSE streams across OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Google Gemini `generateContent`. Even when translated payloads are syntactically valid and return `HTTP 200`, adapters can silently drop instructions, alter tool contracts, inject sampling or reasoning controls, or corrupt streamed replies.

`convpreserve` normalizes source and translated payloads into a vendor-neutral conversation representation and evaluates **27 deterministic preservation rules** across three severity tiers (`T1` / `T2` / `T3`)—offline, with zero third-party runtime dependencies and no LLM calls.

[Website](https://convpreserve.github.io) · [Quickstart](#quickstart) · [CLI Reference](#cli-reference) · [Gateway Probe](#probing-a-live-gateway) · [CI Integration](#ci-integration)

---

## Installation

Requires **Python 3.11+**.

```bash
pip install git+https://github.com/convpreserve/conv-preserve.git
```

For local development and running the test suite:

```bash
git clone https://github.com/convpreserve/conv-preserve.git
cd conv-preserve
pip install -e ".[dev]"
convpreserve selftest
```

---

## Quickstart

### 1. Check a single translated pair

Compare the payload a translator received (`src`) against the payload it emitted (`dst`). Below, an OpenAI Chat request without reasoning settings was translated into an Anthropic Messages request with an unrequested `thinking` budget injected:

```bash
$ convpreserve check --side request \
    --src-dialect openai_chat --dst-dialect anthropic_messages \
    examples/quickstart/src.json examples/quickstart/dst_thinking_injected.json
S08	T1		unlicensed control thinking injected: {'type': 'enabled', 'budget_tokens': 1024}	dst:/thinking
$ echo $?
1
```

- **Tab-separated output**: `rule_id`, `tier`, `object_id`, `detail`, and (in default oracle `v1.3`) `side:json_pointer`.
- **Clean translation**: comparing against `examples/quickstart/dst_clean.json` prints nothing and exits with code `0`.
- **Standard input**: pass `-` as either `src` or `dst` to read a payload from `stdin`.

Pass `--json` (or `--format json`) for structured output with RFC 6901 JSON Pointer locations:

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
  "carriers_sha256": "98eea551338f242fb7184114d0acd26e6988db4b1a87eabf43ce98381a877fdc"
}
```

### 2. Evaluate a JSONL batch manifest

Evaluate a suite of payload pairs in one run using inline payloads (`src`, `dst`) or relative file paths (`src_file`, `dst_file`):

```json
{"id": "req-1", "side": "request", "src_dialect": "openai_chat", "dst_dialect": "anthropic_messages", "src_file": "quickstart/src.json", "dst_file": "quickstart/dst_thinking_injected.json"}
```

```bash
$ convpreserve batch examples/manifest.jsonl --out findings.jsonl --summary-md summary.md
pairs=14 errors=0 T1=26 T2=5 T3=7 pairs_with_T1=14
```

### 3. Inspect rule definitions and known translator root causes

```bash
$ convpreserve explain S08
S08  CONTROL  (T1)

Meaning: a control the source SET is dropped or changed without a documented equivalent carrier

Name: CONTROL
Tier: T1
Principle: P1
Replaces (earlier id): V08a

Common root causes (translators and releases pinned in the study):

- [control_max_tokens_dropped] omission; paths: musistudio->gemini; 51 cells (51 silent)
  generationConfig is built with thinkingConfig only; request.max_tokens is never copied into it.
  Source: Study mechanism catalogue (control_max_tokens_dropped)
...
```

---

## Supported Dialects & Inspection Sides

### Dialects (`convpreserve dialects`)

| Canonical identifier | Short alias | Wire format |
|---|---|---|
| `openai_chat` | `openai` | OpenAI Chat Completions (`/v1/chat/completions`) |
| `openai_responses` | `responses` | OpenAI Responses API (`/v1/responses`) |
| `anthropic_messages` | `anthropic` | Anthropic Messages API (`/v1/messages`) |
| `gemini_generatecontent` | `gemini` | Google Gemini `generateContent` / `streamGenerateContent` |

### Inspection sides (`--side`)

| `--side` | `src` input | `dst` input |
|---|---|---|
| `request` | Client request body sent to the translator (`.json`) | Upstream request body emitted by the translator (`.json`) |
| `response` | Upstream provider response body (`.json`) | Translated response body returned to the client (`.json`) |
| `stream` | Reference non-streaming response (`.json`) or upstream SSE capture | Translated SSE stream text delivered to the client (`.sse` / raw text) |

---

## Severity Tiers & Exit Codes

### Finding tiers

| Tier | Category | Description |
|---|---|---|
| **`T1`** | **Silent semantic change** | Undeclared change to messages, roles, tool declarations/calls/results, generation controls, reasoning continuity, or stop reasons (`S01`–`S13`). |
| **`T2`** | **Declared degradation / operational drift** | Documented capability loss, adapter-injected default where unset in source, role demotion, or dropped cache/usage metadata (`D01`–`D06`). |
| **`T3`** | **Representation only** | Wire-format normalization that preserves conversation semantics, such as `""` vs. `null` or merged adjacent text chunks (`R01`–`R08`). |

### Exit codes

| Exit code | Condition |
|---|---|
| `0` | No findings at or above `--fail-on` (default: `T1`) |
| `1` | At least one finding at or above `--fail-on` |
| `2` | CLI usage error, malformed input JSON/SSE, or unreadable file |

---

## CLI Reference

| Command | Summary |
|---|---|
| `convpreserve check` | Compare one `(src, dst)` payload pair (`--format text\|json\|github`, `--fail-on T1\|T2\|T3\|none`). |
| `convpreserve batch` | Evaluate a JSONL manifest of pairs and emit per-pair findings (`--out`) and Markdown tables (`--summary-md`). |
| `convpreserve probe` | Send 51 bundled Anthropic request shapes through a live gateway into a local loopback sink. |
| `convpreserve explain` | Print rule metadata and known translator root causes (`explain <RULE>` or `explain --list [--json]`). |
| `convpreserve selftest` | Replay the 400-pair stratified sample and 174 per-rule unit cases (`--oracle v1.0\|v1.3`). |
| `convpreserve dialects` | Print supported dialect names and aliases. |
| `convpreserve version` | Print package version, available oracles, and the `carriers.json` SHA-256 digest. |

### Optional `--context` keys (`check` and `batch`)

| Key | Format | Purpose |
|---|---|---|
| `object_id` | `string` | Identifier echoed in the `object_id` column of every finding for the pair. |
| `vendor_tokens` | `tok1,tok2` (JSON list in manifests) | Integrity tokens (e.g., thinking signatures) already present in prior turns so `S10` treats them as licensed. |
| `disabled` | `P1,P4` (JSON list in manifests) | Disable specific oracle principles (`P1`–`P4`) for ablation experiments. |
| `non_stream_body` | JSON object *(manifest only)* | Non-streaming response body compared against a reassembled SSE stream under `S13`. |

---

## Probing a Live Gateway

`convpreserve probe` tests a running translation gateway end-to-end on the request side without upstream API keys or external network traffic. It starts a local HTTP loopback sink (`127.0.0.1:18600` by default), posts 51 contract-valid Anthropic Messages requests to the gateway, captures the translated upstream requests arriving at the sink, and judges each `(source, forwarded)` pair.

```bash
convpreserve probe --gateway http://127.0.0.1:8080 --target openai --oracle v1.0
```

| Option | Default | Description |
|---|---|---|
| `--gateway URL` | *(required)* | Base URL of the gateway's Anthropic Messages ingress (appends `/v1/messages` if needed). |
| `--target DIALECT` | *(required)* | Expected upstream dialect: `openai`, `responses`, `gemini`, or `anthropic`. |
| `--shapes bundled\|PATH` | `bundled` | Request suite to send (51 bundled shapes or a custom JSON file). |
| `--sink-port PORT` | `18600` | Local loopback port for the mock upstream server (`0` selects an ephemeral port). |
| `--model NAME` | *(shape value)* | Override the `model` field in posted HTTP requests while judging against the original shape. |
| `--header 'K: V'` | dummy auth | Repeatable HTTP header passed to the gateway. |
| `--out FILE.jsonl` | — | Save captured upstream bodies and findings per shape. |
| `--timeout SEC` | `10.0` | Per-request timeout in seconds. |

---

## CI Integration

### GitHub Actions

This repository is a composite GitHub Action (`action.yml`). Add it directly to `.github/workflows/` (see [`examples/ci/github-workflow.yml`](examples/ci/github-workflow.yml)):

```yaml
- uses: convpreserve/conv-preserve@main
  with:
    mode: batch
    manifest: tests/fixtures/manifest.jsonl
    fail-on: T1
```

Running with `--format github` writes a Markdown summary table suitable for `$GITHUB_STEP_SUMMARY` or pull-request comments.

### pre-commit

Configure [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml) in `.pre-commit-config.yaml` (see [`examples/ci/pre-commit-config.yaml`](examples/ci/pre-commit-config.yaml)):

```yaml
repos:
  - repo: https://github.com/convpreserve/conv-preserve
    rev: main
    hooks:
      - id: convpreserve-batch
      - id: convpreserve-selftest
```

### Baseline gating (`--write-baseline` / `--baseline`)

Record existing findings in version control so CI fails only on newly introduced regressions:

```bash
# Snapshot current findings without failing the build
convpreserve batch pairs.jsonl --write-baseline convpreserve-baseline.json --fail-on none

# Subsequent CI runs fail only on new findings (or set CONVPRESERVE_BASELINE)
convpreserve batch pairs.jsonl --baseline convpreserve-baseline.json
```

---

## Bundled Examples

[`examples/`](examples/) contains minimal synthetic cross-dialect pairs across `request`, `response`, and `stream` (indexed in [`examples/manifest.jsonl`](examples/manifest.jsonl)):

| Side | Source → Target | Representative `T1` finding |
|---|---|---|
| `request` | `anthropic_messages → openai_chat` | `S09` declared tool names truncated |
| `request` | `anthropic_messages → gemini_generatecontent` | `S05` developer/system instruction dropped |
| `request` | `anthropic_messages → openai_responses` | `S05` developer/system instruction dropped |
| `response` | `openai_chat → anthropic_messages` | `S12` `content_filter` stop reason lost |
| `response` | `openai_chat → gemini_generatecontent` | `S12` stop/finish reason class altered |
| `response` | `openai_chat → openai_responses` | `S05` assistant message text dropped |
| `response` | `gemini_generatecontent → anthropic_messages` | `S12` stop/finish reason class altered |
| `response` | `gemini_generatecontent → openai_chat` | `S12` stop/finish reason class altered |
| `response` | `gemini_generatecontent → openai_responses` | `S12` stop/finish reason class altered |
| `stream` | `openai_chat → anthropic_messages` | `S05` adapter-inserted text; `S13` delta after block stop |
| `stream` | `openai_chat → gemini_generatecontent` | `S06` tool call/result pairing broken |
| `stream` | `gemini_generatecontent → anthropic_messages` | `S05` adapter-inserted text; `S13` block pairing broken |
| `stream` | `gemini_generatecontent → openai_chat` | `S04` tool-call JSON arguments corrupted |
| `stream` | `gemini_generatecontent → openai_responses` | `S06` tool call/result pairing broken |

---

## Oracle Versions & Benchmarks

- **Oracle `v1.3` (default)**: Assembles streamed OpenAI Chat `reasoning_content` / `reasoning` deltas and attaches RFC 6901 JSON Pointer `evidence` paths to every finding ([`CHANGES_FROM_V100.md`](src/convpreserve/judge_v13/CHANGES_FROM_V100.md)).
- **Oracle `v1.0` (frozen baseline)**: Exact frozen judge used for the paper's request-side evaluation. Select via `--oracle v1.0` or `CONVPRESERVE_ORACLE=v1.0`.
- **Benchmarks**: Run `python bench/throughput.py` for single-core in-memory verdict throughput on the 400-pair sample, or `python bench/scale_throughput.py --json out.json` for end-to-end stage breakdowns across 400–40,000 pairs.

---

## License

Licensed under the [Apache License, Version 2.0](LICENSE).
