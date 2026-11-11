# Bundled Self-Test Reference Manifest

`convpreserve selftest` verifies local installations against three deterministic reference archives bundled in `src/convpreserve/data/`:

1. **`selftest_sample.json.gz`**
   - **SHA-256**: `27460b7d824b7839d07e5b916ed224742719d5f0024cd5174877181058405809`
   - **Contents**: Stratified sample of 400 translated payload pairs spanning `request`, `response`, and `stream` across OpenAI Chat, OpenAI Responses, Anthropic Messages, and Gemini `generateContent`, paired with frozen Oracle `v1.0` verdicts.

2. **`selftest_expected_v13.json.gz`**
   - **SHA-256**: `3c0c8f91b15485af8f30ff3492ddae8c2c0b7d482c29b7e8cc60e4d0b0028701`
   - **Contents**: Expected Oracle `v1.3` verdicts and RFC 6901 JSON Pointer `evidence` objects for the same 400-pair sample (`python tests/gen_selftest_expected_v13.py --check`).

3. **`selftest_rule_cases.json.gz`**
   - **SHA-256**: `b1d206b5768c5fa774b9932422c11730493d8e5677870a447701ae5c3c3d2237`
   - **Contents**: 174 minimal positive and negative test pairs covering all 27 preservation rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`) (`python tests/gen_rule_cases.py --check`).
