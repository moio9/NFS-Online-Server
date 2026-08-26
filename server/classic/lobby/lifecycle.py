"""Classic lobby connection and race-transport lifecycle.

This mixin owns U2 post-race transport retirement and socket-release cleanup.
The public prelogin API, state transitions and operational logger remain
compatible with the original service.
"""

from __future__ import annotations

import logging
from threading import Timer
from typing import TYPE_CHECKING

from classic.ea.directory import GameSession, SessionState
from classic.lobby.constants import U2_POSTRACE_UDP_GRACE_SECONDS
from classic.lobby.models import ClassicPreloginContext

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


log = logging.getLogger("classic.protocols.prelogin")


class ClassicLifecycleMixin:
    """Own connection teardown and delayed U2 transport retirement."""

    def _init_lifecycle(self) -> None:
        """Initialize state owned by the lifecycle boundary."""
        # Stock U2 closes its lobby connection at GSTA and opens a new one to
        # submit rank/RESU after the race. Preserve that identity-to-game
        # bridge independently of the per-socket prelogin context.
        self._u2_pending_games: dict[int, int] = {}
        self._u2_transport_retire_timers: dict[int, Timer] = {}

    def _clear_game_transport_state(self, game: GameSession) -> None:
        """Clear lobby-owned metadata after a transport is retired or moved."""

        self._mw_release_join_serial_slot(game.game_id)
        self._mw_postrace_handoff_returners.pop(game.game_id, None)
        game.participant_race_addresses.clear()
        self._mw_ready_states.pop(game.game_id, None)
        self._mw_session_seeds.pop(game.game_id, None)
        with self._connections_lock:
            self._mw_postrace_room_view_games.discard(game.game_id)
            self._mw_control_messages = {
                key: value
                for key, value in self._mw_control_messages.items()
                if key[0] != game.game_id
            }

    def _retire_game_transport(self, game: GameSession) -> None:
        if self.race_unregistrar is not None:
            self.race_unregistrar(game)
        self._clear_game_transport_state(game)

    def _handoff_game_transport(
        self,
        previous: GameSession,
        replacement: GameSession,
    ) -> bool:
        """Transfer a live MW relay graph to the post-race replacement game."""

        handoff = self.race_handoff
        if handoff is None:
            return False
        try:
            addresses = handoff(previous, replacement)
        except Exception:
            # A transport callback must never make GCRE fatal.  Remove either
            # possible registration and let the normal registrar rebuild a
            # fresh graph when the guests issue GJOI.
            log.exception(
                "%s failed MW race UDP handoff: old_game=%d new_game=%d",
                self.profile.game_id,
                previous.game_id,
                replacement.game_id,
            )
            if self.race_unregistrar is not None:
                self.race_unregistrar(previous)
                self.race_unregistrar(replacement)
            return False
        if addresses is None:
            return False

        replacement.participant_race_addresses = dict(addresses)
        self._clear_game_transport_state(previous)
        log.info(
            "%s handed off MW race UDP transport: old_game=%d new_game=%d "
            "participants=%d",
            self.profile.game_id,
            previous.game_id,
            replacement.game_id,
            len(addresses),
        )
        return True

    def _preserve_transport_for_guest_exit(
        self,
        game: GameSession,
        actor_id: int,
    ) -> bool:
        """Keep a room's shared relay alive when only a guest departs."""

        if int(actor_id) == int(game.owner_id):
            return False
        # MW allocates the room relay as soon as the first guest joins.  A
        # later GLEA/ULEA (including a failed third-player attempt) removes
        # only that guest's edge; unregistering the whole room invalidates the
        # virtual addresses still used by the host and the other guests.
        if self._is_most_wanted:
            return True
        return game.state in {SessionState.ACTIVE, SessionState.FINISHED}

    def _sync_preserved_game_transport(self, game: GameSession) -> None:
        """Drop departed guest sockets while retaining the shared game relay."""

        if self.race_registrar is None:
            return
        game.participant_race_addresses = self.race_registrar(game)
        log.info(
            "%s synchronized preserved race transport membership: "
            "game=%d participants=%d",
            self.profile.game_id,
            game.game_id,
            len(game.participants),
        )


    def _schedule_u2_transport_retirement(self, game: GameSession) -> None:
        """Keep the finished U2 race route alive while clients leave gameplay."""

        game_id = int(game.game_id)

        def retire() -> None:
            with self._connections_lock:
                self._u2_transport_retire_timers.pop(game_id, None)
            self._retire_game_transport(game)
            log.info(
                "%s retired U2 race UDP transport after post-race grace: game=%d",
                self.profile.game_id,
                game_id,
            )

        with self._connections_lock:
            previous = self._u2_transport_retire_timers.pop(game_id, None)
            if previous is not None:
                previous.cancel()
            timer = Timer(U2_POSTRACE_UDP_GRACE_SECONDS, retire)
            timer.daemon = True
            self._u2_transport_retire_timers[game_id] = timer
        timer.start()
        log.info(
            "%s scheduled U2 race UDP transport retirement: game=%d grace=%.1fs",
            self.profile.game_id,
            game_id,
            U2_POSTRACE_UDP_GRACE_SECONDS,
        )


    def release(self, context: ClassicPreloginContext) -> None:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        preserve_mw_membership = False
        if context.lobby_game_id and identity is not None:
            game = self.sessions.get_game(context.lobby_game_id)
            if self._is_most_wanted and game is not None:
                # A socket can vanish while its GJOI is still staged and
                # before AUX reports LT=0. Let the next queued guest proceed.
                self._mw_release_join_serial_slot(
                    game.game_id,
                    identity.user_id,
                )
            preserve_u2_race = bool(
                not self._is_most_wanted
                and game is not None
                and game.state is SessionState.ACTIVE
            )
            if preserve_u2_race:
                # Stock U2 closes both lobby sockets immediately after GSTA,
                # before sending its command-5/command-1 UDP bootstrap.  This
                # is a transport transition, not a room leave.  Retiring the
                # game here removes the route before the first race datagram.
                log.info(
                    "%s preserved active race transport after lobby close: "
                    "game=%d user=%d participants=%d",
                    self.profile.game_id,
                    game.game_id,
                    identity.user_id,
                    len(game.participants),
                )
            elif game is not None and game.owner_id == identity.user_id:
                participants = set(game.participants)
                if self._is_most_wanted:
                    self._mw_remember_departed_room_personas(
                        game,
                        viewer_ids=participants,
                        departed_ids=participants,
                    )
                self._retire_game_transport(game)
                self.sessions.close_game(game.game_id)
                closed = ClassicEAFrame.from_fields(
                    "gdel",
                    (("IDENT", game.game_id),),
                    separator="\t",
                    final_separator=False,
                ).encode()
                for user_id in participants:
                    peer = self._context_for_user(user_id)
                    if peer is not None:
                        peer.lobby_game_id = 0
                self._send_users(participants, (closed,), exclude=identity.user_id)
            else:
                preserve_shared_race_transport = bool(
                    game is not None
                    and self._preserve_transport_for_guest_exit(
                        game,
                        identity.user_id,
                    )
                )
                preserve_mw_membership = bool(
                    preserve_shared_race_transport
                    and self._is_most_wanted
                    and game is not None
                    and game.state in {SessionState.ACTIVE, SessionState.FINISHED}
                )
                if game is not None and not preserve_shared_race_transport:
                    self._retire_game_transport(game)
                elif preserve_shared_race_transport:
                    log.info(
                        "%s preserved owner race transport after guest "
                        "lobby disconnect: game=%d user=%d owner=%d membership=%d",
                        self.profile.game_id,
                        game.game_id,
                        identity.user_id,
                        game.owner_id,
                        1 if preserve_mw_membership else 0,
                    )
                if not preserve_mw_membership:
                    if self._is_most_wanted and game is not None:
                        self._mw_remember_departed_room_personas(
                            game,
                            viewer_ids=set(game.participants)
                            - {identity.user_id},
                            departed_ids={identity.user_id},
                        )
                    detached = self.sessions.leave_game(
                        context.lobby_game_id,
                        identity.user_id,
                    )
                    remaining = self.sessions.get_game(context.lobby_game_id)
                    if remaining is not None:
                        if detached and preserve_shared_race_transport:
                            self._sync_preserved_game_transport(remaining)
                        managed = ClassicEAFrame.from_fields(
                            "+mgm",
                            self._game_fields(remaining),
                            separator="\t",
                            final_separator=False,
                        ).encode()
                        self._send_users(set(remaining.participants), (managed,))
                else:
                    log.info(
                        "%s preserved active race room membership after passive "
                        "lobby close: game=%d user=%d participants=%d",
                        self.profile.game_id,
                        game.game_id,
                        identity.user_id,
                        len(game.participants),
                    )
            context.lobby_game_id = 0
        if context.userset_id and identity is not None:
            with self._usersets_lock:
                userset = self._usersets.get(context.userset_id)
                if userset is not None and not preserve_mw_membership:
                    if userset.owner_id == identity.user_id:
                        self._usersets.pop(userset.userset_id, None)
                    elif userset.members is not None:
                        userset.members.discard(identity.user_id)
                elif userset is not None:
                    log.info(
                        "%s preserved userset membership after passive race "
                        "lobby close: userset=%d game=%d user=%d members=%d",
                        self.profile.game_id,
                        userset.userset_id,
                        userset.game_id,
                        identity.user_id,
                        len(userset.members or ()),
                    )
                context.userset_id = 0
        if identity is not None:
            with self._connections_lock:
                if self._connections.get(identity.user_id) is context:
                    self._connections.pop(identity.user_id, None)
        self.auth.release(context.auth)
