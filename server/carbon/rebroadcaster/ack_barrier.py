"""Bounded, destination-local buffering behind a CommUDP acknowledgement.

This primitive does not identify a platform or decide when to open a barrier.
Callers must only supply acknowledged, authenticated endpoint traffic and must
cancel the barrier when the associated endpoint is retired or reconnected.
If an aggregate contains both the predecessor and a later sequence, the whole
datagram is held; callers must retain a separately retransmittable predecessor.
Retry deadlines for held publications must begin when they are actually sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Generic, TypeVar

from carbon.rebroadcaster.confirmations import ConfirmationManager
from carbon.rebroadcaster.state import Address

T = TypeVar("T")
_MASK = 0x0FFFFFFF


@dataclass
class _Waiting(Generic[T]):
    sequence: int
    records: list[tuple[bytes, T]] = field(default_factory=list)
    size: int = 0
    failed: bool = False


class AckBarrier(Generic[T]):
    """Hold later records until the preceding reliable sequence is ACKed.

    Identical wire retransmissions are stored once.  On overflow the barrier
    fails closed: partial buffered data is never released as a valid stream.
    """

    def __init__(self, *, max_records: int = 64, max_bytes: int = 131072) -> None:
        if max_records < 1 or max_bytes < 1:
            raise ValueError("barrier limits must be positive")
        self.max_records = max_records
        self.max_bytes = max_bytes
        self._waiting: dict[Address, _Waiting[T]] = {}
        self._lock = RLock()

    def open(self, address: Address, sequence: int) -> bool:
        """Start one barrier; never replace an outstanding endpoint barrier."""
        with self._lock:
            if address in self._waiting:
                return False
            self._waiting[address] = _Waiting(int(sequence) & _MASK)
            return True

    def pending(self, address: Address) -> bool:
        with self._lock:
            return address in self._waiting

    def failed(self, address: Address) -> bool:
        with self._lock:
            waiting = self._waiting.get(address)
            return bool(waiting and waiting.failed)

    def defer(self, address: Address, sequences: tuple[int, ...], wire: bytes, item: T,
              *, newly_built: bool = False) -> bool:
        """Return True if this datagram must be held or discarded on overflow."""
        with self._lock:
            waiting = self._waiting.get(address)
            if waiting is None or (not newly_built and not any(
                ConfirmationManager.sequence_newer(sequence, waiting.sequence)
                for sequence in sequences
            )):
                return False
            if waiting.failed:
                return True
            payload = bytes(wire)
            if any(previous == payload for previous, _item in waiting.records):
                return True
            if len(waiting.records) >= self.max_records or waiting.size + len(payload) > self.max_bytes:
                waiting.records.clear()
                waiting.size = 0
                waiting.failed = True
                return True
            waiting.records.append((payload, item))
            waiting.size += len(payload)
            return True

    def acknowledge(self, address: Address, acknowledgement: int) -> tuple[T, ...] | None:
        """Release in order on a cumulative ACK; None means still waiting."""
        with self._lock:
            waiting = self._waiting.get(address)
            if (waiting is None or waiting.failed or not
                    ConfirmationManager.sequence_acked(acknowledgement, waiting.sequence)):
                return None
            self._waiting.pop(address)
            return tuple(item for _wire, item in waiting.records)

    def cancel(self, address: Address) -> None:
        with self._lock:
            self._waiting.pop(address, None)
