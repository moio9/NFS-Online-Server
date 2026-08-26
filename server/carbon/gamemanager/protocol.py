"""Named Carbon Online/GameManager wire constants.

The numeric OLMSG mapping is recovered directly from the retail
``rebroadcaster.exe`` message table at 0x004ac444.  Keeping it in one place
prevents room/race state code from depending on unexplained hexadecimal
literals.

Not every *payload* is decoded yet.  A named message type means that the outer
message identity is known; it does not imply that every field in its body is
semantic.
"""

from __future__ import annotations

from enum import IntEnum


LOGICAL_PREFIX = b"\x00\x00\x00\x00"
PLAIN_TERMINATOR = b"\x04"
DESTINATION_FOOTER_FLAG = b"\x44"
NGL_FOOTER_FLAG = b"\x40"
NGL_FOOTER_WITH_TRAILER = b"\x40\x0d"
REDUNDANT_BODY_SEPARATOR = b"\x0c"


class OLMessageType(IntEnum):
    """Retail Carbon OLMSG ids from the rebroadcaster type table."""

    GAME_INIT_INFO = 0x00
    CLOCK_SYNC_REQUEST = 0x01
    CLOCK_SYNC_START = 0x02
    CLOCK_SYNC_END = 0x03
    PLAYER_CAR_DATA = 0x04
    PLAYER_CONTROLLED_AI_CAR = 0x05
    CAR_STATE = 0x06
    CAR_STATE_BLOCK = 0x07
    START_LOADING = 0x08
    READY_TO_START = 0x09
    START_RACE_SYNC_BEGIN = 0x0A
    START_RACE_SYNC_MESSAGE = 0x0B
    START_RACE = 0x0C
    LEADER_FINISHED = 0x0D
    RACER_FINISHED = 0x0E
    READY = 0x0F
    GAME_RESULTS = 0x10
    FINAL_GAME_RESULTS = 0x11
    LATENCY_INFO = 0x12
    MATCHMAKING_ON_REQUEST = 0x13
    MATCHMAKING_OFF_REQUEST = 0x14
    INVITES_ON_REQUEST = 0x15
    INVITES_OFF_REQUEST = 0x16
    ENABLE_JOINS_REQUEST = 0x17
    DISABLE_JOINS_REQUEST = 0x18
    ACTIVE_GAME_COLLECT_STATS = 0x19
    ACTIVE_GAME_UPDATE_STATS = 0x1A
    START_TIMER = 0x1B
    ACTIVE_GAME_MESSAGE = 0x1C
    GAME_ATTRIBUTES = 0x1D
    BIG_MESSAGE = 0x1E
    PURSUIT_TAG_SYNC = 0x1F
    KILL_REBROADCASTER = 0x20
    POST_RACE_SYNC = 0x21


class ObservedTimerId(IntEnum):
    """Capture-confirmed Carbon StartTimer ids.

    The names describe observed lifecycle roles.  They are intentionally kept
    separate from :class:`OLMessageType`, because all three are payload values
    carried by OLMSG ``0x1B``.
    """

    ROOM_WAIT_WINDOW = 0
    RETAIL_RACE_COUNTDOWN = 2
    POST_RACE_WINDOW = 4
    RACE_COUNTDOWN = 5


class GMMessageType(IntEnum):
    """GameManager packet ids carried inside CommUDP active messages."""

    SESSION_TICKET = 0x00
    HOST_HELLO = 0x02
    PLAYER_ROSTER = 0x03
    PLAYER_PUBLISH = 0x04
    PLAYER_JOINED = 0x05
    PLAYER_LEFT = 0x07
    HOST_PROPERTIES = 0x0C


def gm_message_tag(message_type: GMMessageType | int) -> bytes:
    parsed = int(message_type)
    if not 0 <= parsed <= 0x7F:
        raise ValueError(f"GameManager type out of range: {message_type}")
    return b"\x01" + bytes((0x80 | parsed,))


class ObservedActiveGameState(IntEnum):
    """Observed values inside OLMSG 0x1C.

    The outer message is semantically identified as ``ActiveGame_Message``.
    These inner state meanings are based on captures and client-side effects,
    not recovered enum symbols, so the names intentionally say what was
    observed rather than claiming a complete original definition.
    """

    COUNTDOWN_EXPIRED = 2
    COUNTDOWN_CONTEXT = 9
    PLAYER_COUNTDOWN_CONTEXT = 14
    ACTIVE_GAME_ALLOCATING = 15


def logical_message(message_type: OLMessageType | int, payload: bytes = b"") -> bytes:
    """Build an un-terminated logical OLMSG body."""

    parsed = int(message_type)
    if not 0 <= parsed <= 0xFF:
        raise ValueError(f"OLMSG type out of range: {message_type}")
    return LOGICAL_PREFIX + bytes((parsed,)) + bytes(payload)


def with_plain_terminator(body: bytes) -> bytes:
    return bytes(body) + PLAIN_TERMINATOR
