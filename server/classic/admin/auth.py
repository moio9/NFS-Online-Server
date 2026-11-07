"""Compatibility entry point for shared account administration."""

import sys
from common.admin import auth as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())
sys.modules[__name__] = _implementation
