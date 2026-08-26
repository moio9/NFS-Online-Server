"""Server-owned retry state for reliable Carbon publications.

The names in this module are implementation abstractions, not recovered EA
symbols.  Protocol-specific code still decides what constitutes confirmation
and whether a retry must replay exact bytes or rebuild an outer datagram.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """Bound exponential backoff by both attempts and wall-clock time."""

    initial_delay_seconds: float
    maximum_delay_seconds: float
    maximum_retries: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.initial_delay_seconds <= 0:
            raise ValueError("retry initial delay must be positive")
        if self.maximum_delay_seconds < self.initial_delay_seconds:
            raise ValueError("retry maximum delay must cover the initial delay")
        if self.maximum_retries < 1:
            raise ValueError("retry maximum must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("retry timeout must be positive")

    def begin(self, now: float) -> "RetrySchedule":
        opened_at = float(now)
        return RetrySchedule(
            policy=self,
            opened_at=opened_at,
            retry_not_before=opened_at + self.initial_delay_seconds,
            deadline=opened_at + self.timeout_seconds,
        )


@dataclass
class RetrySchedule:
    """Mutable timing state shared by differently encoded publications."""

    policy: RetryPolicy
    opened_at: float
    retry_not_before: float
    deadline: float
    retries_sent: int = 0

    def due(self, now: float) -> bool:
        current = float(now)
        return not self.exhausted(current) and current >= self.retry_not_before

    def exhausted(self, now: float) -> bool:
        return (
            self.retries_sent >= self.policy.maximum_retries
            or float(now) >= self.deadline
        )

    def exhaustion_reason(self, now: float) -> str | None:
        if float(now) >= self.deadline:
            return "deadline"
        if self.retries_sent >= self.policy.maximum_retries:
            return "attempt-limit"
        return None

    def defer_from(self, now: float) -> None:
        """Restart only the current wait without extending the deadline."""
        self.retry_not_before = min(
            self.deadline,
            float(now) + self.policy.initial_delay_seconds,
        )

    def record_retry(self, now: float) -> float:
        if self.exhausted(now):
            raise RuntimeError("cannot record an exhausted retry schedule")
        self.retries_sent += 1
        delay = min(
            self.policy.maximum_delay_seconds,
            self.policy.initial_delay_seconds * (2 ** self.retries_sent),
        )
        self.retry_not_before = min(self.deadline, float(now) + delay)
        return delay


@dataclass
class ReliableWindow:
    """Exact CommUDP records retained until their phase-level confirmation."""

    records: tuple[bytes, ...]
    base_sequence: int
    final_sequence: int
    retry: RetrySchedule
    transport_acknowledged: bool = False

    def __post_init__(self) -> None:
        self.records = tuple(bytes(record) for record in self.records)
        if not self.records:
            raise ValueError("reliable window requires at least one record")
