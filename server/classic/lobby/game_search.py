"""Classic lobby game discovery and search filtering.

This mixin owns masked flag matching and the ``gsea`` listing response while
the session directory remains the authoritative game-state owner.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from classic.ea.directory import GameSession, SessionState
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


class ClassicGameSearchMixin:
    """Filter and project open U2/MW game sessions."""

    @classmethod
    def _masked_flag_match(
        cls,
        actual: object,
        wanted: object,
        mask: object,
    ) -> bool:
        mask_text = str(mask or "").strip()
        wanted_text = str(wanted or "").strip()
        if not mask_text:
            return True
        actual_value = cls._lobby_int(actual, 0)
        wanted_value = cls._lobby_int(wanted_text, 0)
        mask_value = cls._lobby_int(mask_text, 0)
        return (actual_value & mask_value) == (wanted_value & mask_value)

    def _game_matches_search(
        self,
        game: GameSession,
        fields: dict[str, str],
    ) -> bool:
        name = str(fields.get("NAME", "") or "").strip()
        if name and name not in {"*", "ANY", "any"}:
            if game.name.casefold() != name.casefold():
                return False
        if not self._masked_flag_match(
            game.custflags,
            fields.get("CUSTFLAGS", "0"),
            fields.get("CUSTMASK", ""),
        ):
            return False
        if not self._masked_flag_match(
            game.sysflags,
            fields.get("SYSFLAGS", "0"),
            fields.get("SYSMASK", ""),
        ):
            return False
        requested_max = self._lobby_int(fields.get("MAXSIZE", "0"), 0)
        if requested_max and game.capacity != requested_max:
            return False
        requested_min = self._lobby_int(fields.get("MINSIZE", "0"), 0)
        if requested_min and game.capacity < requested_min:
            return False
        free_slots = self._lobby_int(fields.get("FREESLOTS", "0"), 0)
        if free_slots and game.capacity - len(game.participants) < free_slots:
            return False
        return True

    def _dispatch_game_search(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        games = [
            game
            for game in self.sessions.list_games()
            if game.state is SessionState.OPEN
            and (identity is None or game.visible_to(identity.user_id))
            and (
                identity is None
                or identity.user_id not in game.kicked_participants
            )
            and self._game_matches_search(game, fields)
        ]
        search = ClassicEAFrame.from_fields(
            "gsea",
            (("COUNT", len(games)),),
            separator="\t",
            final_separator=False,
        ).encode()
        status = ClassicEAFrame.from_fields(
            "+sst",
            (
                ("GCR", 1 if games else 0),
                ("UIL", 1),
                ("UIR", 0),
                ("GIP", 1 if context.lobby_game_id else 0),
            ),
            separator="\t",
            final_separator=False,
        ).encode()
        listings = tuple(
            ClassicEAFrame.from_fields(
                "+mgm" if game.game_id == context.lobby_game_id else "+gam",
                self._game_fields(game),
                separator="\t",
                final_separator=False,
            ).encode()
            for game in games
        )
        return ClassicPreloginReply(
            (search, status, *listings),
            "game_search",
        )
