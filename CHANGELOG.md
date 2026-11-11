# Changelog

## 0.1.0

Initial public release.

- **Core CLI (`check`, `batch`)**: Deterministic semantic preservation checking across OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Google Gemini `generateContent` on requests, responses, and SSE streams.
- **Dual Oracles (`v1.3` default, `v1.0` frozen)**: Ships both the frozen baseline judge from the paper (`--oracle v1.0`) and `v1.3` with streamed reasoning delta assembly and RFC 6901 JSON Pointer evidence paths (`src/convpreserve/judge_v13/CHANGES_FROM_V100.md`).
- **Live Gateway Probing (`probe`)**: Built-in loopback upstream sink and 51 contract-valid Anthropic Messages request shapes for black-box gateway testing without provider API keys.
- **Rule Catalogue (`explain`)**: Definitions and curated root-cause notes across the 27 preservation rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`).
- **CI & Regression Gating**: GitHub step-summary formatter (`--format github`), composite GitHub Action (`action.yml`), `pre-commit` hooks, and fingerprint baselines (`--write-baseline`, `--baseline`).
