# Oracle v1.3: changes from the frozen Oracle v1.0

`convpreserve.judge_v13` is a copy of `convpreserve.judge` (Oracle v1.0, commit 9024873, byte-identical to `artifacts/g4/oracle_v100/judge`, hashes in `../../../VENDORED.md`) with three changes. `convpreserve.judge` is not modified; use `--oracle v1.0` (or `CONVPRESERVE_ORACLE=v1.0`) to reproduce the paper. `SHA256SUMS` pins the files of this directory and `tests/test_oracle_v13.py` checks it.

| # | Change | Files | Effect on verdicts |
|---|---|---|---|
| 1 | `_parse_openai_chat_stream` assembles `delta.reasoning_content` and `delta.reasoning` into the message it builds (same carriers and the same comparison, whitespace collapsed, as the non-stream path) | `ir.py` | Removes the `S05` "lost / invented assistant thinking" findings that v1.0 reports when an OpenAI Chat stream carries reasoning correctly. Where the thinking now exists on both sides, rules that need it can fire on streams as they already do on responses (for example `S03` signature carrier lost). |
| 2 | Every finding carries `evidence`: `{"side": "src"\|"dst", "path": "<JSON Pointer>", "precision": "exact"\|"container"\|"payload"}` | `judge.py`, new `evidence.py` | None. The locator runs after the verdicts and only reads the payloads. v1.3 verdicts equal v1.0 verdicts on all 400 selftest records. |
| 3 | The `S10` detail text of the P4-ablation path no longer contains the word that v1.0 uses ("carries known <word> sentinel" becomes "carries known sentinel value"); the sentinel table is looked up by key pattern so no Python source contains the word either. Three comments in `order.py` and one docstring are reworded | `judge.py`, `order.py` | None (text only). The default `S10` text is the same in both oracles. |

`carriers.py` differs from v1.0 in its docstring only; `schema.py` and `schemacanon.py` are identical. `data/carriers.json` and `data/catalog.json` are shared by both oracles.

## Evidence pointers

- Request and response payloads are addressed as parsed JSON. Stream payloads are addressed as the list of their `data:` frames, 0-based, in order, `[DONE]` included (`/3/delta` = the `delta` object of the fourth frame). Pre-parsed event lists (`.json` stream input) are addressed as given.
- `exact`: the pointer resolves to the offending value (a control, a key, a text, a tool declaration, a frame). `container`: it resolves to the object that holds it (a message, an array, the payload root for an absent key, then `missing_key` names the key). `payload`: nothing narrower was found; the pointer is the empty string.
- `evidence.resolve_pointer(doc, pointer)` resolves a pointer; `evidence._doc(body, side)` builds the document.
- Precision on the 400-record selftest sample (323 findings): 240 exact, 27 container, 56 payload. The payload cases are mostly findings about content that was dropped in stream reassembly or about usage that is absent. Per rule: `tests/report_v13_vs_v10.py` (output `bench/out/v13_vs_v10_*.json`).

## Checked on committed inputs (`tests/report_v13_vs_v10.py`)

- 400-record sample: no record has different verdicts; every evidence pointer resolves.
- Census pins of Gate A (request side): cell-level identical, sent 714 / refused 29 / judged 685 / T1 cells 489.
- `reverse_response_census` captures (1,115 judged response and stream cells): 41 stream cells change, all by change 1. 11 go from T1 to no finding, 30 stay T1 (25 of them gain an `S03` finding, as the same cells already have on the response side in v1.0). Response cells are unchanged.

## Not changed

Refusal deltas and `reasoning_details` in streams are not assembled. `redacted_thinking` has no OpenAI Chat carrier; its `S05` is kept.
