# Differences Between Ruleset v1.3 (Default) and v1.0 (Paper Baseline)

`convpreserve` uses `v1.3` by default (`convpreserve.judge_v13`). Pass `--oracle v1.0` (or set `CONVPRESERVE_ORACLE=v1.0`) to run the frozen ruleset from the paper's request-side evaluation (`convpreserve.judge`).

| # | Change in `v1.3` | Module | Effect |
|---|---|---|---|
| 1 | Reassembles streamed `delta.reasoning_content` and `delta.reasoning` chunks in OpenAI Chat SSE streams | `ir.py` | Avoids false `S05` findings on OpenAI reasoning streams and checks reasoning preservation on streams. |
| 2 | Attaches RFC 6901 JSON Pointer `evidence` locations (`side`, `path`, `precision`) to every finding | `judge.py`, `evidence.py` | Adds field-level locations; never alters rule verdicts. |
| 3 | Normalizes `S10` sentinel diagnostic wording | `judge.py` | Diagnostic text only; rule logic and tiers are unchanged. |
