# Oracle v1.3 Release Notes (vs. Frozen Oracle v1.0)

`convpreserve.judge_v13` extends the frozen paper baseline (`convpreserve.judge`, selectable via `--oracle v1.0` or `CONVPRESERVE_ORACLE=v1.0`) with two functional enhancements while preserving identical verdicts across all 400 bundled self-test records:

| # | Enhancement | Module | Impact on verdicts |
|---|---|---|---|
| 1 | Assembles streamed `delta.reasoning_content` and `delta.reasoning` chunks in OpenAI Chat SSE streams on par with non-streaming responses | `ir.py` | Prevents false `S05` findings when an OpenAI Chat stream carries reasoning deltas, and enables downstream reasoning checks (e.g., `S03` signature preservation) on streams. |
| 2 | Attaches RFC 6901 JSON Pointer locations (`{"side": "src"\|"dst", "path": "...", "precision": "exact"\|"container"\|"payload"}`) to every finding | `judge.py`, `evidence.py` | None. Evidence localization runs post-verdict and never alters rule outcomes. |
| 3 | Normalizes `S10` ablation detail wording (`"carries known sentinel value"`) | `judge.py` | Diagnostic text only; rule logic and tiers are unchanged. |

## Evidence Pointer Semantics

- **Requests & responses** are indexed as parsed JSON documents (RFC 6901).
- **SSE streams** are indexed as 0-based arrays of `data:` frames in wire order, including `[DONE]` (e.g., `/3/delta` addresses the `delta` object of the fourth frame).
- **Precision levels**:
  - `exact`: pointer resolves directly to the offending field or value;
  - `container`: pointer resolves to the enclosing object or array (with `missing_key` naming an omitted key);
  - `payload`: finding spans the whole message or stream reassembly (`path: ""`).
