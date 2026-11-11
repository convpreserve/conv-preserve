# Changelog

## 0.1.0

First public release.

- `convpreserve check` and `convpreserve batch`: judge one payload pair or a JSONL manifest of pairs on the request, response or stream side, for OpenAI Chat, OpenAI Responses, Anthropic Messages and Gemini `generateContent`.
- Two oracles: v1.0 (the frozen judge of the paper, `--oracle v1.0`) and v1.3 (default; assembles streamed reasoning, adds an evidence path to each finding; see `src/convpreserve/judge_v13/CHANGES_FROM_V100.md`).
- `convpreserve probe`: posts 51 bundled Anthropic Messages request shapes to a running gateway and judges what it forwards to a loopback sink.
- `convpreserve explain`: rule catalogue with curated root causes for S03 to S09.
- `convpreserve selftest`: replays a bundled 400-record sample and 174 per-rule cases for either oracle.
- CI: `--format github`, a composite GitHub Action (`action.yml`), pre-commit hooks and a baseline of known findings (`--write-baseline`, `--baseline`).
- No runtime dependencies beyond Python 3.11+; no network access except `probe`; no LLM calls.
