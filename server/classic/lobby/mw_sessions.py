"""Most Wanted game-session projection and ready/post-race state.

This mixin owns the MW-specific bridge between userset membership, lobby game
objects, race-start frames, ready-state AUX data and the post-race room view.
The public ``ClassicPreloginService`` API remains unchanged.
"""

from __future__ import annotations

import logging
import struct
from threading import Condition
import time
from typing import TYPE_CHECKING

from classic.ea.directory import GameSession
from classic.lobby.constants import U2_ACTIVE_SYSFLAG
from classic.lobby.models import (
    _MWAuxiliaryState,
    _MWReadyState,
    ClassicPreloginContext,
)

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


log = logging.getLogger("classic.protocols.prelogin")


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


class ClassicMWSessionMixin:
    """MW lobby-game projection, ready state and post-race room bridge."""

    def _init_mw_sessions(self) -> None:
        """Initialize state owned by the MW session lifecycle."""
        self._mw_control_messages: dict[
            tuple[int, int, str], tuple[str, float]
        ] = {}
        self._mw_ready_states: dict[int, _MWReadyState] = {}
        self._mw_session_seeds: dict[int, int] = {}
        self._mw_postrace_room_view_games: set[int] = set()
        # Stock clients can issue two GJOI transactions in the same scheduler
        # window, but their local CommUDP graph is not safe to expand twice at
        # once.  Admit one new guest per game until that guest publishes a
        # non-negative LT value, which is the client-owned proof that its host
        # edge has actually settled.
        self._mw_join_serial_condition = Condition(self._connections_lock)
        self._mw_join_serial_unstable: dict[int, set[int]] = {}
        self._mw_join_serial_expires_at: dict[int, float] = {}
        # Replacement games compact participant_order after each GJOI, while
        # retail allows every guest inherited from the completed race to join
        # before any one of them settles LT.  Preserve that expected-return set
        # independently until each guest has entered the replacement game.
        self._mw_postrace_handoff_returners: dict[int, set[int]] = {}

    def _mw_reserve_join_serial_slot(
        self,
        game_id: int,
        user_id: int,
        *,
        timeout: float = 2.0,
        lease_seconds: float = 1.0,
    ) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._mw_join_serial_condition:
            while self._mw_join_serial_unstable.get(int(game_id)):
                now = time.monotonic()
                expires_at = self._mw_join_serial_expires_at.get(
                    int(game_id),
                    0.0,
                )
                if expires_at and now >= expires_at:
                    expired = self._mw_join_serial_unstable.pop(
                        int(game_id),
                        set(),
                    )
                    self._mw_join_serial_expires_at.pop(int(game_id), None)
                    self._mw_join_serial_condition.notify_all()
                    log.info(
                        "MW serialized gjoi lease expired: game=%d "
                        "users=%s",
                        game_id,
                        ",".join(str(value) for value in sorted(expired)),
                    )
                    continue
                remaining = deadline - now
                if remaining <= 0:
                    return False
                if expires_at:
                    remaining = min(remaining, max(0.0, expires_at - now))
                self._mw_join_serial_condition.wait(remaining)
            self._mw_join_serial_unstable.setdefault(
                int(game_id),
                set(),
            ).add(int(user_id))
            self._mw_join_serial_expires_at[int(game_id)] = (
                time.monotonic() + max(0.0, float(lease_seconds))
            )
            return True

    def _mw_release_join_serial_slot(
        self,
        game_id: int,
        user_id: int = 0,
    ) -> bool:
        released = False
        with self._mw_join_serial_condition:
            pending = self._mw_join_serial_unstable.get(int(game_id))
            if pending is None:
                return False
            if int(user_id):
                if int(user_id) in pending:
                    pending.discard(int(user_id))
                    released = True
            else:
                released = bool(pending)
                pending.clear()
            if not pending:
                self._mw_join_serial_unstable.pop(int(game_id), None)
                self._mw_join_serial_expires_at.pop(int(game_id), None)
            if released:
                self._mw_join_serial_condition.notify_all()
        return released

    def notify_mw_transport_settled(self, game_id: int, user_id: int) -> bool:
        """Release a join slot after the relay delivered MW command 2."""

        released = self._mw_release_join_serial_slot(game_id, user_id)
        if released:
            log.info(
                "MW serialized gjoi released after UDP command 2: "
                "game=%d user=%d",
                game_id,
                user_id,
            )
        return released

    @staticmethod
    def _mw_auxiliary_link_settled(text: str) -> bool:
        latency = _MWAuxiliaryState.parse(text).get("LT")
        try:
            return latency is not None and int(latency) >= 0
        except (TypeError, ValueError):
            return False

    def _mw_game_fields(
        self,
        game: GameSession,
        *,
        active: bool = False,
        viewer_id: int = 0,
        include_relay: bool = True,
    ) -> tuple[tuple[str, object], ...]:
        # The userset and game identifiers only happen to be equal for the
        # first room.  Stock MW keeps the userset across races but allocates a
        # new game id (for example userset 1 -> game 2), and every G/IDENT
        # field in game presence uses that new game id.
        display_id = game.game_id
        when = time.localtime(game.created_at)
        created = (
            f"{when.tm_year}.{when.tm_mon}.{when.tm_mday} "
            f"{when.tm_hour:02d}:{when.tm_min:02d}:{when.tm_sec:02d}"
        )
        # MW assigns stable roles to the participant slots: the room owner is
        # slot 0 and guests retain their join slots for every viewer, before
        # and during race start.  In particular, adding a third player must
        # not move the first guest from OPPO1 to OPPO2.  Global lobby wire IDs
        # may have been allocated in a different login order, so sorting by
        # them corrupts the already-established transport record.
        local_id = int(viewer_id or game.owner_id)
        participants = list(game.ordered_participants())
        game.participant_wire_ids = {
            int(user_id): self._mw_wire_user_id(user_id)
            for user_id in participants
        }
        if (
            include_relay
            and len(participants) > 1
            and self.race_registrar is not None
        ):
            game.participant_race_addresses = self.race_registrar(game)
        fields: list[tuple[str, object]] = [
            ("IDENT", display_id),
            ("WHEN", created),
            ("NAME", game.name or game.host_persona),
            ("HOST", game.host_persona),
            ("ROOM", game.room_id),
            ("MAXSIZE", game.capacity),
            ("MINSIZE", game.min_players),
            ("COUNT", len(participants)),
            ("PRIV", 0),
            ("CUSTFLAGS", 0),
            ("SYSFLAGS", U2_ACTIVE_SYSFLAG if active else 0),
            ("EVID", 0),
            ("EVGID", 0),
            ("NUMPART", 1),
        ]
        for index, user_id in enumerate(participants):
            persona = game.participant_personas.get(
                user_id,
                f"Player{self._mw_wire_user_id(user_id)}",
            )
            address = (
                game.participant_race_addresses.get(
                    user_id,
                    game.participant_addresses.get(user_id, "127.0.0.1"),
                )
                if include_relay
                else game.participant_addresses.get(user_id, "127.0.0.1")
            )
            fields.extend(
                (
                    (f"OPPO{index}", persona),
                    (f"OPPART{index}", 0),
                    (f"OPFLAG{index}", 0),
                    (f"PRES{index}", 0),
                    (f"OPID{index}", self._mw_wire_user_id(user_id)),
                    (f"ADDR{index}", address),
                    (f"LADDR{index}", address),
                    (f"MADDR{index}", ""),
                )
            )
        fields.extend(
            (
                ("PARTSIZE0", game.capacity),
                ("PARAMS", ""),
                ("PARTPARAMS0", ""),
            )
        )
        for index, _user_id in enumerate(participants):
            fields.append((f"OPPARAM{index}", ""))
        # Relay discovery is an ASI extension, not part of the retail MW game
        # object. Keep it after the complete stock field sequence. Putting it
        # between NUMPART and OPPO0 makes the incremental +mgm parser encounter
        # custom fields before the later OPPO records when a third player joins.
        if (
            include_relay
            and not active
            and len(participants) > 1
            and self.race_endpoints
        ):
            endpoint = self._race_endpoint_for_participant(game, local_id)
            if endpoint is not None:
                fields.extend(
                    (
                        ("RLYHOST", endpoint.host),
                        ("RLYPORT", endpoint.port),
                    )
                )
        return tuple(fields)

    def _mw_context_for_callback(
        self,
        fields: dict[str, str],
    ) -> ClassicPreloginContext | None:
        """Resolve the stock ASI's shared uppercase callback to a main user."""
        try:
            wire_id = int(fields.get("CALLUSER", "0") or 0)
        except (TypeError, ValueError):
            wire_id = 0
        name = str(fields.get("NAME", "") or "").strip().casefold()
        with self._connections_lock:
            connections = tuple(self._connections.items())
        if wire_id:
            for user_id, wire_user_id in tuple(self._wire_user_ids.items()):
                if wire_user_id == wire_id:
                    return dict(connections).get(user_id)
        if name:
            return next(
                (
                    candidate
                    for _user_id, candidate in connections
                    if candidate.auth.persona.casefold() == name
                ),
                None,
            )
        return None

    @staticmethod
    def _mw_callback_ack(packet: ClassicEAFrame) -> bytes:
        """Stock callback replies use the request token as their command word."""
        ClassicEAFrame = _ea_frame()
        if packet.reserved:
            return (
                struct.pack(">I", packet.reserved)
                + b"\x00\x00\x00\x00"
                + struct.pack(">I", 13)
                + b"\x00"
            )
        return ClassicEAFrame.from_fields(
            packet.command,
            (),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_usr_frame(
        self,
        context: ClassicPreloginContext,
        game: GameSession,
    ) -> bytes:
        """Project one MW lobby user on the shared callback connection.

        Retail answers an uppercase ``AUXI`` callback with ``+usr`` carrying
        the sender's newest AUX text.  The room UI consumes that projection to
        associate a completed CE vector with the corresponding OPPO slot.
        ``+usm`` alone updates the userset but does not reliably refresh the
        sender's own lobby-user record when a third participant is appended.
        """
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        wire_id = self._mw_wire_user_id(user_id)
        persona = context.auth.persona or "Player"
        aux_text = game.participant_aux.get(
            user_id,
            self._participant_aux.get(user_id, ""),
        )
        projected_address = self._mw_projected_address(context, game)
        context.mw_user_sync = max(3, int(context.mw_user_sync or 0)) + 1
        active = getattr(game.state, "name", "").casefold() == "active"
        return ClassicEAFrame.from_fields(
            "+usr",
            (
                ("IDENT", wire_id),
                ("NAME", persona),
                ("PERS", persona),
                ("UID", ""),
                ("ROOM", 0),
                ("GAME", game.game_id),
                ("STAT", ""),
                ("AUX", aux_text),
                ("AUXFL", 2629632),
                ("RGB", 511),
                ("PING", 425),
                ("SEED", 0),
                ("FLAGS", "G" if active else ""),
                ("SYNC", context.mw_user_sync),
                ("ADDR", projected_address),
                ("LADDR", projected_address),
                ("SERV", self.control_endpoint.host),
                ("SPRT", context.client_port),
                ("MADDR", ""),
                ("GFIDS", 0),
                ("ATTR", ""),
                ("HWFLAG", 0),
                ("HWMASK", 0),
                ("_LEVEL", 0),
                ("MEDALS", 0),
                ("LOC", "enUS"),
                ("_REP", 0),
                ("MAC", ""),
                ("PUID", ""),
                ("_CID", ""),
                ("_CTAG", ""),
                ("CRIT", ""),
                ("SETS", 1),
                ("SESS", 1024 + wire_id),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_gam_frame(self, game: GameSession) -> bytes:
        ClassicEAFrame = _ea_frame()
        return ClassicEAFrame.from_fields(
            "+gam",
            (
                ("IDENT", game.game_id),
                ("GAME", game.game_id),
                ("NAME", game.name or game.host_persona),
                ("HOST", game.host_persona),
                ("COUNT", len(game.participants)),
                ("SYSFLAGS", U2_ACTIVE_SYSFLAG),
                ("PARAMS", game.params),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _mw_start_frames(
        self,
        context: ClassicPreloginContext,
        game: GameSession,
        seed: int,
    ) -> tuple[bytes, ...]:
        ClassicEAFrame = _ea_frame()
        display_id = game.game_id
        fields = self._mw_game_fields(
            game,
            active=True,
            viewer_id=self._user_id(context),
        )
        participant_frames: list[bytes] = []
        for user_id in game.ordered_participants():
            participant = self._context_for_user(user_id)
            if participant is not None:
                participant_frames.append(
                    self._mw_usm_frame(
                        participant,
                        game=game,
                        display_game_id=display_id,
                        flags="G",
                    )
                )
        return (
            self._mw_who_frame(
                context,
                game=game,
                display_game_id=display_id,
                flags="GU",
            ),
            *participant_frames,
            ClassicEAFrame.from_fields(
                "+mgm", fields, separator="\t", final_separator=False
            ).encode(),
            ClassicEAFrame.from_fields(
                "+ses",
                (*fields, ("SEED", seed), ("SELF", context.auth.persona)),
                separator="\t",
                final_separator=False,
            ).encode(),
        )

    def _mw_postrace_room_frames(
        self,
        viewer: ClassicPreloginContext,
        game: GameSession,
    ) -> tuple[bytes, ...]:
        """Retire an MW race object while preserving its userset room."""
        ClassicEAFrame = _ea_frame()

        frames: list[bytes] = [self._mw_who_frame(viewer, game=game)]
        for user_id in game.ordered_participants():
            participant = self._context_for_user(user_id)
            if participant is not None:
                frames.append(self._mw_usm_frame(participant, game=game))
        frames.append(
            ClassicEAFrame.from_fields(
                "+mgm",
                (("IDENT", game.game_id),),
                separator="\t",
                final_separator=False,
            ).encode()
        )
        return tuple(frames)

    def _mw_ready_refresh_frames(
        self,
        viewer: ClassicPreloginContext,
        game: GameSession,
        *,
        include_self: bool = True,
        include_onln: bool = False,
    ) -> tuple[bytes, ...]:
        """Room-state refresh consumed during an MW ready transition."""
        ClassicEAFrame = _ea_frame()
        display_id = game.game_id
        viewer_id = self._user_id(viewer)
        # MW treats +who as the identity of the connection receiving the
        # refresh.  Emitting one +who per room member changes the client's
        # local player while its ready state machine is running; peers belong
        # in +usm records instead.
        frames: list[bytes] = [
            self._mw_who_frame(
                viewer,
                game=game,
                display_game_id=display_id,
            )
        ]
        for user_id in game.ordered_participants():
            participant = self._context_for_user(user_id)
            if participant is None:
                continue
            if include_self or user_id != viewer_id:
                frames.append(
                    self._mw_usm_frame(
                        participant,
                        game=game,
                        display_game_id=display_id,
                    )
                )
            if include_onln:
                frames.append(
                    ClassicEAFrame.from_fields(
                        "onln",
                        self._mw_presence_fields(
                            participant,
                            game=game,
                            display_game_id=display_id,
                        ),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
        return tuple(frames)

    @staticmethod
    def _mw_auxiliary_for_new_game(text: str) -> str:
        """Drop transport state that belongs to the retired CommUDP graph.

        CE is emitted by the stock client and describes its current peer
        transport edges.  Reusing it after GCRE seeds the replacement room
        with the previous race's statuses even though that relay graph has
        already been retired.  Preserve every other AUX record and let the
        client publish fresh CE values as the new sockets complete; do not
        synthesize or force a connected state.
        """

        auxiliary = _MWAuxiliaryState.parse(text)
        auxiliary.records = [
            (key, value)
            for key, value in auxiliary.records
            if key is None or key.casefold() != "ce"
        ]
        return auxiliary.encode()

    def _mw_ready_state(self, game: GameSession) -> _MWReadyState:
        return self._mw_ready_states.setdefault(game.game_id, _MWReadyState())

    def _mw_record_auxiliary(
        self,
        game: GameSession,
        user_id: int,
        text: str,
    ) -> None:
        self._mw_ready_state(game).auxiliary[int(user_id)] = (
            _MWAuxiliaryState.parse(text)
        )

    def _mw_push_ready_refresh(
        self,
        game: GameSession,
        *,
        exclude: int = 0,
        include_self: bool = True,
        include_onln: bool = False,
    ) -> None:
        for user_id in set(game.participants):
            if user_id == int(exclude):
                continue
            viewer = self._context_for_user(user_id)
            if viewer is None or viewer.send_wire is None:
                continue
            for frame in self._mw_ready_refresh_frames(
                viewer,
                game,
                include_self=include_self,
                include_onln=include_onln,
            ):
                if not viewer.send_wire(frame):
                    break


__all__ = ["ClassicMWSessionMixin"]
