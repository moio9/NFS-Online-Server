"""Wire constants shared by the Classic lobby handlers."""

from __future__ import annotations

U2_ACTIVE_SYSFLAG = 1 << 19
U2_PASSWORD_SYSFLAG = 1 << 16
U2_READY_FLAG = 1 << 27
U2_PARTITION_COUNT = 1
U2_PARTITION_INDEX = 0
U2_POSTRACE_UDP_GRACE_SECONDS = 20.0
MW_GJOI_UNAVAILABLE_RESERVED = 0x7567616D  # "ugam"
U2_ROOMS: tuple[tuple[int, str], ...] = tuple(
    (index + 1, f"{letter}.LAN")
    for index, letter in enumerate("ABCDEFGH")
)

__all__ = [
    "MW_GJOI_UNAVAILABLE_RESERVED",
    "U2_ACTIVE_SYSFLAG",
    "U2_PARTITION_COUNT",
    "U2_PARTITION_INDEX",
    "U2_PASSWORD_SYSFLAG",
    "U2_POSTRACE_UDP_GRACE_SECONDS",
    "U2_READY_FLAG",
    "U2_ROOMS",
]
