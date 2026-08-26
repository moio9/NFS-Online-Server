"""State models used by the shared Underground 2/Most Wanted lobby."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from classic.protocols.auth import ClassicAuthContext

_MW_AUX_LINE_BREAK = re.compile(r"%0a", re.IGNORECASE)
_MW_AUX_EQUALS = re.compile(r"%3d", re.IGNORECASE)


@dataclass
class _MWAuxiliaryState:
    """Parsed MW AUX text with unknown records preserved in wire order."""

    records: list[tuple[str | None, str]] = field(default_factory=list)
    quoted: bool = False
    trailing_break: bool = True

    @classmethod
    def parse(cls, text: str) -> "_MWAuxiliaryState":
        wire = str(text or "")
        quoted = len(wire) >= 2 and wire.startswith('"') and wire.endswith('"')
        body = wire[1:-1] if quoted else wire
        trailing_break = bool(re.search(r"%0a$", body, re.IGNORECASE))
        records: list[tuple[str | None, str]] = []
        for line in _MW_AUX_LINE_BREAK.split(body):
            if not line:
                continue
            encoded = _MW_AUX_EQUALS.search(line)
            plain_index = line.find("=")
            if encoded is not None and (
                plain_index < 0 or encoded.start() < plain_index
            ):
                records.append((line[: encoded.start()], line[encoded.end() :]))
            elif plain_index >= 0:
                records.append((line[:plain_index], line[plain_index + 1 :]))
            else:
                records.append((None, line))
        return cls(records, quoted, trailing_break)

    def get(self, name: str) -> str | None:
        expected = str(name).casefold()
        return next(
            (
                value
                for key, value in self.records
                if key is not None and key.casefold() == expected
            ),
            None,
        )

    def set(self, name: str, value: str) -> None:
        expected = str(name).casefold()
        replacement = (str(name), str(value))
        updated: list[tuple[str | None, str]] = []
        replaced = False
        for key, current in self.records:
            if key is not None and key.casefold() == expected:
                if not replaced:
                    updated.append(replacement)
                    replaced = True
                continue
            updated.append((key, current))
        if not replaced:
            updated.append(replacement)
        self.records = updated

    def encode(self) -> str:
        lines = [
            value if key is None else f"{key}%3d{value}"
            for key, value in self.records
        ]
        body = "%0a".join(lines)
        if lines and self.trailing_break:
            body += "%0a"
        return f'"{body}"' if self.quoted else body


@dataclass
class _MWReadyState:
    auxiliary: dict[int, _MWAuxiliaryState] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassicPreloginProfile:
    game_id: str
    news_payload_length: int = 567
    news_reserved: int = 0x6E657737  # "new7"
    selection_payload_length: int = 102
    user_payload_length: int = 106
    tos_url_keys: tuple[str, ...] = ("TOSURL",)
    news_url_key: str = "NEWSURL"
    news_path: str = "/news"
    lobby_heartbeat_wire: bytes | Callable[[], bytes] = b""
    u2_game_size_policy: str = "client"
    u2_game_min_players: int = 2
    u2_game_max_players: int = 4


@dataclass
class ClassicPreloginContext:
    auth: ClassicAuthContext
    client_address: str = ""
    client_port: int = 0
    authenticated: bool = False
    persona_selected: bool = False
    lobby_game_id: int = 0
    u2_room_id: int = 0
    u2_room_name: str = ""
    u2_rooms_requested: bool = False
    userset_id: int = 0
    mw_userset_staged_game_id: int = 0
    mw_join_pending_game_id: int = 0
    mw_join_snapshot_pending_game_id: int = 0
    mw_join_pending_viewer_ids: set[int] = field(default_factory=set)
    mw_staged_onln_target_ids: dict[int, set[int]] = field(default_factory=dict)
    mw_departed_room_personas: set[str] = field(default_factory=set)
    mw_postrace_return_pending: bool = False
    mw_postrace_snapshot_game_id: int = 0
    mw_postrace_room_view_game_id: int = 0
    mw_deferred_usea_game_id: int = 0
    mw_deferred_gjoi_game_id: int = 0
    mw_user_sync: int = 3
    send_wire: Callable[[bytes], bool] | None = None
    close_requested: bool = False
    close_reason: str = ""


@dataclass
class ClassicUserset:
    userset_id: int
    owner_id: int
    owner_persona: str
    name: str
    capacity: int = 4
    type_value: str = "0"
    sysflags: str = "KV"
    custflags: str = "JKM-"
    params: str = ""
    description: str = ""
    game_id: int = 0
    members: set[int] | None = None

    def __post_init__(self) -> None:
        if self.members is None:
            self.members = {self.owner_id}


@dataclass(frozen=True)
class ClassicPreloginReply:
    frames: tuple[bytes, ...]
    reason: str = "ok"
    close_connection: bool = False
    after_send: Callable[[], None] | None = None


__all__ = [
    "ClassicPreloginContext",
    "ClassicPreloginProfile",
    "ClassicPreloginReply",
    "ClassicUserset",
]
