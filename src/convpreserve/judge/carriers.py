"""Carriers loader for Oracle v1.0.
Loads `carriers.json` as the single source of truth for all vendor wire field names,
sentinel values, and per-dialect carrier mappings. Zero vendor literals live in Python code.
"""
import json
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_CARRIERS_PATH = os.path.join(_HERE, "..", "data", "carriers.json")
_CACHE = None


def load_carriers():
    global _CACHE
    if _CACHE is None:
        with open(_CARRIERS_PATH, "r", encoding="utf-8") as f:
            _CACHE = json.load(f)
    return _CACHE


def wire_tables():
    return load_carriers()["wire_tables"]


def common():
    return wire_tables()["common"]


def resolve_dialect(dialect_name):
    aliases = wire_tables()["dialect_aliases"]
    if dialect_name in aliases:
        return aliases[dialect_name]
    raise ValueError(f"Unsupported dialect: {dialect_name!r}")


def table(dialect_name, phase):
    d = resolve_dialect(dialect_name)
    return wire_tables()[d][phase]


def first_present(mapping, keys, default=None):
    if not isinstance(mapping, dict):
        return default
    if isinstance(keys, str):
        return mapping.get(keys, default)
    for k in keys:
        if k in mapping and mapping[k] is not None:
            return mapping[k]
    for k in keys:
        if k in mapping:
            return mapping[k]
    return default


def has_any_key(mapping, keys):
    if not isinstance(mapping, dict):
        return False
    if isinstance(keys, str):
        return keys in mapping
    return any(k in mapping for k in keys)
