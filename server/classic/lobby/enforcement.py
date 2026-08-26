"""Immediate account-policy cleanup for Classic lobby and race state."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Iterable

from classic.ea.directory import GameSession
from classic.lobby.models import ClassicPreloginContext, ClassicUserset


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


log = logging.getLogger("classic.protocols.prelogin")


@dataclass(frozen=True)
class ClassicAccountEnforcementResult:
    games_closed: int = 0
    usersets_deleted: int = 0
    userset_members_removed: int = 0
    contexts_reset: int = 0


class ClassicAccountEnforcementMixin:
    """Remove restricted identities from every in-memory Classic domain."""

    @staticmethod
    def _reset_game_context(context: ClassicPreloginContext) -> None:
        context.lobby_game_id = 0
        context.mw_join_pending_game_id = 0
        context.mw_postrace_return_pending = False
        context.mw_postrace_snapshot_game_id = 0
        context.mw_postrace_room_view_game_id = 0
        context.mw_deferred_usea_game_id = 0
        context.mw_deferred_gjoi_game_id = 0

    @classmethod
    def _reset_userset_context(cls, context: ClassicPreloginContext) -> None:
        context.userset_id = 0
        context.mw_userset_staged_game_id = 0
        cls._reset_game_context(context)

    def _cancel_game_retirement(self, game_id: int) -> None:
        with self._connections_lock:
            timer = self._u2_transport_retire_timers.pop(int(game_id), None)
        if timer is not None:
            timer.cancel()

    def _close_policy_game(
        self,
        game: GameSession,
        restricted_user_ids: set[int],
    ) -> None:
        ClassicEAFrame = _ea_frame()
        participants = set(game.participants)
        self._cancel_game_retirement(game.game_id)
        self._retire_game_transport(game)
        self.sessions.close_game(game.game_id)
        with self._connections_lock:
            self._u2_pending_games = {
                user_id: pending_game_id
                for user_id, pending_game_id in self._u2_pending_games.items()
                if pending_game_id != game.game_id
            }

        peers: dict[int, ClassicPreloginContext] = {}
        for user_id in participants:
            peer = self._context_for_user(user_id)
            if peer is not None:
                self._reset_game_context(peer)
                peers[user_id] = peer

        remaining = participants - restricted_user_ids
        if self._is_most_wanted:
            deleted_game = ClassicEAFrame.from_fields(
                "+mgm",
                (("IDENT", game.game_id),),
                separator="\t",
                final_separator=False,
            ).encode()
            roster = tuple(
                self._mw_usm_frame(peers[user_id])
                for user_id in sorted(remaining)
                if user_id in peers
            )
            for user_id in remaining:
                peer = peers.get(user_id)
                sender = peer.send_wire if peer is not None else None
                if sender is None:
                    continue
                if not sender(self._mw_who_frame(peer)):
                    continue
                for frame in roster:
                    if not sender(frame):
                        break
                else:
                    sender(deleted_game)
        else:
            for user_id in remaining:
                peer = peers.get(user_id)
                sender = peer.send_wire if peer is not None else None
                if sender is None:
                    continue
                for frame in self._closed_game_reset_frames(peer, game):
                    if not sender(frame):
                        break

    def _enforce_usersets(
        self,
        restricted_user_ids: set[int],
        closed_game_ids: set[int],
    ) -> tuple[int, int]:
        ClassicEAFrame = _ea_frame()
        deleted: list[tuple[ClassicUserset, set[int], bytes]] = []
        updated: list[tuple[ClassicUserset, set[int], bytes]] = []
        removed_members = 0

        with self._usersets_lock:
            for userset in tuple(self._usersets.values()):
                members = set(userset.members or ())
                restricted_members = members & restricted_user_ids
                game_closed = bool(userset.game_id in closed_game_ids)
                if not restricted_members and not game_closed:
                    continue
                if userset.owner_id in restricted_user_ids:
                    self._usersets.pop(userset.userset_id, None)
                    survivors = members - restricted_user_ids
                    deleted.append(
                        (
                            userset,
                            survivors,
                            ClassicEAFrame.from_fields(
                                "+ust",
                                (("I", userset.userset_id),),
                                separator="\t",
                                final_separator=False,
                            ).encode(),
                        )
                    )
                    removed_members += len(restricted_members)
                    continue

                if userset.members is not None:
                    userset.members.difference_update(restricted_user_ids)
                if game_closed:
                    userset.game_id = 0
                removed_members += len(restricted_members)
                survivors = set(userset.members or ())
                updated.append(
                    (userset, survivors, self._mw_userset_frame("+ust", userset))
                )

        for userset, survivors, frame in deleted:
            for user_id in survivors:
                peer = self._context_for_user(user_id)
                if peer is None:
                    continue
                if peer.userset_id == userset.userset_id:
                    self._reset_userset_context(peer)
                if peer.send_wire is not None:
                    peer.send_wire(frame)
        for userset, survivors, frame in updated:
            for user_id in survivors:
                peer = self._context_for_user(user_id)
                if peer is not None and peer.send_wire is not None:
                    peer.send_wire(frame)
        return len(deleted), removed_members

    def enforce_account_policy(
        self,
        user_ids: Iterable[int],
        *,
        reason: str,
    ) -> ClassicAccountEnforcementResult:
        """Synchronously remove account personas from games, rooms and usersets."""

        restricted = {int(user_id) for user_id in user_ids if int(user_id) > 0}
        if not restricted:
            return ClassicAccountEnforcementResult()

        games = [
            game
            for game in self.sessions.list_games()
            if restricted.intersection(game.participants)
        ]
        closed_game_ids = {int(game.game_id) for game in games}
        for game in games:
            self._close_policy_game(game, restricted)

        usersets_deleted = 0
        userset_members_removed = 0
        if self._is_most_wanted:
            usersets_deleted, userset_members_removed = self._enforce_usersets(
                restricted,
                closed_game_ids,
            )

        contexts: list[tuple[int, ClassicPreloginContext]] = []
        with self._connections_lock:
            for user_id in restricted:
                context = self._connections.get(user_id)
                if context is not None:
                    contexts.append((user_id, context))
                self._participant_aux.pop(user_id, None)
                self._u2_pending_games.pop(user_id, None)
        with self._usersets_lock:
            for user_id in restricted:
                self._wire_user_ids.pop(user_id, None)

        for user_id, context in contexts:
            if context.u2_room_id:
                self.sessions.leave_room(context.u2_room_id, user_id)
            context.u2_room_id = 0
            context.u2_room_name = ""
            self._reset_userset_context(context)

        log.info(
            "%s account policy cleanup: reason=%s users=%s games_closed=%d "
            "usersets_deleted=%d userset_members_removed=%d contexts_reset=%d",
            self.profile.game_id,
            reason,
            ",".join(str(user_id) for user_id in sorted(restricted)),
            len(games),
            usersets_deleted,
            userset_members_removed,
            len(contexts),
        )
        return ClassicAccountEnforcementResult(
            games_closed=len(games),
            usersets_deleted=usersets_deleted,
            userset_members_removed=userset_members_removed,
            contexts_reset=len(contexts),
        )


__all__ = [
    "ClassicAccountEnforcementMixin",
    "ClassicAccountEnforcementResult",
]
