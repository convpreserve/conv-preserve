# Testing Conversation Preservation Across LLM APIs

LLM API translators can silently change instructions, tool interactions, error states, and generation controls, even when translated requests remain valid. ConvPreserve provides a deterministic, rule-based oracle to identify these changes and assess whether conversation information survives translation.

`convpreserve` checks whether an LLM API request, response or stream still means the same thing after a gateway or translator rewrote it from one dialect to another (OpenAI Chat, OpenAI Responses, Anthropic Messages, Gemini `generateContent`). You give it the payload the translator received and the payload it emitted. It reports findings in three tiers (T1/T2/T3). It is deterministic, has no dependencies beyond Python 3.11+ and never calls an LLM. The judge itself opens no sockets; `convpreserve probe` (below) is the one sub-command that does.

The checker ships two oracle versions. **v1.0** is the frozen judge of the paper (`--oracle v1.0`). **v1.3** (the default) is v1.0 plus three documented changes: streamed `reasoning_content` is assembled, findings carry an evidence path, and one detail text is reworded ([`CHANGES_FROM_V100.md`](src/convpreserve/judge_v13/CHANGES_FROM_V100.md)). `convpreserve version` prints both with the oracle commit and the sha256 of the rule tables they share.

## Install

Python 3.11, 3.12 or 3.13; no third-party runtime dependencies, so there is nothing to lock. Building from source (the `pip install .` line or the wheel build) fetches the build backend `setuptools>=77` from your package index; offline, install it beforehand and add `--no-build-isolation --no-index` to the `pip install` line.

```bash
# from a checkout of this repository (one command, 4 to 7 s)
pip install .                               # or: pip install -e .  for development
# or from a wheel you built yourself (the wheel is not committed; PyPI release is not published yet)
uv build --wheel -o dist .                  # or: python -m pip wheel --no-deps -w dist .
pip install dist/convpreserve-0.1.0-py3-none-any.whl
convpreserve --version                      # convpreserve 0.1.0 (oracles v1.0, v1.3; default v1.3); `convpreserve version` adds the oracle commit and the carriers.json sha256
convpreserve selftest                       # must print "400 identical / 400 total", "rule cases: 174 identical / 174 total" and PASS
convpreserve selftest --oracle v1.0         # the same for the frozen oracle
python -m convpreserve --version            # same entry point without the console script
```

## Five-minute tour

Files used below are in [`examples/`](examples/). All commands run from this directory.

**1. Check one pair.** A client sent an OpenAI Chat request without any reasoning setting. The gateway forwarded this to Anthropic:

```bash
$ convpreserve check --side request --src-dialect openai_chat --dst-dialect anthropic_messages \
      examples/quickstart/src.json examples/quickstart/dst_thinking_injected.json
S08	T1		unlicensed control thinking injected: {'type': 'enabled', 'budget_tokens': 1024}	dst:/thinking
$ echo $?
1
```

Output columns are `rule_id`, `tier`, `object_id`, `detail` and (oracle v1.3) `side:path`, tab separated. `--oracle v1.0` prints the four leading columns only. With `dst_clean.json` instead, nothing is printed and the exit code is `0`.

**2. Machine-readable output** (`--json`) adds the summary and the versions that produced it:

```bash
$ convpreserve check --json --side request --src-dialect openai_chat --dst-dialect anthropic_messages \
      examples/quickstart/src.json examples/quickstart/dst_thinking_injected.json
{
  "findings": [{"rule_id": "S08", "tier": "T1", "object_id": "", "detail": "unlicensed control thinking injected: ...", "evidence": {"side": "dst", "path": "/thinking", "precision": "exact"}}],
  "summary": {"T1": 1, "T2": 0, "T3": 0},
  "oracle": "v1.3",
  "convpreserve": "0.1.0",
  "carriers_sha256": "ed8317e655b04e13ea421a9b7f9cd6c6dde21a7113b68a5009fb597a2dd01948"
}
```

(Output of `--json` is pretty-printed, one object; the `findings` list is shortened here.) Use `-` instead of a file name to read one side from stdin.

**Where the evidence is.** Each finding carries `rule_id`, `tier`, `object_id`, `detail` and, with oracle v1.3, `evidence`: `{"side": "src" | "dst", "path": <JSON Pointer>, "precision": "exact" | "container" | "payload"}`. `side` says which of your two payloads the pointer points into. `exact` means the pointer resolves to the offending value, `container` to the object that holds it (for an absent key, the object where it is missing; `missing_key` names it), `payload` (pointer `""`) means nothing narrower was found. A stream payload is addressed as the list of its `data:` frames, 0-based and counting `[DONE]` (`/3/delta` is the `delta` object of the fourth frame). On the 400-record sample 240 of 323 findings are `exact`, 27 `container`, 56 `payload`; the evidence is computed after the verdict and never changes it. `detail` still holds the values that differ. `object_id` is set only by some rules. `--oracle v1.0` findings have no `evidence` field.

**3. Many pairs at once** (`batch`). One JSON object per line, payloads inline (`src`, `dst`) or by path (`src_file`, `dst_file`, relative to the manifest):

```json
{"id": "req-1", "side": "request", "src_dialect": "openai_chat", "dst_dialect": "anthropic_messages", "src_file": "quickstart/src.json", "dst_file": "quickstart/dst_thinking_injected.json"}
```

```bash
$ convpreserve batch examples/manifest.jsonl --out findings.jsonl --summary-md summary.md
pairs=14 errors=0 T1=26 T2=5 T3=7 pairs_with_T1=14
D03	T2	1
...
```

`findings.jsonl` repeats each manifest line with `findings` and a per-pair `summary` added. `summary.md` holds the T1/T2/T3 totals, counts per rule, and counts per (side, source dialect, target dialect). `examples/manifest.jsonl` has one pair for each of the 14 cross-dialect (side, source, target) combinations of the bundled sample that has a T1 finding (the table below).

## Worked example per dialect pair

Each row is `convpreserve check --side SIDE --src-dialect SRC --dst-dialect DST examples/SIDE/SRC__DST/src.* examples/SIDE/SRC__DST/dst.*`. Inputs were selected by [`examples/make_examples.py`](examples/make_examples.py) (smallest recorded pair with a T1 finding); they are minimal synthetic payloads.

| Side | Source → target | Example finding |
|---|---|---|
| request | anthropic_messages → openai_chat | `S09 T1` declared tool names changed (and `D03 T2`) |
| request | anthropic_messages → gemini_generatecontent | `S05 T1` lost developer text |
| request | anthropic_messages → openai_responses | `S05 T1` lost developer text |
| response | openai_chat → anthropic_messages | `S12 T1` stop/finish reason semantic class changed |
| response | openai_chat → gemini_generatecontent | `S12 T1` stop/finish reason semantic class changed |
| response | openai_chat → openai_responses | `S05 T1` lost assistant text |
| response | gemini_generatecontent → anthropic_messages | `S12 T1` stop/finish reason semantic class changed |
| response | gemini_generatecontent → openai_chat | `S12 T1` stop/finish reason semantic class changed |
| response | gemini_generatecontent → openai_responses | `S12 T1` stop/finish reason semantic class changed |
| stream | openai_chat → anthropic_messages | `S05 T1` invented assistant text; `S13 T1` stream protocol anomaly |
| stream | openai_chat → gemini_generatecontent | `S06 T1` tool call/result pairing broken; `S05`, `S12` |
| stream | gemini_generatecontent → anthropic_messages | `S05 T1` invented assistant text; `S13 T1` block pairing broken |
| stream | gemini_generatecontent → openai_chat | `S04 T1` tool-call arguments altered |
| stream | gemini_generatecontent → openai_responses | `S06 T1` tool call/result pairing broken; `S05`, `S12` |

Sample output (stream side):

```bash
$ convpreserve check --side stream --src-dialect openai_chat --dst-dialect anthropic_messages \
      examples/stream/openai_chat__anthropic_messages/src.json examples/stream/openai_chat__anthropic_messages/dst.sse
S05	T1		invented assistant text (adapter-inserted): '(continued)'
S13	T1		stream protocol anomaly: delta_after_content_block_stop:0
R08	T3		envelope id changed: 'chatcmpl-abc' -> 'msg_x'
```

### What goes in each side

| `--side` | `src` | `dst` |
|---|---|---|
| `request` | request body as the client sent it (JSON) | request body as the gateway sent it upstream (JSON) |
| `response` | response body of the upstream provider (JSON) | response body the gateway returned to the client (JSON) |
| `stream` | the reference the stream must be equivalent to: the non-stream response body (`.json`) or a raw SSE capture (any other extension) | the SSE text the client received (raw text, any extension except `.json`) |

For `stream`, a file whose name ends in `.json` is parsed as JSON; any other file is read as raw SSE text. Dialect names: `convpreserve dialects` (`anthropic_messages`, `gemini_generatecontent`, `openai_chat`, `openai_responses`; the short aliases `anthropic`, `gemini`, `openai`, `responses` also work).

### Context keys

`check --context K=V ...` (and the `"context": {...}` object of a `batch` manifest line) passes extra information to the judge. These are all the keys the judge reads:

| Key | Value | What it does |
|---|---|---|
| `object_id` | text | the label printed in the object column of every finding of that pair |
| `vendor_tokens` | `a,b` (manifest: a JSON list) | integrity tokens (thinking signatures, encrypted reasoning content) that rule `S10` counts as present in the source conversation, in addition to the tokens found in the source payload; `S10` flags a destination token only if it is in neither set |
| `disabled` | `P1,P4` (manifest: a JSON list) | ablation switch for the judge's principles, for experiments; it is not meant for normal checks |
| `non_stream_body` | parsed response body (manifest only) | for `--side stream` in a manifest: a non-stream response to compare the reassembled stream with (`S13`); not accepted on the command line, which answers with a usage error |

On the command line, `vendor_tokens` and `disabled` take a comma-separated list (spaces and empty items are dropped). `--context` takes any number of pairs, so put it after `src` and `dst`, or end the pairs with `--`. There is no key that marks a degradation as declared or accepted; to silence known findings use a baseline (below).

## Probe a running gateway

```bash
convpreserve probe --gateway http://127.0.0.1:8080 --target openai --oracle v1.0
```

`probe` starts a loopback server (the *sink*, `127.0.0.1:18600` by default) that stands in for the upstream provider, POSTs the 51 bundled Anthropic Messages request shapes to the gateway, captures the request body the gateway forwards to the sink, and judges (source shape, forwarded body) on the request side. Configure the gateway so that its upstream for the chosen `--target` is `http://127.0.0.1:18600` (any key is accepted); no provider account or key is involved, nothing leaves the machine, and the sink replies with a minimal valid non-streaming response. Only requests are judged, the inbound dialect is Anthropic Messages, and the standard library is the only dependency.

Options: `--target {openai,responses,gemini,anthropic}`; `--src-dialect anthropic`; `--shapes bundled|PATH` (a JSON file of shapes, same format as the bundled `src/convpreserve/data/probe_shapes_anthropic_51.json`); `--sink-port N` (`0` = any free port); `--model NAME` (replaces the model in the posted body only; the judge still sees the original shape); `--header 'Name: value'` (repeatable; defaults: `content-type`, `anthropic-version: 2023-06-01`, `x-api-key` and `authorization` set to the dummy value `probe`); `--oracle {v1.0,v1.3}`; `--fail-on {T1,T2,T3,none}`; `--json`; `--out findings.jsonl` (one line per shape, with the captured upstream body); `--timeout S`; `--keep-sink`. The inbound path comes from `--gateway`: no path means `/v1/messages`, a URL ending in `/messages` is used as is, any other path gets `/v1/messages` appended (`http://127.0.0.1:8080/anthropic` posts to `/anthropic/v1/messages`).

Output is tab-separated like `check`, with the shape id as leading column, then one summary line:

```text
$ convpreserve probe --gateway http://127.0.0.1:8080 --target openai --oracle v1.0
G351    S06     T1      G351    tool call/result reference pairing broken: calls ['#0']->[], results ['#0']->[]
G351    S05     T1      G351    lost assistant tool_call 'get_weather|{"city":"Paris"}'
G351    S08     T1      G351    control tool_choice changed/dropped: {'mode': 'any'} -> None
...
T1 cells: 51/51 (judged 51/51, no wire 0)
```

(the sample is from a toy gateway that forwards message text only.) A shape for which no upstream request reached the sink in time is reported on stderr as `no wire: <shape id> (...)` and counts in `no wire`. With `--json` the output has `shapes`, `reached` (upstream request seen), `judged` (reached and judged without error), `no_wire`, `t1_cells`, `t2_cells`, `t3_cells` (shapes with at least one finding of that tier; the counts are not exclusive) and a `per_shape` list with the HTTP status, upstream path and findings of each shape. Exit codes are those above; `2` also covers a sink port that cannot be bound and a gateway that refuses the initial connection.

The study's gateway table uses the same 51 shapes and Oracle v1.0 on the request side, so `T1 cells: N/51` is the figure reported for that path; the table counts a cell only when the gateway answered HTTP 200, while `probe` also judges a wire that arrived with another status and prints the status per shape in `--json`.

Gateway configurations that send their upstream to the sink (the configurations used in the study):

- **Bifrost** (`config.json`): providers `openai`, `anthropic`, `gemini` and a custom provider `openaichat` (`base_provider_type: openai`, `allowed_requests: {chat_completion: true}`), each with `"network_config": {"base_url": "http://127.0.0.1:18600", "max_retries": 0}` and a key `x`; run `convpreserve probe --gateway http://127.0.0.1:8080/anthropic --header 'x-api-key: x' --target openai --model openaichat/gpt-4o` (`--target gemini --model gemini/gemini-2.5-flash` for Gemini).
- **new-api**: channels of type 1 (OpenAI, model `gpt-4o`), 14 (Anthropic) and 24 (Gemini, model `gemini-2.5-flash`) with `base_url: http://127.0.0.1:18600` and key `x`; create a token in the admin UI and run `convpreserve probe --gateway http://127.0.0.1:3000 --header 'x-api-key: sk-…' --target openai --model gpt-4o`.
- **Portkey gateway** (no config file; headers select the upstream): `convpreserve probe --gateway http://127.0.0.1:8787 --target openai --model gpt-4o --header 'x-portkey-provider: openai' --header 'x-portkey-custom-host: http://127.0.0.1:18600/v1' --header 'authorization: Bearer x'`; for Gemini use `--target gemini --model gemini-2.5-flash --header 'x-portkey-provider: google' --header 'x-portkey-custom-host: http://127.0.0.1:18600'`.
- **CLIProxyAPI** (`config.yaml`): a client key under `access.api-keys` (here `recheck-client-key`) and one upstream block, either `openai-compatibility: [{name: mock-openai, base-url: "http://127.0.0.1:18600/v1", keys: [{api-key: dummy}], models: [{name: gpt-4o, alias: claude-3-5-sonnet-20241022}]}]` or `gemini: [{name: mock-gemini, base-url: "http://127.0.0.1:18600", keys: [{api-key: dummy}], models: [{name: gemini-2.5-flash, alias: claude-3-5-sonnet-20241022}]}]`; run `convpreserve probe --gateway http://127.0.0.1:8317 --header 'authorization: Bearer recheck-client-key' --target openai --model claude-3-5-sonnet-20241022`.

Ports above are examples; use the port your instance listens on.

## CI integration

`check` and `batch` take `--format {text,json,github}` (default `text`; `--json` stays an alias of `--format json`, and giving both `--json` and another `--format` is a usage error, exit `2`). The exit codes and the text and JSON output of earlier versions are unchanged.

- `--format github` prints GitHub-flavoured Markdown that you can paste into a pull-request comment or append to `$GITHUB_STEP_SUMMARY`: a heading with the T1/T2/T3 counts and the oracle version, one table of findings (rule id, tier, path or field, one-line meaning of the rule, detail), and `No findings.` when the pair is clean. More than 50 findings are folded into a `<details>` block. For `batch`, the path column is prefixed with the manifest id of the pair.
- `batch --format json` prints the run statistics as one JSON object (`pairs`, `errors`, `breaching_pairs`, `fail_on`, `summary` with the T1/T2/T3 finding counts, `pairs_with_tier`, `rules`, `by_pair`, `error_lines`, `oracle`, `convpreserve`, `carriers_sha256`).

```
$ convpreserve check --side request --src-dialect openai_chat --dst-dialect anthropic_messages \
    examples/quickstart/src.json examples/quickstart/dst_thinking_injected.json --format github
### convpreserve check: T1 1, T2 0, T3 0 (oracle v1.3)

convpreserve 0.1.0. `examples/quickstart/src.json` to `examples/quickstart/dst_thinking_injected.json` (request, openai_chat to anthropic_messages).

| Rule | Tier | Path / field | Meaning | Detail |
|---|---|---|---|---|
| `S08` | T1 | dst:/thinking | a control the source SET is dropped or changed without a documented equivalent carrier | unlicensed control thinking injected: {'type': 'enabled', 'budget_tokens': 1024} |
```

### GitHub Action

`action.yml` (at the repository root, so `uses: convpreserve/conv-preserve@main` works) is a composite action. It installs the `convpreserve` package that sits next to it (`pip install` of the action path; as for any source install, pip fetches the build requirement named in `pyproject.toml`; the package itself is not taken from an index), runs `check` or `batch`, appends the `--format github` result to the job summary, and fails the step according to `fail-on`. A copyable workflow is in `examples/ci/github-workflow.yml`.

| Input | Default | Meaning |
|---|---|---|
| `mode` | `check` | `check` (one pair) or `batch` (a JSONL manifest) |
| `src`, `dst` | empty | `mode=check`: source payload file and the file the translation layer produced |
| `side` | `request` | `mode=check`: `request`, `response` or `stream` |
| `src-dialect`, `dst-dialect` | empty | `mode=check`: dialect names (`convpreserve dialects`) |
| `manifest` | empty | `mode=batch`: JSONL manifest path |
| `oracle` | empty (package default) | `v1.0` or `v1.3` |
| `fail-on` | `T1` | lowest tier that fails the step: `T1`, `T2`, `T3` or `none` |

| Output | Meaning |
|---|---|
| `exit-code` | `0` no finding at or above `fail-on`, `1` at least one, `2` usage or input error (the step also exits with this code) |
| `t1`, `t2`, `t3` | number of findings per tier (not set when `exit-code` is `2`) |

Use `continue-on-error: true` on the step if later steps should run after a failed check, and read the outputs through the step id.

### pre-commit

`.pre-commit-hooks.yaml` (at the repository root) defines two hooks for [pre-commit](https://pre-commit.com); a copyable `.pre-commit-config.yaml` is in `examples/ci/pre-commit-config.yaml`.

- `convpreserve-batch`: runs `convpreserve batch FILE --fail-on T1` for every changed `*.jsonl` file whose name contains `convpreserve` (override `files:` for another naming scheme). A failing manifest does not stop the others from being checked; the hook fails if any of them has a T1 finding or cannot be judged.
- `convpreserve-selftest`: runs `convpreserve selftest` for both oracles (`always_run`, no file arguments).

The hooks use `language: python`, so pre-commit installs this package into its own environment from the checked-out repository.

### Baseline of known findings

A team that cannot fix every finding at once can record the current ones and fail only on new ones. `check` and `batch` take `--write-baseline PATH` (write the fingerprints of all findings of this run) and `--baseline PATH` (read them; the environment variable `CONVPRESERVE_BASELINE` does the same, which is how the GitHub Action can use a baseline: set it in the workflow `env:`; this was checked by running the action's step locally with the variable set, not on GitHub).

```
convpreserve batch pairs.jsonl --write-baseline convpreserve-baseline.json --fail-on none   # accept the current state
convpreserve batch pairs.jsonl --baseline convpreserve-baseline.json                        # later runs: fail on new findings only
```

- A fingerprint is: rule id, payload side, source dialect, destination dialect, the location (the `dst:/path` evidence column of oracle v1.3; for a v1.0 finding the object id), and the pair (batch: the manifest `id`, or `line N` without one; check: the source and destination file names without directory). The `detail` text is not part of it. Several findings with the same fingerprint are covered by one entry. The file is JSON, sorted, one entry per fingerprint; keep it in version control and review changes to it.
- A baselined finding is still printed. Text: a last column `[baselined]`. JSON: `"baselined": true` on the finding. GitHub: the heading and tables count and list only new findings, the subject line states how many findings were baselined, and the baselined ones are listed in a folded block.
- A baselined finding does not count for the exit code, for `--fail-on`, for the tier counts (`summary`, `t1`/`t2`/`t3`, batch `rules` and `by_pair`) or for `pairs_with_T1`. `check --format json` and `batch --format json` add `baselined` (number) and `baseline` (file name). `batch` text output adds ` baselined=N` at the end of its totals line.
- `--write-baseline` does not change the output or the exit code; add `--fail-on none` for the run that creates the file. It is written even when some manifest lines fail (those lines contribute nothing, so the next run lists their findings as new).
- A baseline written with one oracle version does not match the other (v1.0 findings carry no path); a note on stderr says so. An unreadable or malformed baseline file is a usage error (exit `2`).
- Without `--baseline` (and without `CONVPRESERVE_BASELINE`) the text and JSON output and the exit codes are byte-identical to before.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | no finding at or above `--fail-on` (default `T1`) |
| `1` | at least one finding at or above `--fail-on` |
| `2` | usage, parse or I/O error; for `batch`, also: at least one manifest line could not be judged (the other lines are still judged and written) |

`--fail-on T2` fails on T1 and T2; `--fail-on none` never fails on findings (use it to collect findings only). `selftest` returns `0` on PASS and `1` on any mismatch.

## Reading T1 / T2 / T3

- **T1 — silent semantic change.** The model (or the client) sees a different conversation, different controls, a different tool contract, or a different causal history than the source expressed, and no error is raised. Fix these.
- **T2 — documented or declared degradation, or an operational difference.** Examples: an adapter default injected where the source left the control unset; system text lowered to a user/developer message; caching or `service_tier` hints dropped; usage numbers changed; model name echoed differently; a stop reason duplicated into a second carrier.
- **T3 — representation only.** Same meaning, different wire form: absent vs `null`; `""` vs `null` next to tool calls; string vs one-element text array; adjacent text blocks split or merged; unknown custom keys.

Rule identifiers (`S03`, `S12`, `D04`, `R08`, …) are defined in the paper's appendix; `convpreserve explain --list` prints them all.

## Explain a rule

```
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
  Source: artifacts/g4/mechanism_code_locations/out/mechanism_code_locations_2026-10-12T0939Z.json (mechanism control_max_tokens_dropped); paper/tables/mechanisms.tex; paper/CLAIMS.md C-604
...
```

`convpreserve explain RULE` prints what the rule catalogue (`data/catalog.json`) says about a rule (name, tier, principle, the earlier id it replaces, the meaning) followed by curated root causes. `RULE` is a rule id (`S08`) or a rule name (`CONTROL`), case-insensitive. `convpreserve explain --list` prints every rule id, one per line (`--list --json` adds name and tier); `convpreserve explain RULE --json` prints the same content as one JSON object (`root_causes` is a list, empty when there are no notes). An unknown rule prints the closest rule ids to stderr and exits `2`; so does a call with no rule.

Root causes exist only where the study located the cause in translator code: `data/explain_notes.json` covers `S03` to `S09` (21 notes, 19 mechanisms), and every note carries a `source` field naming the study file, the paper table and the claim row it comes from. For the other rules `explain` prints `no curated notes yet`; nothing is guessed. The notes describe the translators and releases pinned in the study and say nothing about other versions.

## What it does not do

- No live proxying and no capture in `check` and `batch`: they judge payloads you already hold. `probe` is the exception: it talks to a gateway you name and to a loopback sink it starts itself.
- No LLM calls. Apart from `probe`, no network access.
- No schema-validation-only mode: a pair of well-formed but unrelated payloads yields findings, not a schema error.

## Known limitations

- **Oracle v1.0 only, stream side, OpenAI Chat:** `delta.reasoning_content` (and `delta.reasoning`) is not assembled, so reasoning that a translator emitted correctly is reported as `S05`. Oracle v1.3 assembles both carriers (`tests/test_oracle_v13.py`); `tests/test_rule_coverage.py::TestKnownLimitation` pins the v1.0 behaviour.
- **Both oracles:** refusal deltas and `reasoning_details` in streams are not assembled; `redacted_thinking` has no OpenAI Chat carrier and is reported as lost (`S05`).
- **Evidence** locates the finding in the payload by searching for what the finding names; about one finding in six (stream reassembly, absent usage, dropped content) gets the whole payload (`precision: payload`).
- The v1.0 `S10` ablation text contains a word that v1.3 rewords; selftest compares detail strings per oracle.

## Tests

```bash
pip install -e . pytest
python -m pytest tests          # under a minute
convpreserve selftest           # replays the bundled 400-record sample and 174 per-rule cases
```

`convpreserve selftest` replays a stratified 400-record sample bundled in the wheel (`tests/expected/MANIFEST.md` records its sha256 and how it was generated) and 174 per-rule cases (at least three positive and three negative for each of the 27 rules; `tests/README.md` has the generation method). `tests/test_rule_coverage.py` holds one firing pair and one clean control for each rule that the sample never fires (S01, S11, D01, D06, R02, R06, R07), and `tests/test_oracle_v13.py` covers the differences between the two oracles.

The equivalence and run-of-record tests against the frozen v1.0 pipeline (`test_equivalence_v100.py`, `test_runofrecord_v100.py`) and the scripts `check_gate_a.py` and `report_v13_vs_v10*.py` read the study's capture archive, which is not part of this repository; without it they are reported as skipped. Paper numbers come from oracle v1.0: pass `--oracle v1.0` (or set `CONVPRESERVE_ORACLE=v1.0`) when comparing with the paper.

## Throughput

`python bench/throughput.py` judges the bundled 400-pair sample 5 times (after one warm-up) with the frozen v1.0 judge (`convpreserve.judge`) in one process and reports the median pairs per second together with the CPU, Python version and sample hash.

That number is the time of `compare()` alone on 400 payload pairs that are already parsed in memory: it excludes process start-up, reading the files and `json.loads`. `python bench/scale_throughput.py --json OUT.json` measures the stages of a real run separately (start-up, read, parse, judge, `convpreserve batch` wall clock) at 400, 4,000 and 40,000 pairs, for single requests with long histories, and for both oracles; `--render OUT.json ...` prints the tables from saved results. Results of one run on one machine are in `bench/out/`; the default oracle v1.3 is slower than v1.0 in that measurement.

## Versioning

Oracle v1.0 is frozen. Bug fixes to the oracle logic go to a new oracle version, never silently into v1.0; v1.3 is the initial such version (see `CHANGES_FROM_V100.md`). The `convpreserve` version tracks packaging and CLI changes over the frozen oracle. `VENDORED.md` lists the frozen judge files with their hashes; `tests/test_vendoring.py` checks them.

## License

Apache-2.0 (`LICENSE`).

## Citation

```bibtex
@article{convpreserve2026,
  title   = {Testing Conversation Preservation Across LLM APIs: A Rule-Based Oracle for Detecting Silent Information Loss},
  author  = {Anonymous},
  journal = {Under review},
  year    = {2026}
}
```
