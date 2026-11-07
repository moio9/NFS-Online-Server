"""Experimental Xbox clear-mode ingress selection for loopback tests only.

The shared rebroadcaster owns endpoint state, activity and rejection counts;
this module owns only clear-mode framing and eligibility. It is not an Xbox
Live security layer and does not enable remote clear-mode traffic.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from enum import Enum, auto

from carbon.platforms.xbox360.prototunnel import decode_clear_datagram
from carbon.transport.commudp import CommUDPControl, CommUDPType
from carbon.transport.prototunnel import ProtoTunnelError, TunnelDatagram

Address = tuple[str, int]


class ClearIngress(Enum):
    ENCRYPTED = auto()
    ACCEPTED = auto()
    REJECTED_AUTHORIZATION = auto()
    REJECTED_SELECTED = auto()


def classify_clear_ingress(
    payload: bytes,
    addr: Address,
    *,
    enabled: bool,
    previous_exists: bool,
    clear_selected: bool,
    control_packets: Callable[[TunnelDatagram], Iterable[CommUDPControl]],
    authorizer: Callable[[Address], bool] | None,
) -> tuple[ClearIngress, TunnelDatagram | None]:
    if enabled and addr[0] == "127.0.0.1":
        try:
            clear = decode_clear_datagram(payload)
        except ProtoTunnelError:
            clear = None
        first_connect = bool(
            clear is not None
            and [(p.channel, len(p.payload)) for p in clear.packets] == [(7, 6), (1, 8)]
            and any(c.kind is CommUDPType.CONNECT for c in control_packets(clear))
        )
        if clear is not None and (clear_selected or (not previous_exists and first_connect)):
            if authorizer is not None and not authorizer(addr):
                return ClearIngress.REJECTED_AUTHORIZATION, None
            return ClearIngress.ACCEPTED, clear
    if clear_selected:
        return ClearIngress.REJECTED_SELECTED, None
    return ClearIngress.ENCRYPTED, None
