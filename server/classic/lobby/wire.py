"""Shared Classic lobby wire projections and frame builders.

The methods are extracted from the historical pre-login service without
changing command tags, field order, separators or callback semantics.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from classic.ea.directory import GameSession
from classic.lobby.constants import (
    U2_ACTIVE_SYSFLAG,
    U2_PARTITION_COUNT,
    U2_PARTITION_INDEX,
    U2_PASSWORD_SYSFLAG,
    U2_READY_FLAG,
)
from classic.lobby.models import ClassicPreloginContext

if TYPE_CHECKING:
    from classic.protocols.frame import ClassicEAFrame


def _ea_frame():
    """Load the wire codec lazily to avoid a protocols-package import cycle."""
    from classic.protocols.frame import ClassicEAFrame

    return ClassicEAFrame


log = logging.getLogger("classic.protocols.prelogin")


class ClassicWireMixin:
    """Build common U2/MW lobby frames and parse shared scalar fields."""

    @staticmethod
    def _signed_ack(command: str) -> bytes:
        ClassicEAFrame = _ea_frame()
        return ClassicEAFrame.signed(command, b"\x00", 9).encode()

    def _news_frame(self, *, client_ip: str = "") -> bytes:
        ClassicEAFrame = _ea_frame()
        messenger = self._endpoint_for_client(self.control_endpoint, client_ip)
        web = self._endpoint_for_client(self.web_endpoint, client_ip)
        http_host = web.host if web.port in (0, 80) else f"{web.host}:{web.port}"
        lines = tuple(
            [f"{key}=http://{http_host}/tos" for key in self.profile.tos_url_keys]
            + [
                "CIRCUIT_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999,119999",
                "DRAG_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999,119999",
                "URL_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999,119999",
                f"BUDDY_SERVER={messenger.host}",
                f"BUDDY_PORT={messenger.port}",
                "STREET_CROSS_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999,119999",
                f"{self.profile.news_url_key}=http://{http_host}{self.profile.news_path}",
                "SPRINT_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999",
                "DRIFT_TIER_POINTS=0,1999,4999,9999,19999,39999,59999,79999,99999,119999",
            ]
        )
        payload = ("\n".join(lines) + "\n").encode("utf-8") + b"\x00"
        total_payload_length = max(
            self.profile.news_payload_length,
            len(payload) + 8,
        )
        return ClassicEAFrame.signed(
            "news",
            payload,
            total_payload_length,
            reserved=self.profile.news_reserved,
        ).encode()

    def _user_frame(self, context: ClassicPreloginContext) -> bytes:
        ClassicEAFrame = _ea_frame()
        stats = self._stats_csv(context)
        persona = context.auth.persona or "Player"
        if self._is_most_wanted:
            values = stats.rstrip(",").split(",")
            if len(values) >= 33:
                log.info(
                    "MW USER STAT skill slots: persona=%s circuit=%s "
                    "sprint=%s drag=%s",
                    persona,
                    values[8],
                    values[20],
                    values[32],
                )
        if self._is_underground2:
            # The stock client keeps a local game-report sequence.  The
            # nfsuserver USER contract seeds both sides at 186; without these
            # fields U2 declares the post-race report refused and never sends
            # its rank/RESU frame.  Keep LMSTAT/LGAME as compatibility fields
            # for the current profile/statistics views, but provide the full
            # report acknowledgement envelope consumed by the retail client.
            payload = (
                f"PERS={persona}\n"
                "LAST=2004.6.1 15:57:52\n"
                "EXPR=1072566000\n"
                f"STAT={stats}\n"
                "CHEAT=3\n"
                "ACK_REP=186\n"
                "REP=186\n"
                "PLAST=2004.6.1 15:57:46\n"
                "PSINCE=2003.11.25 07:56:09\n"
                "DCNT=0\n"
                f"ADDR={context.client_address}\n"
                "SERV=159.153.229.239\n"
                "RANK=99999\n"
                "MESG=\n"
                f"LMSTAT={stats}\n"
                "LGAME=\n"
            ).encode("utf-8") + b"\x00"
        else:
            payload = (
                f"LMSTAT={stats}\n"
                f"STAT={stats}\n"
                "LGAME=\n"
            ).encode("utf-8") + b"\x00"
        return ClassicEAFrame.signed(
            "user",
            payload,
            max(self.profile.user_payload_length, len(payload) + 8),
        ).encode()

    @staticmethod
    def _auxiliary_frame() -> bytes:
        ClassicEAFrame = _ea_frame()
        return ClassicEAFrame.signed("auxi", b"\x00", 9).encode()

    @staticmethod
    def _fields(frame: ClassicEAFrame) -> dict[str, str]:
        return frame.fields()

    @staticmethod
    def _lobby_int(value: object, default: int = 0) -> int:
        text = str(value or "").strip()
        if not text:
            return int(default)
        try:
            return int(text, 0)
        except (TypeError, ValueError):
            try:
                return int(text, 16)
            except (TypeError, ValueError):
                return int(default)

    @staticmethod
    def _message_frame(
        text: str,
        sender: str,
        *,
        attr: str = "",
        flag: str = "",
        quote_text: bool = False,
        include_user: bool = False,
    ) -> bytes:
        ClassicEAFrame = _ea_frame()
        if quote_text:
            text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
        fields: list[tuple[str, object]] = []
        if flag:
            fields.append(("F", flag))
        fields.append(("T", text))
        if include_user:
            fields.append(("U", ""))
        fields.append(("N", sender))
        if attr:
            fields.append(("A", attr))
        return ClassicEAFrame.from_fields(
            "+msg", fields, separator="\t", final_separator=False
        ).encode()

    def _game_fields(
        self,
        game: GameSession,
        *,
        viewer_id: int = 0,
        start: bool = False,
    ) -> tuple[tuple[str, object], ...]:
        host = game.host_persona or "Player"
        address = game.host_address or "127.0.0.1"
        local_id = int(viewer_id or game.owner_id)
        race_endpoint = self._race_endpoint_for_participant(game, local_id)
        when = time.localtime(game.created_at)
        created = (
            f"{when.tm_year}.{when.tm_mon}.{when.tm_mday} "
            f"{when.tm_hour:02d}:{when.tm_min:02d}:{when.tm_sec:02d}"
        )
        try:
            sysflags = int(str(game.sysflags or "0"), 0)
        except (TypeError, ValueError):
            sysflags = 0
        if game.password:
            # FUN_004f1620 opens UI_OLPassword.fng when the parsed game's
            # SYSFLAGS has bit 0x10000 and no password has been entered yet.
            sysflags |= U2_PASSWORD_SYSFLAG
        if start:
            sysflags |= U2_ACTIVE_SYSFLAG
        room = self._u2_room(game.room_id) if self._is_underground2 else None
        room_value: object = room[1] if room is not None else game.room_id
        fields: list[tuple[str, object]] = [
            ("IDENT", game.game_id),
            ("WHEN", created),
            ("NAME", game.name or host),
            ("HOST", host),
            ("ROOM", room_value),
            ("MAXSIZE", game.capacity),
            ("MINSIZE", game.min_players),
            ("COUNT", len(game.participants)),
            ("CUSTFLAGS", game.custflags),
            ("SYSFLAGS", sysflags),
            ("EVID", 0),
            ("EVGID", 0),
            # Stock U2's game record parser is order-sensitive.  NUMPART
            # immediately follows EVGID in captures from the working server.
            ("NUMPART", U2_PARTITION_COUNT),
        ]
        participants = sorted(
            game.participants,
            key=lambda user_id: (
                0 if viewer_id and user_id == viewer_id else 1,
                0 if user_id == game.owner_id else 1,
                user_id,
            ),
        )
        if start:
            fields.extend(
                (
                    ("LIMIT", game.capacity),
                    ("FLAGS", game.custflags),
                    ("PARAMS", game.params),
                )
            )
            if race_endpoint is not None:
                fields.extend(
                    (
                        ("RLYHOST", race_endpoint.host),
                        ("RLYPORT", race_endpoint.port),
                    )
                )
        elif len(participants) == 1 and race_endpoint is not None:
            # This is the captured GCRE layout used to initialise the local
            # stock game object before a guest joins.
            fields.extend(
                (
                    ("RLYHOST", race_endpoint.host),
                    ("RLYPORT", race_endpoint.port),
                )
            )
        for index, user_id in enumerate(participants):
            persona = game.participant_personas.get(
                user_id, host if user_id == game.owner_id else f"Player{user_id}"
            )
            participant_address = (
                game.participant_race_addresses.get(user_id)
                if start
                else None
            ) or game.participant_addresses.get(
                user_id, address if user_id == game.owner_id else "127.0.0.1"
            )
            fields.extend(
                (
                    (f"OPID{index}", user_id),
                    (f"OPPO{index}", persona),
                    (f"ADDR{index}", participant_address),
                    (f"LADDR{index}", participant_address),
                    (f"MADDR{index}", ""),
                    (f"OPPART{index}", U2_PARTITION_INDEX),
                    (
                        f"OPFLAG{index}",
                        U2_READY_FLAG if user_id in game.ready_participants else 0,
                    ),
                )
            )
            if start:
                fields.append((f"OPPARAM{index}", game.params))
        fields.append(("PARTSIZE0", game.capacity))
        if start:
            fields.append(("PARTPARAMS0", game.params))
        else:
            fields.append(("PARAMS", game.params))
            fields.append(("PARTPARAMS0", ""))
            for index, _user_id in enumerate(participants):
                fields.append((f"OPPARAM{index}", ""))
            # The working classic server advertises the relay throughout the
            # room lifecycle, not only after GSTA.  This also lets online.asi
            # learn the endpoint before the game tears down the lobby socket.
            if len(participants) != 1 and race_endpoint is not None:
                fields.extend(
                    (
                        ("RLYHOST", race_endpoint.host),
                        ("RLYPORT", race_endpoint.port),
                    )
                )
        return tuple(fields)

    def _who_frame(self, context: ClassicPreloginContext, game_id: int) -> bytes:
        ClassicEAFrame = _ea_frame()
        identity = context.auth.identity
        persona = context.auth.persona or "Player"
        user_id = identity.user_id if identity is not None else 0
        aux_text = self._participant_aux.get(user_id, "")
        game = self.sessions.get_game(game_id) if game_id else None
        if game is not None:
            aux_text = game.participant_aux.get(user_id, aux_text)
        stats = self.ranking.full_hex_csv(self.profile.game_id, persona)
        return ClassicEAFrame.from_fields(
            "+who",
            (
                ("I", user_id),
                ("M", context.auth.account.account_name if context.auth.account else persona),
                ("N", persona),
                ("F", "U"),
                ("A", context.client_address),
                ("P", 1),
                ("S", stats),
                ("X", aux_text),
                ("G", game_id),
                ("AT", ""),
                ("CL", 0),
                ("LV", 0),
                ("MD", 0),
                ("LA", context.client_address),
                ("HW", 0),
                ("RP", 0),
                ("MA", context.client_address),
                ("US", ""),
                ("C", ""),
            ),
            separator="\t",
            final_separator=False,
        ).encode()

    def _game_membership_reset_frames(
        self,
        context: ClassicPreloginContext,
        game: GameSession,
    ) -> tuple[bytes, ...]:
        ClassicEAFrame = _ea_frame()
        return (
            # Clear the local user's game membership before updating the
            # current game.  In U2's +mgm consumer this selects callback 5
            # (the local participant was removed).
            self._who_frame(context, 0),
            # Keep NAME and the complete surviving-game record.  The client
            # therefore takes its update/removal branch and can distinguish a
            # kick from the whole session simply disappearing.
            ClassicEAFrame.from_fields(
                "+mgm",
                self._game_fields(game),
                separator="\t",
                final_separator=False,
            ).encode(),
            ClassicEAFrame.from_fields(
                "+sst",
                (
                    ("GCR", 0),
                    ("UIL", 1),
                    ("UIR", 0),
                    ("GIP", 0),
                ),
                separator="\t",
                final_separator=False,
            ).encode(),
        )

    def _kicked_reset_frames(
        self,
        context: ClassicPreloginContext,
        game: GameSession,
    ) -> tuple[bytes, ...]:
        ClassicEAFrame = _ea_frame()
        persona = context.auth.persona or "Player"
        return (
            ClassicEAFrame.from_fields(
                "gset",
                (("NAME", game.name), ("KICK", persona)),
                separator="\t",
                final_separator=False,
            ).encode(),
            self._message_frame(
                f"You have been kicked out of the room by {game.host_persona}",
                "Server",
                # Original server flags are 0x210000 after its target-user and
                # non-self routing bits are applied: P (private) + U (user).
                flag="PU",
                quote_text=True,
            ),
            *self._game_membership_reset_frames(context, game),
        )

    def _closed_game_reset_frames(
        self,
        context: ClassicPreloginContext,
        game: GameSession,
    ) -> tuple[bytes, ...]:
        ClassicEAFrame = _ea_frame()
        return (
            self._who_frame(context, 0),
            # IDENT without NAME is U2's game-deleted event.  It selects the
            # session-closed callback instead of the kick/participant-removed
            # callback used by a complete +mgm record.
            ClassicEAFrame.from_fields(
                "+mgm",
                (("IDENT", game.game_id),),
                separator="\t",
                final_separator=False,
            ).encode(),
            ClassicEAFrame.from_fields(
                "+sst",
                (
                    ("GCR", 0),
                    ("UIL", 1),
                    ("UIR", 0),
                    ("GIP", 0),
                ),
                separator="\t",
                final_separator=False,
            ).encode(),
        )
