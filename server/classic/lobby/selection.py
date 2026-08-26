"""Classic lobby capability selection and MW lobby re-entry snapshots.

The ``sele`` command starts the stock U2 room path and restores MW userset/game
views after a race.  The extracted mixin preserves callback and frame order.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from classic.ea.directory import SessionState
from classic.lobby.models import ClassicPreloginContext, ClassicPreloginReply

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


# Preserve the existing operational logger category.
log = logging.getLogger("classic.protocols.prelogin")


class ClassicSelectionMixin:
    """Handle capability selection and MW post-race lobby restoration."""

    def _selection_frame(self) -> bytes:
        ClassicEAFrame = _ea_frame()
        if self._is_underground2:
            # Retail U2/nfsuserver capability record.  In particular, GAMES=1
            # and SLOTS=36 select the stock room/game path that later emits
            # rank/RESU for races started from a ranked room.
            payload = (
                b"GAMES=1\n"
                b"ROOMS=1\n"
                b"USERS=1\n"
                b"MESGS=1\n"
                b"RANKS=0\n"
                b"MORE=1\n"
                b"SLOTS=36\n\x00"
            )
        else:
            payload = (
                b"ROOMS=1\n"
                b"SLOTS=32\n"
                b"USERSET=1\n"
                b"MORE=1\n"
                b"MYGAME=1\n"
                b"RANKS=1\n"
                b"GAMES=2\n"
                b"ASYNC=1\n"
                b"STATS=500\n"
                b"MESGS=1\n"
                b"USERS=5\n\x00"
            )
        return ClassicEAFrame.signed(
            "sele",
            payload,
            self.profile.selection_payload_length,
        ).encode()

    def _dispatch_selection(
        self,
        context: ClassicPreloginContext,
        fields: dict[str, str],
    ) -> ClassicPreloginReply:
        ClassicEAFrame = _ea_frame()
        if self._is_underground2:
            context.u2_rooms_requested = True
        selection = self._selection_frame()
        if (
            self._is_most_wanted
            and context.userset_id
            and context.auth.identity is not None
        ):
            with self._usersets_lock:
                selected_userset = self._usersets.get(context.userset_id)
            selected_game = (
                self.sessions.get_game(selected_userset.game_id)
                if selected_userset is not None
                and selected_userset.game_id
                else None
            )
            user_id = context.auth.identity.user_id
            if (
                selected_game is not None
                and user_id in selected_game.participants
                and context.lobby_game_id != selected_game.game_id
            ):
                stale_game_id = context.lobby_game_id
                context.lobby_game_id = selected_game.game_id
                context.mw_join_pending_game_id = 0
                context.mw_postrace_return_pending = False
                context.mw_postrace_snapshot_game_id = 0
                context.mw_postrace_room_view_game_id = 0
                context.mw_deferred_usea_game_id = 0
                context.mw_deferred_gjoi_game_id = 0
                log.info(
                    "%s corrected stale lobby game from userset: "
                    "user=%d stale_game=%d current_game=%d userset=%d",
                    self.profile.game_id,
                    user_id,
                    stale_game_id,
                    selected_game.game_id,
                    selected_userset.userset_id,
                )
        if self._is_most_wanted and "INGAME" in fields:
            if fields.get("INGAME") == "1":
                context.mw_postrace_return_pending = False
                context.mw_postrace_snapshot_game_id = 0
                context.mw_postrace_room_view_game_id = 0
                context.mw_deferred_usea_game_id = 0
                context.mw_deferred_gjoi_game_id = 0
            elif fields.get("INGAME") == "0" and context.lobby_game_id:
                selected_game = self.sessions.get_game(
                    context.lobby_game_id
                )
                postrace_pending = bool(
                    selected_game is not None
                    and selected_game.state
                    in {SessionState.ACTIVE, SessionState.FINISHED}
                )
                if (
                    postrace_pending
                    and not context.mw_postrace_return_pending
                ):
                    context.mw_join_pending_game_id = 0
                    context.mw_postrace_snapshot_game_id = 0
                    context.mw_postrace_room_view_game_id = 0
                    context.mw_deferred_usea_game_id = 0
                    context.mw_deferred_gjoi_game_id = 0
                context.mw_postrace_return_pending = postrace_pending
        if self._is_most_wanted and context.auth.identity is not None:
            log.info(
                "%s lobby select fields: user=%d INGAME=%s MYGAME=%s "
                "USERSETS=%s lobby_game=%d userset=%d",
                self.profile.game_id,
                self._user_id(context),
                fields.get("INGAME", "-"),
                fields.get("MYGAME", "-"),
                fields.get("USERSETS", "-"),
                context.lobby_game_id,
                context.userset_id,
            )
        if (
            self._is_most_wanted
            and context.userset_id
            and (
                fields.get("MYGAME") == "1"
                or fields.get("USERSETS") == "1"
            )
        ):
            with self._usersets_lock:
                userset = self._usersets.get(context.userset_id)
            game = None
            if context.lobby_game_id:
                game = self.sessions.get_game(context.lobby_game_id)
            if game is None and userset is not None and userset.game_id:
                game = self.sessions.get_game(userset.game_id)
            if (
                userset is not None
                and game is not None
            ):
                # Stock MW keeps a guest that finishes first attached to
                # the active race game. Its MYGAME subscription receives
                # a complete active-game snapshot, so the client returns
                # to the same room and waits for the owner. Once the owner
                # recreates the game, gcre clears the guest's game id and
                # the waiting-room bridge below takes over.
                if (
                    context.lobby_game_id == game.game_id
                    and context.mw_postrace_return_pending
                ):
                    with self._connections_lock:
                        game_in_room_view = (
                            game.game_id
                            in self._mw_postrace_room_view_games
                        )
                    if game_in_room_view or (
                        context.mw_postrace_room_view_game_id
                        == game.game_id
                    ):
                        context.mw_postrace_snapshot_game_id = 0
                        context.mw_postrace_room_view_game_id = game.game_id
                        if self._user_id(context) != game.owner_id:
                            room_frames = list(
                                self._mw_postrace_room_frames(context, game)
                            )
                            room_frames.insert(
                                1,
                                self._mw_userset_frame("+ust", userset),
                            )
                            room_frames.append(
                                ClassicEAFrame.from_fields(
                                    "+sst",
                                    (
                                        (
                                            "UIL",
                                            len(userset.members or set()),
                                        ),
                                        ("UIR", 0),
                                        ("UIG", 0),
                                        ("GIP", 0),
                                        ("GCR", 0),
                                        ("GCM", 2),
                                    ),
                                    separator="\t",
                                    final_separator=False,
                                ).encode()
                            )
                            log.info(
                                "%s replayed guest post-race room view "
                                "on lobby select: user=%d userset=%d "
                                "game=%d participants=%d",
                                self.profile.game_id,
                                self._user_id(context),
                                userset.userset_id,
                                game.game_id,
                                len(game.participants),
                            )
                            return ClassicPreloginReply(
                                (selection, *room_frames),
                                "selection_postrace_guest_room_view",
                            )
                        return ClassicPreloginReply(
                            (selection,),
                            "selection_postrace_room_view",
                        )
                    frames: list[bytes] = [
                        selection,
                        self._mw_userset_frame("+ust", userset),
                    ]
                    for user_id in sorted(
                        game.participants,
                        key=lambda candidate: (
                            candidate != game.owner_id,
                            candidate,
                        ),
                    ):
                        participant = self._context_for_user(user_id)
                        if participant is not None:
                            frames.append(
                                self._mw_usm_frame(
                                    participant,
                                    game=game,
                                    display_game_id=game.game_id,
                                    flags="G",
                                )
                            )
                    frames.extend(
                        (
                            ClassicEAFrame.from_fields(
                                "+mgm",
                                self._mw_game_fields(
                                    game,
                                    active=game.state
                                    in {
                                        SessionState.ACTIVE,
                                        SessionState.FINISHED,
                                    },
                                    viewer_id=self._user_id(context),
                                ),
                                separator="\t",
                                final_separator=False,
                            ).encode(),
                            ClassicEAFrame.from_fields(
                                "+sst",
                                (
                                    ("UIL", 0),
                                    ("UIR", 0),
                                    ("UIG", len(game.participants)),
                                    ("GIP", 1),
                                    ("GCR", 0),
                                    ("GCM", 2),
                                ),
                                separator="\t",
                                final_separator=False,
                            ).encode(),
                        )
                    )
                    context.mw_postrace_snapshot_game_id = game.game_id
                    log.info(
                        "%s replayed phase-1 active post-race snapshot "
                        "on lobby select: user=%d userset=%d game=%d "
                        "participants=%d",
                        self.profile.game_id,
                        self._user_id(context),
                        userset.userset_id,
                        game.game_id,
                        len(game.participants),
                    )
                    return ClassicPreloginReply(
                        tuple(frames),
                        "selection_postrace_phase1_active_game",
                    )
                if context.lobby_game_id:
                    return ClassicPreloginReply(
                        (selection,),
                        "selection",
                    )
                owner = self._context_for_user(game.owner_id)
                frames: list[bytes] = [
                    selection,
                    self._mw_userset_frame("+ust", userset),
                    self._mw_usm_frame(context),
                ]
                if owner is not None:
                    frames.append(
                        self._mw_usm_frame(
                            owner,
                            game=game,
                            display_game_id=game.game_id,
                        )
                    )
                frames.append(
                    ClassicEAFrame.from_fields(
                        "+sst",
                        (
                            ("UIL", len(userset.members or set())),
                            ("UIR", 0),
                            ("UIG", 0),
                            ("GIP", 0),
                            ("GCR", 1),
                            ("GCM", 1),
                        ),
                        separator="\t",
                        final_separator=False,
                    ).encode()
                )
                log.info(
                    "%s replayed post-race userset bridge on lobby select: "
                    "user=%d userset=%d game=%d",
                    self.profile.game_id,
                    self._user_id(context),
                    userset.userset_id,
                    game.game_id,
                )
                return ClassicPreloginReply(
                    tuple(frames),
                    "selection_postrace_bridge",
                )
        return ClassicPreloginReply((selection,), "selection")
