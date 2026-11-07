"""Select the existing per-platform session conventions.

These policies describe the current implementation, including experimental
Xbox ordering rules; they are not proof of retail Xbox behavior. Unknown
platforms deliberately keep the historical PC-compatible behavior.
"""

from __future__ import annotations

from carbon.accounts.identity import ClientPlatform
from carbon.platforms.contracts import SessionPolicy
from carbon.platforms.pc import POLICY as PC_POLICY
from carbon.platforms.xbox360.session import POLICY as XBOX_POLICY


def policy_for(platform: ClientPlatform) -> SessionPolicy:
    return XBOX_POLICY if platform is ClientPlatform.XBOX360 else PC_POLICY
