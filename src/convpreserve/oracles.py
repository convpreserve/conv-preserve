"""Oracle versions shipped in ConvPreserve.

- v1.0: Frozen baseline oracle used for the paper's request evaluation (`convpreserve.judge`).
- v1.3: Default oracle (`convpreserve.judge_v13`); assembles streamed OpenAI Chat reasoning deltas
        and attaches RFC 6901 JSON Pointer evidence locations to findings.
"""
import importlib
import os

ORACLES = {"v1.0": "convpreserve.judge", "v1.3": "convpreserve.judge_v13"}
DEFAULT_ORACLE = "v1.3"


def default_oracle():
    v = os.environ.get("CONVPRESERVE_ORACLE", DEFAULT_ORACLE)
    if v not in ORACLES:
        raise ValueError(f"CONVPRESERVE_ORACLE={v!r} is not one of {sorted(ORACLES)}")
    return v


def get_judge(version):
    """Return the judge module exposing `compare` and `Finding` for `version`."""
    if version not in ORACLES:
        raise ValueError(f"unknown oracle {version!r}; choose from {sorted(ORACLES)}")
    return importlib.import_module(ORACLES[version] + ".judge")
