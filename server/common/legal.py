"""Canonical legal content shared by every supported game."""

from __future__ import annotations


# The Carbon client expects this FESL version token.  It is a wire-compatibility
# identifier, not a hash of the text below.
TERMS_OF_SERVICE_VERSION = "20426_17.20426_17"
TERMS_OF_SERVICE_TEXT = (
    "NFS Online community server terms of use:\n\n"
    "By using this server, you agree to follow its rules. "
    "This unofficial service is not affiliated with or endorsed by Electronic Arts."
)


__all__ = ["TERMS_OF_SERVICE_TEXT", "TERMS_OF_SERVICE_VERSION"]
