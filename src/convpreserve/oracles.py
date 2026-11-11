"""Oracle versions shipped in the package.

v1.0  the frozen Oracle v1.0 judge (`convpreserve.judge`, byte-identical to artifacts/g4/oracle_v100; kept for reproducing the paper).
v1.3  Oracle v1.0 plus three changes (`convpreserve.judge_v13`, see judge_v13/CHANGES_FROM_V100.md and CHANGELOG.md):
      streamed OpenAI Chat reasoning is assembled, findings carry an evidence path, the S10 detail text is reworded.
The default is v1.3; set CONVPRESERVE_ORACLE=v1.0 (or pass --oracle v1.0) for the frozen behaviour.
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
    """The `judge` module (with `compare` and `Finding`) of oracle `version`."""
    if version not in ORACLES:
        raise ValueError(f"unknown oracle {version!r}; choose from {sorted(ORACLES)}")
    return importlib.import_module(ORACLES[version] + ".judge")
