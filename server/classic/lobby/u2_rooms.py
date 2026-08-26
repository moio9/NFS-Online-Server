"""Underground 2 lobby rooms, room presence and room movement.

This mixin owns the stock U2 room catalogue, room population projections,
room-scoped ``+who/+usr`` frames, game-size policy and the ``move`` command.
The extraction keeps the historical pre-login API and wire ordering intact.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from classic.ea.directory import GameSession
from classic.lobby.constants import U2_ROOMS
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


class ClassicU2RoomMixin:
    """Own Underground 2 room discovery, population and movement."""

    @staticmethod
    def _u2_room(room_id: int = 0, name: str = "") -> tuple[int, str] | None:
        wanted_id = int(room_id or 0)
        wanted_name = str(name or "").strip().casefold()
        for candidate_id, candidate_name in U2_ROOMS:
            if wanted_id and candidate_id == wanted_id:
                return candidate_id, candidate_name
            if wanted_name and candidate_name.casefold() == wanted_name:
                return candidate_id, candidate_name
        return None

    def _u2_game_sizes(
        self,
        fields: dict[str, str],
        *,
        current: GameSession | None = None,
    ) -> tuple[int, int]:
        """Resolve U2 MINSIZE/MAXSIZE without overriding valid retail choices."""
        configured_min = max(1, min(8, int(self.profile.u2_game_min_players)))
        configured_max = max(
            configured_min,
            min(8, int(self.profile.u2_game_max_players)),
        )
        if self.profile.u2_game_size_policy == "server":
            minimum, capacity = configured_min, configured_max
        else:
            fallback_min = current.min_players if current is not None else configured_min
            fallback_max = current.capacity if current is not None else configured_max

            def player_count(name: str, fallback: int) -> int:
                try:
                    return int(fields.get(name, "") or fallback)
                except (TypeError, ValueError):
                    return fallback

            capacity = max(1, min(8, player_count("MAXSIZE", fallback_max)))
            minimum = max(1, min(capacity, player_count("MINSIZE", fallback_min)))
        if current is not None:
            capacity = max(capacity, len(current.participants))
            minimum = min(minimum, capacity)
        return minimum, capacity

    def _u2_room_count(self, room_id: int) -> int:
        with self._connections_lock:
            return sum(
                1
                for connection in self._connections.values()
                if connection.u2_room_id == int(room_id)
            )

    def _u2_room_frame(self, room_id: int, name: str) -> bytes:
        ClassicEAFrame = _ea_frame()
        return ClassicEAFrame.from_fields(
            "+rom",
            (
                ("I", room_id),
                ("N", name),
                ("H", "3PriedeZ"),
                ("F", "CK"),
                ("T", self._u2_room_count(room_id)),
                ("L", 50),
                ("P", 0),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _u2_room_frames(self) -> tuple[bytes, ...]:
        return tuple(
            self._u2_room_frame(room_id, name)
            for room_id, name in U2_ROOMS
        )

    def _u2_who_frame(self, context: ClassicPreloginContext) -> bytes:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        persona = context.auth.persona or "Player"
        account = (
            context.auth.account.account_name
            if context.auth.account is not None
            else persona
        )
        return ClassicEAFrame.from_fields(
            "+who",
            (
                ("I", user_id),
                ("N", persona),
                ("M", account),
                ("F", ""),
                ("A", context.client_address),
                ("S", self._stats_csv(context)),
                ("X", self._participant_aux.get(user_id, "")),
                ("R", context.u2_room_name),
                ("RI", context.u2_room_id),
                ("RF", "C"),
                ("RT", 1),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _u2_usr_frame(self, context: ClassicPreloginContext) -> bytes:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        user_id = identity.user_id if identity is not None else 0
        persona = context.auth.persona or "Player"
        account = (
            context.auth.account.account_name
            if context.auth.account is not None
            else persona
        )
        return ClassicEAFrame.from_fields(
            "+usr",
            (
                ("I", user_id),
                ("N", persona),
                ("M", account),
                ("F", "H"),
                ("A", context.client_address),
                ("P", 211),
                ("S", self._stats_csv(context)),
                ("X", self._participant_aux.get(user_id, "")),
                ("G", context.lobby_game_id),
                ("T", 2),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _dispatch_u2_move(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        if identity is None or not context.auth.persona:
            return ClassicPreloginReply(
                (ClassicEAFrame.short("userbadc"),),
                "not_authenticated",
                True,
            )
        requested_name = str(
            fields.get("NAME")
            or fields.get("ROOM")
            or fields.get("N")
            or ""
        ).strip()
        room = self._u2_room(name=requested_name)
        if room is None:
            empty = ClassicEAFrame.from_fields(
                "move",
                (
                    ("IDENT", 0),
                    ("NAME", ""),
                    ("COUNT", 0),
                    ("LIDENT", context.u2_room_id),
                    ("LCOUNT", 0),
                ),
                separator="\t",
                final_separator=False,
            ).encode()
            context.u2_room_id = 0
            context.u2_room_name = ""
            return ClassicPreloginReply((empty,), "room_left")

        context.u2_room_id, context.u2_room_name = room
        self._register(context)
        count = self._u2_room_count(context.u2_room_id)
        moved = ClassicEAFrame.from_fields(
            "move",
            (
                ("IDENT", context.u2_room_id),
                ("NAME", context.u2_room_name),
                ("COUNT", count),
                ("FLAGS", "C"),
            ),
            separator="\t",
            final_separator=False,
        ).encode()
        user = self._u2_usr_frame(context)
        population = ClassicEAFrame.from_fields(
            "+pop",
            (("Z", f"{context.u2_room_id}/{count}"),),
            separator="\t",
            final_separator=False,
        ).encode()
        with self._connections_lock:
            room_users = {
                user_id
                for user_id, peer in self._connections.items()
                if peer.u2_room_id == context.u2_room_id
            }
        self._send_users(
            room_users,
            (user, population),
            exclude=identity.user_id,
        )
        return ClassicPreloginReply(
            (moved, self._u2_who_frame(context), user, population),
            "room_moved",
        )
