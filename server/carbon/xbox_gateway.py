"""Backward-compatible entry point for the experimental Xbox login bridge.

The implementation lives in :mod:`carbon.platforms.xbox360.live_gateway`.
It is loopback-only and does not authenticate or verify Xbox Live ownership.
"""

import sys
from carbon.platforms.xbox360 import live_gateway as _implementation

if __name__ == "__main__":
    raise SystemExit(_implementation.main())

sys.modules[__name__] = _implementation
