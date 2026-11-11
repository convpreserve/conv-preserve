# Bundled Self-Test Archives

`convpreserve selftest` verifies local installations against three reference archives in `src/convpreserve/data/`:

- **`selftest_sample.json.gz`** (`27460b7d824b7839d07e5b916ed224742719d5f0024cd5174877181058405809`): 400 translated payload pairs across requests, responses, and SSE streams with pinned `v1.0` verdicts.
- **`selftest_expected_v13.json.gz`** (`3c0c8f91b15485af8f30ff3492ddae8c2c0b7d482c29b7e8cc60e4d0b0028701`): Expected `v1.3` verdicts and JSON Pointer `evidence` locations for the 400-pair sample.
- **`selftest_rule_cases.json.gz`** (`b1d206b5768c5fa774b9932422c11730493d8e5677870a447701ae5c3c3d2237`): 174 minimal positive and negative unit cases covering all 27 rules (`S01`–`S13`, `D01`–`D06`, `R01`–`R08`).
