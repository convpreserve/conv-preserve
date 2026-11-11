# Vendored Files

| Original Path | Vendored Path | Bytes | SHA-256 (Original) | SHA-256 (Vendored) |
| --- | --- | --- | --- | --- |
| `artifacts/g4/oracle_v100/judge/__init__.py` | `src/convpreserve/judge/__init__.py` | 246 | `962612386564b52db4298162c8cf99164a7b625164ad1ac5c70b43a08220fa76` | `962612386564b52db4298162c8cf99164a7b625164ad1ac5c70b43a08220fa76` |
| `artifacts/g4/oracle_v100/judge/carriers.py` | `src/convpreserve/judge/carriers.py` | 1564 | `51de7ac7f1a12fc82fac05b7fcdb452afe29c88ece231a4b418cdc19816be279` | `f6b8f9884e17a0145be5d714ae96db43b6fc4bcf0ef3738c0dcdb585d8c72148` |
| `artifacts/g4/oracle_v100/judge/ir.py` | `src/convpreserve/judge/ir.py` | 86606 | `af5746963d51432cddcb378f534e472406181f1779c688e74cb5d8d4d7774676` | `af5746963d51432cddcb378f534e472406181f1779c688e74cb5d8d4d7774676` |
| `artifacts/g4/oracle_v100/judge/judge.py` | `src/convpreserve/judge/judge.py` | 39893 | `913f9563b4a7d0879ad5fb6a8a2150da177ce558545b18402b641e741fbdc6e3` | `5a8d78e47b25367438780356cddf30f7f1cb7728590fe2a60bee87d35c8b4707` |
| `artifacts/g4/oracle_v100/judge/order.py` | `src/convpreserve/judge/order.py` | 12688 | `f6b67e3f212848c92ef746cce09f051ab93ad86a44bed494cb5d1215707239ad` | `f6b67e3f212848c92ef746cce09f051ab93ad86a44bed494cb5d1215707239ad` |
| `artifacts/g4/oracle_v100/judge/schema.py` | `src/convpreserve/judge/schema.py` | 8128 | `a3cc01015cffb7add4def60cdef4f59ad532ebe7f3ee9f3bd315ff72be80d717` | `291ad23a02e528241aa21019cc0aa3de8f684e359ed803afa2573d7dbdf38041` |
| `artifacts/g1/m1/procheck/schemacanon.py` | `src/convpreserve/judge/schemacanon.py` | 9597 | `37b4152acf2b98f37f76269add9e0d6a38bd5b1e976adddbb55a4cf87d444765` | `37b4152acf2b98f37f76269add9e0d6a38bd5b1e976adddbb55a4cf87d444765` |
| `artifacts/g4/oracle_v100/carriers.json` | `src/convpreserve/data/carriers.json` | 171389 | `dcda0a647356ec1efd482e32221dfecf9822fe3ad97c046a4f1fbd571d0b4c7f` | `ed8317e655b04e13ea421a9b7f9cd6c6dde21a7113b68a5009fb597a2dd01948` |
| `artifacts/g4/oracle_v100/judge/catalog.json` | `src/convpreserve/data/catalog.json` | 5938 | `e30789aa6b11092939456c35191ea88f469dc105a6161c7456ac0612e175c936` | `e30789aa6b11092939456c35191ea88f469dc105a6161c7456ac0612e175c936` |

## Modifications

### `src/convpreserve/data/carriers.json`
The value of the provenance key `source_pack` (a local directory path) is replaced by `"redacted-for-review"`. No carrier entry changes.

### `src/convpreserve/judge/carriers.py`
```diff
--- artifacts/g4/oracle_v100/judge/carriers.py	2026-10-10 07:48:41.832638854 +0000
+++ src/convpreserve/judge/carriers.py	2026-10-14 20:16:31.099757348 +0000
@@ -6,7 +6,7 @@
 import os
 
 _HERE = os.path.dirname(os.path.abspath(__file__))
-_CARRIERS_PATH = os.path.join(_HERE, "..", "carriers.json")
+_CARRIERS_PATH = os.path.join(_HERE, "..", "data", "carriers.json")
 _CACHE = None
 
 
```

### `src/convpreserve/judge/judge.py`
```diff
--- artifacts/g4/oracle_v100/judge/judge.py	2026-10-10 09:50:05.986076066 +0000
+++ src/convpreserve/judge/judge.py	2026-10-14 20:16:31.100547960 +0000
@@ -17,7 +17,7 @@
 Finding = namedtuple("Finding", ["rule_id", "tier", "object_id", "detail"])
 
 _HERE = os.path.dirname(os.path.abspath(__file__))
-with open(os.path.join(_HERE, "catalog.json"), "r", encoding="utf-8") as _f:
+with open(os.path.join(_HERE, "..", "data", "catalog.json"), "r", encoding="utf-8") as _f:
     CATALOG = {r["id"]: r for r in json.load(_f)["rules"]}
 
 
```

### `src/convpreserve/judge/schema.py`
```diff
--- artifacts/g4/oracle_v100/judge/schema.py	2026-10-10 09:35:58.346574132 +0000
+++ src/convpreserve/judge/schema.py	2026-10-14 20:16:31.101410985 +0000
@@ -8,10 +8,7 @@
 from . import carriers as C
 
 _HERE = os.path.dirname(os.path.abspath(__file__))
-_PROCHECK_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "g1", "m1", "procheck"))
-if _PROCHECK_DIR not in sys.path:
-    sys.path.insert(0, _PROCHECK_DIR)
-import schemacanon as _sc  # read-only import authorized by PREREG_V100.md §1
+from . import schemacanon as _sc  # read-only import authorized by PREREG_V100.md §1
 
 
 def _norm_ws(s):
```

## Re-verification (2026-10-14, workshop agent)

For all 9 rows above the sha256 of the original and of the vendored file were recomputed from the working tree and equal the table values. 6 of 9 files are byte-identical to their originals (`__init__.py`, `ir.py`, `order.py`, `schemacanon.py`, `carriers.json`, `catalog.json`). 3 differ: `carriers.py`, `judge.py`, `schema.py`. `diff original vendored` for those three shows only the path/import lines in the diffs above (data-file location; `schemacanon` imported from the package instead of via `sys.path`) and nothing else.

## Derived (not vendored): `judge_v13`

`src/convpreserve/judge_v13/` is Oracle v1.3: a copy of the `judge/` files above with the three changes listed in `judge_v13/CHANGES_FROM_V100.md`, plus `evidence.py`. It is not a vendored original; `judge/` stays byte-identical to the table above (checked by `tests/test_vendoring.py`) and `judge_v13/SHA256SUMS` pins the derived files (checked by `tests/test_oracle_v13.py`). `data/carriers.json` and `data/catalog.json` are shared by both oracles.
