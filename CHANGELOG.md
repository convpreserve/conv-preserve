# Changelog

## 0.1.0

Initial public release.

- **Payload Comparison (`check`, `batch`)**: Detect silent semantic loss across OpenAI Chat Completions, OpenAI Responses, Anthropic Messages, and Google Gemini (`generateContent`) for requests, responses, and SSE streams.
- **Live Gateway Testing (`probe`)**: Test any local gateway end-to-end with a built-in mock upstream server and 51 Anthropic request cases—no API keys required.
- **Rule Reference (`explain`)**: Inspect all 27 preservation rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`) and known bug patterns across open-source translators.
- **CI Integration**: GitHub Actions step-summary output (`--format github`), composite `action.yml`, `pre-commit` hooks, and baseline regression filtering (`--write-baseline`, `--baseline`).
