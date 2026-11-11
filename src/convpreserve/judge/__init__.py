"""Oracle v1.0 judge package (PREREG_V100.md)."""
from .judge import compare, Finding
from .ir import parse_ir
from .carriers import load_carriers, resolve_dialect

__all__ = ["compare", "Finding", "parse_ir", "load_carriers", "resolve_dialect"]
