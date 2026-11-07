"""Xbox Live-related account compatibility, not Xbox Live authentication.

The password login preference only selects wire conventions for an account
that the shared credential service has already authenticated. It establishes
neither a genuine Xbox Live session nor ownership of an XUID.
"""

from __future__ import annotations

from collections.abc import Mapping

from carbon.accounts.identity import ClientPlatform


def unverified_xuid_lookup_fields() -> dict[str, str]:
    """Preserve the existing empty lookup until ownership is verified."""
    return {"TXN": "GetUserIdsByXuids", "userIds.[]": "0"}


def authenticated_platform_preference(fields: Mapping[str, str]) -> ClientPlatform:
    if fields.get("clientPlatform", "").strip().casefold() == "xbox360":
        return ClientPlatform.XBOX360
    return ClientPlatform.UNKNOWN


def login_platform_fields(platform: ClientPlatform) -> dict[str, str]:
    return {"clientPlatform": ClientPlatform.XBOX360.value} if platform is ClientPlatform.XBOX360 else {}
