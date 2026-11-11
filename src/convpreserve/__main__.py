"""Allow ``python -m convpreserve`` (same entry point as the ``convpreserve`` command)."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
