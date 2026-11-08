"""Clock on the MW Xbox lobby wire, independent of the server timezone."""
from datetime import datetime, timezone

from classic.protocols.frame import ClassicEAFrame


def xbox_lobby_heartbeat(now: datetime | None = None) -> bytes:
    # The original MW LAN DLL sends UTC in REF. Xbox clients use this as a
    # clock reference for host countdowns, so local time introduces a timezone
    # offset between peers that have received a heartbeat and those that have not.
    now = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    reference = f"{now.year}.{now.month}.{now.day}-{now:%H:%M:%S}"
    return ClassicEAFrame.from_fields("~png", (("REF", reference),),
                                      final_separator=False).encode()
