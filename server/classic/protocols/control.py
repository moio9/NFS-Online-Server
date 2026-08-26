"""Classic EA Messenger/control protocol adapter used by U2 and MW.

The wire protocol is a 12-byte EA frame with an uppercase four-byte verb and a
NUL-terminated newline-separated key/value payload.  Social state lives in
``classic.ea.social``; this module only maps commands and fields.
"""

from __future__ import annotations

import logging

from dataclasses import dataclass
from collections.abc import Iterable, Mapping
import socket
import struct
from typing import Callable

from classic.core.catalog import GameId
from classic.ea.messenger import messenger_profile
from classic.ea.social import (
    LobbyIdentity,
    SocialRow,
    SocialService,
    canonical_persona,
)

from .frame import ClassicEAFrame
from classic.lobby.mw_control_bridge import resolve_mw_control_projection


ControlSender = Callable[[str, tuple[tuple[str, str], ...]], bool]



log = logging.getLogger(__name__)

@dataclass(frozen=True)
class ClassicControlProfile:
    game_id: str
    auth_title: str = "EA MESSENGER"
    auth_product: str = "NFS-CONSOLE-2005"
    default_stat: str = "EX%3d0%0aP%3dnfs5%0a"
    default_product: str = "is playing Underground 2"
    default_title: str = "Need for Speed Underground 2 [PC]"
    default_show: str = "PASS"
    default_attr: str = "D"
    email_enabled: bool = False
    email_address: str = ""

    @classmethod
    def for_game(cls, game: GameId) -> "ClassicControlProfile":
        if game not in {GameId.UNDERGROUND2, GameId.MOST_WANTED}:
            raise ValueError(f"classic control profile cannot serve {game.value}")
        shared = messenger_profile(game)
        return cls(
            game_id=game.value,
            default_stat=shared.default_status,
            default_product=shared.default_product,
            default_title=shared.title,
            default_show=shared.default_show,
            default_attr=shared.default_attr,
        )


@dataclass
class ClassicControlContext:
    connection_id: str
    client_ip: str
    identity: LobbyIdentity | None = None
    authenticated: bool = False
    show: str = "PASS"
    stat: str = ""
    product: str = ""
    title: str = ""
    attr: str = ""
    sender: ControlSender | None = None
    close_requested: bool = False
    mw_user_sync: int = 3

    @property
    def persona(self) -> str:
        return self.identity.persona if self.identity is not None else ""


@dataclass(frozen=True)
class ClassicControlReply:
    frames: tuple[bytes, ...] = ()
    reason: str = "ok"
    close_connection: bool = False


def encode_control_frame(
    verb: str,
    fields: Mapping[str, object] | Iterable[tuple[str, object]] = (),
) -> bytes:
    command = str(verb or "")
    if len(command) != 4 or not command.isascii() or not command.isupper():
        raise ValueError(f"invalid classic control verb: {verb!r}")
    return ClassicEAFrame.from_fields(command, tuple(fields)).encode()


@dataclass(frozen=True)
class ClassicControlWireMessage:
    kind: str
    frame: ClassicEAFrame | None = None
    data: bytes = b""


class ClassicControlSocket:
    """Buffered socket reader for framed control, PREL and small HTTP requests."""

    def __init__(
        self,
        conn: socket.socket,
        *,
        max_frame_size: int = 65_535,
        max_http_size: int = 16_384,
    ) -> None:
        if int(max_frame_size) < 12:
            raise ValueError("max_frame_size must be at least 12")
        if int(max_http_size) < 512:
            raise ValueError("max_http_size must be at least 512")
        self.conn = conn
        self.max_frame_size = int(max_frame_size)
        self.max_http_size = int(max_http_size)
        self._buffer = bytearray()

    def _fill(self, size: int) -> bool:
        while len(self._buffer) < size:
            chunk = self.conn.recv(max(1, size - len(self._buffer)))
            if not chunk:
                return False
            self._buffer.extend(chunk)
        return True

    def _take(self, size: int) -> bytes:
        output = bytes(self._buffer[:size])
        del self._buffer[:size]
        return output

    def _take_until(self, marker: bytes, limit: int) -> bytes | None:
        while True:
            index = self._buffer.find(marker)
            if index >= 0:
                return self._take(index + len(marker))
            if len(self._buffer) >= limit:
                raise ValueError("classic control message exceeds configured limit")
            chunk = self.conn.recv(min(4096, limit - len(self._buffer)))
            if not chunk:
                if not self._buffer:
                    return None
                return self._take(len(self._buffer))
            self._buffer.extend(chunk)

    def read(self) -> ClassicControlWireMessage | None:
        if not self._fill(12):
            return None
        header = bytes(self._buffer[:12])
        if header.startswith((b"GET ", b"HEAD ", b"POST ")):
            request = self._take_until(b"\r\n\r\n", self.max_http_size)
            return None if request is None else ClassicControlWireMessage("http", data=request)
        if header[:4] == b"PREL" and header[4:5] in (b"\t", b"\x00"):
            payload = self._take_until(b"\x00", self.max_frame_size)
            return None if payload is None else ClassicControlWireMessage("prel", data=payload)
        if not all(65 <= value <= 90 for value in header[:4]):
            raise ValueError(f"invalid classic control verb: {header[:4].hex()}")
        if header[4:8] != b"\x00\x00\x00\x00":
            raise ValueError("classic control reserved field must be zero")
        total_length = struct.unpack(">I", header[8:12])[0]
        if total_length < 12 or total_length > self.max_frame_size:
            raise ValueError(f"invalid classic control frame length: {total_length}")
        if not self._fill(total_length):
            return None
        wire = self._take(total_length)
        frame, trailing = ClassicEAFrame.decode_one(wire)
        if trailing:
            raise ValueError("unexpected trailing bytes in classic control frame")
        return ClassicControlWireMessage("frame", frame=frame)

    def send_frame(
        self,
        verb: str,
        fields: Iterable[tuple[str, object]] = (),
    ) -> bool:
        self.conn.sendall(encode_control_frame(verb, tuple(fields)))
        return True

    def send_raw(self, data: bytes) -> None:
        self.conn.sendall(bytes(data))

    def send_http(
        self,
        body: bytes,
        *,
        content_type: str = "text/plain; charset=utf-8",
        include_body: bool = True,
    ) -> None:
        payload = bytes(body)
        header = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "Cache-Control: no-store\r\n"
            "\r\n"
        ).encode("ascii")
        self.conn.sendall(header + (payload if include_body else b""))


class ClassicControlService:
    """Translate U2/MW control verbs to the shared social-domain service."""

    _PERSONA_FIELDS = ("PERS", "PERSONA", "NAME", "NICK", "ALIAS", "FROM")
    _TARGET_FIELDS = ("USER", "PERS", "NAME", "TARGET", "TARG", "TO")

    def __init__(
        self,
        social: SocialService,
        *,
        profile: ClassicControlProfile,
    ) -> None:
        self.social = social
        self.profile = profile

    @staticmethod
    def _fields(frame: ClassicEAFrame) -> dict[str, str]:
        return frame.fields()

    @staticmethod
    def _first(fields: Mapping[str, str], names: Iterable[str]) -> str:
        for name in names:
            value = str(fields.get(name, "") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _strip_quotes(value: object) -> str:
        text = str(value or "").strip()
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            return text[1:-1]
        return text

    @classmethod
    def _target(cls, fields: Mapping[str, str]) -> str:
        return canonical_persona(cls._first(fields, cls._TARGET_FIELDS))

    @staticmethod
    def _product_identity(value: object) -> bool:
        text = str(value or "").strip().upper()
        return not text or text.startswith("/") or "NFS-CONSOLE" in text or "EA MESSENGER" in text

    def _requested_persona(self, fields: Mapping[str, str]) -> str:
        candidate = self._first(fields, self._PERSONA_FIELDS)
        if not candidate:
            user = str(fields.get("USER", "") or "").strip()
            if not self._product_identity(user):
                candidate = user
        return canonical_persona(self._strip_quotes(candidate))

    @staticmethod
    def _ack_fields(fields: Mapping[str, str], *, status: bool = False) -> tuple[tuple[str, str], ...]:
        output: list[tuple[str, str]] = []
        request_id = str(fields.get("ID", "") or "").strip()
        if request_id:
            output.append(("ID", request_id))
        if status:
            output.extend((('STAT', 'OK'), ('RESULT', 'OK')))
        return tuple(output)

    def _presence_fields(self, row: SocialRow) -> tuple[tuple[str, str], ...]:
        presence = row.presence
        show = "AWAY" if not row.online else (
            presence.show if presence and presence.show else self.profile.default_show
        )
        stat = presence.stat if presence and presence.stat else self.profile.default_stat
        product = presence.product if presence and presence.product else self.profile.default_product
        title = presence.title if presence and presence.title else self.profile.default_title
        attr = row.attr or (presence.attr if presence else "")
        fields: list[tuple[str, str]] = [
            ("EXTR", self.profile.auth_product),
            ("STAT", stat),
            ("PROD", product),
            ("TITL", title),
            ("SHOW", show),
            ("USER", row.user),
        ]
        if attr:
            fields.append(("ATTR", attr))
        return tuple(fields)

    @staticmethod
    def _roster_fields(row: SocialRow, roster_id: str) -> tuple[tuple[str, str], ...]:
        fields: list[tuple[str, str]] = [
            ("ID", roster_id),
            ("USER", row.user),
        ]
        if row.attr:
            fields.append(("ATTR", row.attr))
        return tuple(fields)

    def _require_auth(self, context: ClassicControlContext, verb: str) -> ClassicControlReply | None:
        if context.authenticated and context.identity is not None:
            return None
        return ClassicControlReply(
            (encode_control_frame(verb, (("STAT", "FAIL"), ("RESULT", "AUTH"))),),
            "not_authenticated",
            close_connection=True,
        )

    def can_authenticate(
        self,
        client_ip: str,
        fields: Mapping[str, str],
    ) -> bool:
        requested = self._requested_persona(fields)
        return self.social.resolve_lobby(
            client_ip,
            requested,
            game_id=self.profile.game_id,
            unclaimed_only=not bool(requested),
        ) is not None

    def _auth(
        self,
        fields: Mapping[str, str],
        context: ClassicControlContext,
        sender: ControlSender,
    ) -> ClassicControlReply:
        requested = self._requested_persona(fields)
        identity = self.social.register_control(
            context.connection_id,
            context.client_ip,
            requested,
            sender,
            game_id=self.profile.game_id,
        )
        if identity is None:
            return ClassicControlReply(
                (
                    encode_control_frame(
                        "AUTH",
                        (("TITL", self.profile.auth_title), ("STAT", "FAIL")),
                    ),
                ),
                "identity_not_active",
                close_connection=True,
            )
        context.identity = identity
        context.authenticated = True
        context.show = self.profile.default_show
        context.stat = self.profile.default_stat
        context.product = self.profile.default_product
        context.title = self.profile.default_title
        context.attr = self.profile.default_attr
        self.social.set_presence(
            identity.persona,
            show=context.show,
            stat=context.stat,
            product=context.product,
            title=context.title,
            attr=context.attr,
        )
        return ClassicControlReply(
            (encode_control_frame("AUTH", (("TITL", self.profile.auth_title),)),),
            "authenticated",
        )

    def _roster(self, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        list_tag = str(fields.get("LIST", "") or "B").strip().upper() or "B"
        roster_id = str(fields.get("ID", "1") or "1")
        game_id = context.identity.game_id if context.identity is not None else self.profile.game_id

        if list_tag == "I":
            candidates = self.social.snapshot(context.persona, "I")
        elif list_tag in {"P", "PLAYER", "PLAYERS"}:
            candidates = self.social.game_player_snapshot(context.persona, game_id)
        elif list_tag in {"A", "ALL"}:
            candidates = (
                *self.social.snapshot(context.persona, "B"),
                *self.social.game_player_snapshot(context.persona, game_id),
                *self.social.snapshot(context.persona, "I"),
            )
        else:
            # Stock U2/MW requests LIST=B and sorts entries into Friend List,
            # Friend Request and Player List from ATTR.  Include transient
            # same-game players with ATTR=D without persisting them as buddies.
            candidates = (
                *self.social.snapshot(context.persona, "B"),
                *self.social.game_player_snapshot(context.persona, game_id),
            )

        unique: dict[str, SocialRow] = {}
        for row in candidates:
            unique.setdefault(row.user.casefold(), row)
        rows = tuple(unique.values())
        frames: list[bytes] = [
            encode_control_frame("RGET", (("ID", roster_id), ("SIZE", len(rows)))),
        ]
        for row in rows:
            frames.append(encode_control_frame("ROST", self._roster_fields(row, roster_id)))
            if row.online or row.request:
                frames.append(encode_control_frame("PGET", self._presence_fields(row)))
        return ClassicControlReply(tuple(frames), f"roster_{list_tag.lower()}")

    def _presence(self, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        context.show = self._strip_quotes(fields.get("SHOW", "")) or context.show
        context.stat = self._strip_quotes(fields.get("STAT", "")) or context.stat
        context.product = self._strip_quotes(fields.get("PROD", "")) or context.product
        context.title = self._strip_quotes(fields.get("TITL", "")) or context.title
        if "ATTR" in fields:
            context.attr = self._strip_quotes(fields.get("ATTR", ""))
        self.social.set_presence(
            context.persona,
            show=context.show,
            stat=context.stat,
            product=context.product,
            title=context.title,
            attr=context.attr,
        )
        # Notify friends and pending peers using their owner-relative row.
        notify = self.social.snapshot(context.persona, "B")
        for relation in notify:
            target_row = self.social.presence_row(relation.user, context.persona)
            if target_row is not None:
                self.social.deliver(
                    relation.user,
                    "PGET",
                    self._presence_fields(target_row),
                )
        return ClassicControlReply(
            (encode_control_frame("PSET", self._ack_fields(fields)),),
            "presence",
        )

    def _search(self, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        request_id = str(fields.get("ID", "1") or "1")
        query = self._target(fields)
        try:
            limit = max(1, min(100, int(fields.get("MAXR", "20") or "20")))
        except (TypeError, ValueError):
            limit = 20
        rows = self.social.search(context.persona, query, limit)
        frames = [encode_control_frame("USCH", (("SIZE", len(rows)), ("ID", request_id)))]
        frames.extend(
            encode_control_frame(
                "USER",
                (("RSRC", "PC"), ("ID", request_id), ("USER", row.user)),
            )
            for row in rows
        )
        return ClassicControlReply(tuple(frames), "search")

    def _roster_add(self, verb: str, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        target = self._target(fields)
        list_tag = str(fields.get("LIST", "B") or "B").strip().upper()
        if list_tag == "I" or verb in {"BLCK", "BLOK", "RBLK", "RBLO"}:
            result = self.social.set_blocked(context.persona, target, True)
        else:
            result = self.social.request_friend(context.persona, target)
        response = list(self._ack_fields(fields, status=result.accepted))
        if target:
            response.extend((('USER', target), ('LIST', list_tag)))
        if result.accepted and target:
            if list_tag == "I":
                self.social.deliver(target, "RNOT", (("CHNG", "D"), ("USER", context.persona)))
            else:
                attr = "B" if result.reason == "accepted" else "R"
                self.social.deliver(
                    target,
                    "RNOT",
                    (("CHNG", "A"), ("USER", context.persona), ("ATTR", attr)),
                )
        return ClassicControlReply(
            (encode_control_frame(verb, tuple(response)),),
            result.reason,
        )

    def _roster_remove(self, verb: str, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        target = self._target(fields)
        list_tag = str(fields.get("LIST", "B") or "B").strip().upper()
        if list_tag == "I" or verb in {"UBLK", "UBLO", "UNBL", "BDEL"}:
            result = self.social.set_blocked(context.persona, target, False)
        else:
            result = self.social.remove_friend(context.persona, target)
        response = list(self._ack_fields(fields, status=result.accepted))
        if target:
            response.extend((('USER', target), ('LIST', list_tag)))
            self.social.deliver(
                target,
                "RNOT",
                (("CHNG", "D"), ("USER", context.persona)),
            )
        return ClassicControlReply(
            (encode_control_frame(verb, tuple(response)),),
            result.reason,
        )

    def _roster_response(self, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        target = self._target(fields)
        answer = self._first(fields, ("ANSW", "ANSWER", "ACPT", "ACCEPT", "STATUS"))
        accepted = str(answer or "Y").strip().casefold() in {"1", "a", "accept", "accepted", "ok", "true", "y", "yes"}
        result = self.social.respond_friend(context.persona, target, accepted)
        response = list(self._ack_fields(fields, status=result.accepted))
        if target:
            response.append(("USER", target))
        if result.accepted and target:
            self.social.deliver(
                target,
                "RNOT",
                (("CHNG", "A" if accepted else "D"), ("USER", context.persona), ("ATTR", "B" if accepted else "")),
            )
            if accepted:
                own_row = self.social.presence_row(target, context.persona)
                target_row = self.social.presence_row(context.persona, target)
                if own_row is not None:
                    self.social.deliver(target, "ROST", self._roster_fields(own_row, "-1"))
                    self.social.deliver(target, "PGET", self._presence_fields(own_row))
                frames: list[bytes] = [encode_control_frame("RRSP", tuple(response))]
                if target_row is not None:
                    frames.append(encode_control_frame("ROST", self._roster_fields(target_row, "-1")))
                    frames.append(encode_control_frame("PGET", self._presence_fields(target_row)))
                return ClassicControlReply(tuple(frames), result.reason)
        return ClassicControlReply((encode_control_frame("RRSP", tuple(response)),), result.reason)

    def _message(self, verb: str, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        target = self._target(fields)
        text = self._strip_quotes(self._first(fields, ("TEXT", "BODY", "MESG", "MSG")))
        delivered = 0
        if target and target.casefold() != context.persona.casefold() and not self.social.is_blocked(context.persona, target):
            delivered = self.social.deliver(
                target,
                "PMSG",
                (("USER", context.persona), ("FROM", context.persona), ("TEXT", text)),
            )
        response = list(self._ack_fields(fields, status=True))
        response.append(("DELIVERED", str(delivered)))
        return ClassicControlReply((encode_control_frame(verb, tuple(response)),), "message")

    def _send(self, fields: Mapping[str, str], context: ClassicControlContext) -> ClassicControlReply:
        message_type = str(fields.get("TYPE", "") or "C").strip().upper()
        if message_type in {"F", "FR", "REQ", "B"}:
            return self._roster_add("SEND", {**fields, "LIST": "B"}, context)
        target = self._target(fields)
        body = self._first(fields, ("BODY", "TEXT", "MESG", "MSG"))
        text = self._strip_quotes(body)
        delivered = 0
        if target and target.casefold() != context.persona.casefold() and not self.social.is_blocked(context.persona, target):
            target_row = self.social.presence_row(target, context.persona)
            if target_row is not None:
                self.social.deliver(target, "PGET", self._presence_fields(target_row))
            self.social.deliver(target, "PADD", (("LRSC", "PC"), ("USER", context.persona)))
            delivery_fields: list[tuple[str, str]] = [
                ("USER", context.persona),
                ("N", context.persona),
                ("T", text),
                ("F", "P"),
                ("TYPE", message_type or "C"),
                ("BODY", body),
            ]
            seconds = str(fields.get("SECS", "") or "").strip()
            if seconds:
                delivery_fields.append(("SECS", seconds))
            delivered = self.social.deliver(target, "RECV", tuple(delivery_fields))
        response: list[tuple[str, str]] = []
        for name in ("SECS", "USER", "TYPE", "BODY"):
            value = str(fields.get(name, "") or "")
            if value:
                response.append((name, value))
        response.append(("DELIVERED", str(delivered)))
        return ClassicControlReply((encode_control_frame("SEND", tuple(response)),), "send")

    # MW REAL AUXI CALLBACK HANDLER: retail answers Messenger AUXI with
    # the token ACK followed by a +usr projection for the same lobby user.
    def _dispatch_mw_auxiliary_callback(
        self,
        frame: ClassicEAFrame,
        context: ClassicControlContext,
        fields: Mapping[str, str],
    ) -> ClassicControlReply:
        raw_wire = self._first(fields, ("CALLUSER", "IDENT", "USER"))
        try:
            wire_id = max(0, int(self._strip_quotes(raw_wire) or 0))
        except (TypeError, ValueError):
            wire_id = 0
        identity = getattr(context, "identity", None)
        persona = (
            getattr(context, "persona", "")
            or getattr(identity, "persona", "")
            or getattr(context, "account", "")
            or "Player"
        )
        projection = resolve_mw_control_projection(
            wire_id=wire_id,
            persona=persona,
            client_ip=context.client_ip,
        )
        if projection is not None:
            wire_id = projection.wire_id or wire_id
            persona = projection.persona or persona
            game_id = projection.game_id
            address = projection.address
            aux = self._first(fields, ("TEXT", "AUX")) or projection.aux
        else:
            game_id = 0
            address = self._first(fields, ("CALLADDR", "ADDR", "LADDR")) or context.client_ip
            aux = self._first(fields, ("TEXT", "AUX"))
        context.mw_user_sync = max(3, int(context.mw_user_sync or 0)) + 1
        # Stock MW callback replies reuse the request token as the command
        # word and carry one NUL payload byte (13 bytes total).
        if frame.reserved:
            ack = (
                struct.pack(">I", int(frame.reserved) & 0xFFFFFFFF)
                + b"\x00\x00\x00\x00"
                + struct.pack(">I", 13)
                + b"\x00"
            )
        else:
            ack = ClassicEAFrame.from_fields(
                frame.command,
                (),
                separator="\t",
                final_separator=False,
            ).encode()
        usr = ClassicEAFrame.from_fields(
            "+usr",
            (
                ("IDENT", wire_id),
                ("NAME", persona),
                ("PERS", persona),
                ("UID", ""),
                ("ROOM", 0),
                ("GAME", game_id),
                ("STAT", ""),
                ("AUX", aux),
                ("AUXFL", 2629632),
                ("RGB", 511),
                ("PING", 425),
                ("SEED", 0),
                ("FLAGS", ""),
                ("SYNC", context.mw_user_sync),
                ("ADDR", address),
                ("LADDR", address),
                ("SERV", ""),
                ("SPRT", 0),
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
        log.info(
            "MW CONTROL AUXI callback projected: persona=%s wire=%d game=%d aux=%s",
            persona, wire_id, game_id, aux,
        )
        return ClassicControlReply((ack, usr), "mw_auxiliary_callback")

    def dispatch(
        self,
        frame: ClassicEAFrame,
        context: ClassicControlContext,
        sender: ControlSender,
    ) -> ClassicControlReply:
        verb = frame.command.upper()
        fields = self._fields(frame)
        if (
            verb == "AUXI"
            and self.profile.game_id == GameId.MOST_WANTED.value
        ):
            return self._dispatch_mw_auxiliary_callback(frame, context, fields)
        if (
            self.profile.game_id == GameId.MOST_WANTED.value
            and verb in {"AUXI", "PSET"}
        ):
            log.info(
                "MW CONTROL CALLBACK TRACE: command=%s connection=%s authenticated=%d persona=%s fields=%r",
                verb,
                context.connection_id,
                int(bool(context.authenticated)),
                getattr(context, "persona", ""),
                fields,
            )

        if verb == "AUTH":
            return self._auth(fields, context, sender)
        if verb == "DISC":
            return ClassicControlReply((), "client_disconnect", True)

        auth_error = self._require_auth(context, verb)
        if auth_error is not None:
            return auth_error

        if verb == "EPGT":
            request_id = str(fields.get("ID", "4") or "4")
            return ClassicControlReply(
                (
                    encode_control_frame(
                        "EPGT",
                        (
                            ("ID", request_id),
                            ("ENAB", "T" if self.profile.email_enabled else "F"),
                            ("ADDR", self.profile.email_address),
                        ),
                    ),
                ),
                "email_preferences",
            )
        if verb == "RGET":
            return self._roster(fields, context)
        if verb == "PSET":
            return self._presence(fields, context)
        if verb in {"PDEL", "PADD"}:
            return ClassicControlReply(
                (encode_control_frame(verb, self._ack_fields(fields, status=verb == "PDEL")),),
                "presence_ack",
            )
        if verb == "USCH":
            return self._search(fields, context)
        if verb in {"RADD", "RSET", "RADM", "BLCK", "BLOK", "RBLK", "RBLO"}:
            return self._roster_add(verb, fields, context)
        if verb in {"RDEL", "RREM", "RDEM", "UBLK", "UBLO", "UNBL", "BDEL"}:
            return self._roster_remove(verb, fields, context)
        if verb == "RRSP":
            return self._roster_response(fields, context)
        if verb in {"PMSG", "MMSG", "MESG"}:
            return self._message(verb, fields, context)
        if verb == "SEND":
            return self._send(fields, context)
        if verb in {"ABUS", "RPRT", "REPT", "CMPL", "INVT", "INVI", "INVL", "GINV", "PINV"}:
            return ClassicControlReply(
                (encode_control_frame(verb, self._ack_fields(fields, status=True)),),
                "acknowledged",
            )
        return ClassicControlReply(
            (encode_control_frame(verb, self._ack_fields(fields)),),
            "generic_ack",
        )

    def release(self, context: ClassicControlContext) -> None:
        self.social.unregister_control(context.connection_id)
        context.identity = None
        context.authenticated = False
