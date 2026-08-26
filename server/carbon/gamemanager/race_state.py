"""Explicit room-scoped lifecycle state for a Carbon race.

The old implementation used several independent booleans.  That allowed
impossible combinations (for example StartRaceSync sent while StartLoading was
not recorded).  This module keeps the monotonic race lifecycle separate from
the room's join-access lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class RaceStateError(RuntimeError):
    """Raised when code attempts an invalid lifecycle transition."""


class RacePhase(IntEnum):
    SESSION_SETUP = 0
    COUNTDOWN = 1
    COUNTDOWN_EXPIRED = 2
    START_LOCKED = 3
    LOADING = 4
    RACING = 5
    FINISHED = 6


class RoomAccess(IntEnum):
    OPEN = 0
    LOCKED = 1


@dataclass
class GameRaceState:
    attributes: bytes = b""
    # A dedicated Challenge room is tied to the event selected by its host.
    # The first host-published 0x1D15 is authoritative for that event.  Later
    # CommUDP history windows can contain a different local room snapshot;
    # retaining it would make an invited helper resolve another Challenge.
    challenge_event_identity: dict[str, str] = field(default_factory=dict)
    countdown_duration: float = 30.5
    start_delay_seconds: float = 2.0
    start_sync_ping: float = 0.0
    fallback_latency_to_host: float = 25.0
    countdown_deadline: float = 0.0
    latest_match_timer: bytes = b""
    # Timer id 5 lives in the clients' shared wire-clock domain. Keep that
    # deadline and generation separate from ``countdown_deadline``, which is a
    # server-local monotonic lifecycle deadline.
    countdown_wire_deadline: float = 0.0
    countdown_generation_id: int = 0
    countdown_initial_timer: bytes = b""
    countdown_latest_timer: bytes = b""
    latest_room_timer: bytes = b""
    latest_post_race_timer: bytes = b""
    room_wait_deadline: float = 0.0
    post_race_deadline: float = 0.0
    # Challenge invite commit is a three-way barrier: the post-join host
    # token, the host state-7 room context, and the helper allocation window.
    coop_barrier_host: tuple[str, int] | None = None
    coop_barrier_token: bytes = b""
    coop_host_state7_seen: bool = False
    pending_coop_host_state7: bytes = b""
    # The stock host's concrete Challenge attributes publish the actual room
    # size. Keep the latest accepted value here; it is frozen only while a
    # helper's allocation/room-commit window can replay stale local settings.
    challenge_capacity: int = 0
    # Each invited helper needs its own state-7 room commit. A single global
    # flag cannot distinguish the first helper from a later invite.
    coop_committed_helpers: set[tuple[str, int]] = field(default_factory=set)
    room_commit_sent: bool = False
    countdown_transition_sent: bool = False
    # Completed session objects remain cached while both players return to
    # the same room. Do not mistake those old objects for a fresh initial
    # join and seed another countdown immediately after the post-race reopen.
    post_race_reopened: bool = False
    # The coordinator publishes one OLMSG 0x05 for every locally controlled
    # AI racer immediately before the race begins. Retail registers each
    # ServerCarId once, republishes a sanitized body to the whole room, then
    # appends READY. Without this room-scoped registry the clients can receive
    # changing CarState bodies for an AI car that was never attached.
    player_controlled_ai: dict[int, bytes] = field(default_factory=dict)
    # Client RacerFinished (0x0E) is a room notification, not result
    # authority. Keep its car ids only to suppress reliable retransmits while
    # preserving the first native publication to both endpoints.
    relayed_racer_finished: set[int] = field(default_factory=set)
    # StartLoading is a two-stage application barrier. The host opens it and
    # each live client echoes its own loading signal. Some stock clients never
    # publish the later READY (0x0F), so retain both the opening time and the
    # participants that demonstrably entered loading for a bounded fallback.
    loading_started_at: float = 0.0
    loading_player_ids: set[int] = field(default_factory=set)
    phase: RacePhase = RacePhase.SESSION_SETUP
    room_access: RoomAccess = RoomAccess.OPEN

    def begin_countdown(self, now: float) -> bool:
        if self.phase != RacePhase.SESSION_SETUP:
            return False
        if not 0.0 < float(self.countdown_duration) <= 300.0:
            raise RaceStateError(
                f"countdown duration out of range: {self.countdown_duration}"
            )
        self.countdown_deadline = float(now) + float(self.countdown_duration)
        self.phase = RacePhase.COUNTDOWN
        return True

    def lock_room_access(self) -> bool:
        if self.room_access == RoomAccess.LOCKED:
            return False
        self.room_access = RoomAccess.LOCKED
        return True

    def mark_countdown_expired(self) -> bool:
        return self._advance_exact(
            expected=RacePhase.COUNTDOWN,
            target=RacePhase.COUNTDOWN_EXPIRED,
        )

    def mark_start_locked(self) -> bool:
        return self._advance_exact(
            expected=RacePhase.COUNTDOWN_EXPIRED,
            target=RacePhase.START_LOCKED,
        )

    def mark_loading(
        self,
        *,
        now: float | None = None,
        source_player_id: int | None = None,
    ) -> bool:
        if self.phase >= RacePhase.LOADING:
            return False
        if self.phase not in (RacePhase.COUNTDOWN_EXPIRED, RacePhase.START_LOCKED):
            raise RaceStateError(
                f"invalid race transition: {self.phase.name} -> LOADING; "
                "expected COUNTDOWN_EXPIRED or START_LOCKED"
            )
        self.phase = RacePhase.LOADING
        self.loading_started_at = 0.0 if now is None else float(now)
        self.loading_player_ids.clear()
        if source_player_id is not None:
            self.loading_player_ids.add(int(source_player_id))
        return True

    def observe_loading_player(self, player_id: int) -> bool:
        if self.phase != RacePhase.LOADING:
            return False
        previous = len(self.loading_player_ids)
        self.loading_player_ids.add(int(player_id))
        return len(self.loading_player_ids) != previous

    def mark_racing(self) -> bool:
        return self._advance_exact(
            expected=RacePhase.LOADING,
            target=RacePhase.RACING,
        )

    def mark_finished(self) -> bool:
        return self._advance_exact(
            expected=RacePhase.RACING,
            target=RacePhase.FINISHED,
        )

    def _advance_exact(self, *, expected: RacePhase, target: RacePhase) -> bool:
        if self.phase >= target:
            return False
        if self.phase != expected:
            raise RaceStateError(
                f"invalid race transition: {self.phase.name} -> {target.name}; "
                f"expected {expected.name}"
            )
        self.phase = target
        return True
