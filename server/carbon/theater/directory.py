"""Authoritative Carbon game directory shared by FESL and Theater."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import md5
import ipaddress
import logging
import socket
from threading import RLock
import time
from typing import Callable
from uuid import uuid4

from carbon.accounts.identity import Identity, MAX_CARBON_WIRE_PLAYER_ID
from carbon.core.config import Endpoint
from carbon.ea.directory import GameSession, SessionDirectory
from carbon.theater.matchmaking import (
    CHALLENGE_ROOM_IDENTITY,
    CHALLENGE_ROOM_IDENTITY_PROPERTIES,
    DEDICATED_DEFAULTS,
    GAME_MODE_RACE_PROPERTY,
    PlayNowRequest,
    RACE_PREFERENCES,
    RACE_PROPERTY_GAME_MODE,
    match_fit,
    parse_request,
    is_coop_state_bridge,
    is_direct_coop_helper_reset,
    resolved_dedicated_properties,
    selected_challenge_event,
    selected_race_property,
    strict_match,
)


log = logging.getLogger(__name__)


def _direct_local_race_endpoint(internal_ip: str, port: int) -> Endpoint | None:
    """Resolve the local interface used for a directly attached IPv4 peer.

    UDP connect does not send a packet; it only asks the kernel which source
    address its routing table would use.  The conservative /24 check prevents
    an arbitrary private address routed through the default gateway from being
    advertised as a LAN endpoint.
    """
    try:
        remote = ipaddress.IPv4Address(str(internal_ip).strip())
    except ipaddress.AddressValueError:
        return None
    if not remote.is_private or remote.is_loopback or remote.is_unspecified:
        return None

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((str(remote), 9))
        local = ipaddress.IPv4Address(sock.getsockname()[0])
    except (OSError, ipaddress.AddressValueError):
        return None
    finally:
        sock.close()

    if not local.is_private or local.is_loopback or local.is_unspecified:
        return None
    if remote not in ipaddress.IPv4Network(f"{local}/24", strict=False):
        return None
    return Endpoint(str(local), int(port))

def _positive_int(value: object, default: int, *, minimum: int = 1, maximum: int = 64) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _ranked_from_properties(properties: dict[str, str]) -> bool:
    for key in ("B-U-ranked", "B-ranked", "ranked", "RANKED", "QROptionsRankedMode"):
        if key not in properties:
            continue
        value = str(properties[key]).strip().casefold()
        if value in {"1", "true", "yes", "on", "ranked", "r"}:
            return True
        if value in {"0", "false", "no", "off", "unranked", "u"}:
            return False
    # NFSC FUN_008293c0 reads game_type from GDAT and FUN_004aad10 defines
    # ranked as exactly game_type == 0.  matchmaking_state only controls
    # whether the room is currently discoverable and may become 0 when the
    # dedicated server locks matchmaking without changing the ranked mode.
    return str(properties.get("B-U-game_type", "")).strip() == "0"


@dataclass(frozen=True)
class CarbonParticipant:
    identity: Identity
    player_id: int
    internal_ip: str = "0.0.0.0"
    internal_port: int = 0
    # Non-zero only when Theater EGAM entered through the Messenger invite
    # path and explicitly named the already-present remote participant via
    # UID/R-UID.  Retail Carbon uses a different 0x0183/0x0185 publication
    # order for this path than for PlayNow quick join.
    invite_remote_player_id: int = 0
    # Server lifecycle metadata, not a Theater wire field. It bounds an EGAM
    # membership which never reaches the UDP GameManager transport.
    entered_at: float = field(default_factory=time.monotonic)


@dataclass
class CarbonGame:
    gid: str
    ugid: str
    lobby_id: str
    session: GameSession
    host: Identity
    properties: dict[str, str]
    participants: dict[int, CarbonParticipant] = field(default_factory=dict)
    # Messenger may close an accepted invite only after Theater has actually
    # written the invited participant's EGEG completion.  The shared Classic
    # process observes this set through the authenticated room snapshot.
    completed_invite_entries: set[int] = field(default_factory=set)
    server_hosted: bool = False
    allocator_user_id: int | None = None
    coop_match_help_type: str | None = None
    # GameManager owns the live Ready/race access lock while FESL owns public
    # matchmaking. Keep the bridge explicit so an otherwise state=1 room
    # disappears from Quick Join as soon as Ready locks it. Theater invite
    # entry remains a separate, authenticated path.
    quick_join_locked: bool = False
    # Challenge rooms begin in their settings phase and must not be selected
    # by PlayNow there. GameManager promotes this flag only after the native
    # Ready control/seed has been observed.
    challenge_ready: bool = False
    # Retail's 0x22 GameInitInfo descriptor carries a room-stable 32-bit
    # server tick.  Captures show a different value for every room and the
    # same value for every participant joining that room.  Preserve the tick
    # from authoritative directory allocation instead of copying one capture.
    created_tick_ms: int = 0
    # The 0x22 descriptor handle is an opaque allocator token. Official rooms
    # use several unrelated bases (0x64, 0xBE, 0x3D4, 0x56E, ...), advancing by
    # ten for each room slot. Reserve a stable range per room rather than
    # copying one capture's base into every dedicated session.
    descriptor_handle_base: int = 0
    # Capacity can be refined by the host's first authoritative Challenge
    # attributes after the initial descriptor was already published. Reserve
    # the full supported handle span at allocation time so growing a room from
    # the neutral two-player identity cannot overlap the next room's handles.
    descriptor_slot_capacity: int = 0
    # Monotonic allocation time used only for local stale-room cleanup.
    created_at: float = field(default_factory=time.monotonic)

    @property
    def is_ranked(self) -> bool:
        """Return the ranked state published by the client/session."""
        return _ranked_from_properties(self.properties)

    def row(self) -> dict[str, str]:
        count = len(self.participants)
        host_user_id = str(self.host.profile_id)
        join_players = str(max(0, self.session.capacity - count))
        quick_players = str(count)
        if self.server_hosted:
            # Retail dedicated GDAT does not expose the synthetic nfsdevserver
            # account id as the room owner. Across create, quick-join and invite
            # captures it publishes HU=1, AP=<entered participants>, JP=0 and
            # QP=0. The EGEG HUID remains a separate transport identifier.
            host_user_id = "1"
            join_players = "0"
            quick_players = "0"

        # Carbon consumes GDAT incrementally. Retail publishes the complete
        # session identity/population block before any B-U-* attributes. If
        # attributes arrive first, the game can still be joined, but the
        # OnlineGameBrowser has no session object to attach them to.
        header_keys = {
            "N",
            "TYPE",
            "I",
            "P",
            "PW",
            "V",
            "B-version",
            "PL",
            "J",
        }
        row = {
            "LID": self.lobby_id,
            "GID": self.gid,
            "N": self.properties.get("N", ""),
            "TYPE": self.properties.get("TYPE", "G"),
            "MP": str(self.session.capacity),
            "I": self.properties.get("I", ""),
            "P": self.properties.get("P", ""),
            "PW": self.properties.get("PW", "0"),
            "AP": str(count),
            "JP": join_players,
            "QP": quick_players,
            "HN": self.host.persona,
            "HU": host_user_id,
            "V": self.properties.get("V", "1.0"),
            "B-version": self.properties.get("B-version", ""),
            "PL": self.properties.get("PL", "PC"),
            "J": self.properties.get("J", "O"),
        }
        row.update(
            (key, value)
            for key, value in self.properties.items()
            if key not in header_keys
        )
        return row

    def invite_fields(self) -> dict[str, str]:
        """Return the authoritative room snapshot for an outgoing invite.

        The shared Messenger derives its optional ``GSTR`` description from
        these fields.  The same resolved values also describe the room found
        later by Theater ``GDAT USER=<inviter>``; they are not copied wholesale
        into the retail ``GNOT`` envelope.
        """

        row = self.row()

        def property_value(name: str, default: str = "") -> str:
            value = str(row.get(f"B-U-{name}", "") or "").strip()
            return value if value else str(default)

        game_type = property_value("game_type", "1")
        game_mode = property_value("game_mode", DEDICATED_DEFAULTS["game_mode"])
        race_property: str | None = None
        selected_event = ""

        if game_type == "2":
            challenge_event = selected_challenge_event(row, prefix="B-U-")
            if challenge_event is not None:
                race_property, selected_event = challenge_event
        else:
            race_property = selected_race_property(row, prefix="B-U-")
            if race_property is None:
                race_property = GAME_MODE_RACE_PROPERTY.get(game_mode)
            selected_event = (
                property_value(race_property)
                if race_property is not None
                else ""
            )
            if selected_event.upper() in {"", "ABSTAIN"}:
                selected_event = ""
                for candidate in GAME_MODE_RACE_PROPERTY.values():
                    value = property_value(candidate)
                    if value.upper() not in {"", "ABSTAIN"}:
                        race_property = candidate
                        selected_event = value
                        break

        if race_property is not None and selected_event:
            game_mode = RACE_PROPERTY_GAME_MODE.get(race_property, game_mode)

        track = property_value("track")
        if not track or track.upper() == "ABSTAIN":
            track = selected_event or "ABSTAIN"

        race_fields = {name: "ABSTAIN" for name in RACE_PREFERENCES}
        if race_property is not None and selected_event:
            race_fields[race_property] = selected_event

        fields = {
            "version": property_value(
                "version",
                row.get("B-version", "298_prod_server+22012b18"),
            ),
            "matchmaking_state": property_value("matchmaking_state", "0"),
            "game_type": game_type,
            "help_type": property_value("help_type", "0"),
            "game_mode": game_mode,
            "skill": property_value("skill"),
            "team_play": property_value("team_play", "1"),
            "car_tier": property_value(
                "car_tier",
                DEDICATED_DEFAULTS["car_tier"],
            ),
            "max_online_player": property_value(
                "max_online_player",
                str(self.session.capacity),
            ),
            "length": property_value("length", DEDICATED_DEFAULTS["length"]),
            "track": track,
            "n2o": property_value("n2o", DEDICATED_DEFAULTS["n2o"]),
            "collision_detection": property_value(
                "collision_detection",
                DEDICATED_DEFAULTS["collision_detection"],
            ),
            "player_dnf": property_value("player_dnf"),
            "location": property_value("location", "WH-EU"),
            **race_fields,
            # These are the standard Theater population fields.  The client
            # session object uses them for the current/max player display.
            "AP": str(len(self.participants)),
            "MP": str(self.session.capacity),
        }
        if game_type == "2":
            # Challenge identity is allocation-owned.  Its concrete game_mode
            # remains useful for presentation, but the room must never inherit
            # Unranked/Ranked identity from the invitee's local session.
            for name, value in CHALLENGE_ROOM_IDENTITY.items():
                if name not in {"game_mode", "max_online_player"}:
                    fields[name] = value
        return fields

@dataclass(frozen=True)
class CarbonTicketResolution:
    game: CarbonGame
    participant: CarbonParticipant


@dataclass(frozen=True)
class CarbonPlayNowResolution:
    game: CarbonGame
    avg_fit: float
    created: bool




def _match_properties(game: CarbonGame) -> dict[str, str]:
    """Return room properties as seen by the PlayNow fit engine.

    Retail publishes help_type=0 in the final game_type=2 GDAT/0x1D15, while
    still matching helpers against the requester's original Career/Challenge
    preference (1/2/3). Keep that private matchmaking hint off the wire.
    """
    properties = dict(game.properties)
    if game.coop_match_help_type:
        properties["B-U-help_type"] = game.coop_match_help_type
    return properties

class CarbonGameDirectory:
    def __init__(
        self,
        race_endpoint: Endpoint,
        *,
        local_race_endpoint: Endpoint | None = None,
        ekey: str = "9181081919",
        server_huid: int = 799_270_239,
        player_id_resolver: Callable[[Identity], int] | None = None,
        challenge_quick_join_before_ready: bool = False,
        challenge_quick_join_after_ready: bool = False,
        local_route_resolver: Callable[[str, int], Endpoint | None] | None = None,
    ) -> None:
        self.race_endpoint = race_endpoint
        self.local_race_endpoint = local_race_endpoint
        self.ekey = str(ekey)
        self.server_huid = int(server_huid)
        self.player_id_resolver = player_id_resolver
        self._local_route_resolver = (
            local_route_resolver or _direct_local_race_endpoint
        )
        self.challenge_quick_join_before_ready = bool(
            challenge_quick_join_before_ready
        )
        self.challenge_quick_join_after_ready = bool(
            challenge_quick_join_after_ready
        )
        self.sessions = SessionDirectory()
        self._lock = RLock()
        self._games: dict[str, CarbonGame] = {}
        self._next_gid = 235_278
        self._next_descriptor_handle = 100

    def race_endpoint_for(
        self,
        internal_ip: str,
        external_ip: str = "",
    ) -> Endpoint:
        """Choose a directly reachable listener for same-NAT LAN clients."""
        local = self.local_race_endpoint
        if local is not None and str(internal_ip).strip() == local.host:
            return local
        if local is None:
            return self.race_endpoint

        internal = str(internal_ip).strip()
        external = str(external_ip).strip()
        same_server_network = external in {internal, self.race_endpoint.host}
        if same_server_network:
            routed = self._local_route_resolver(internal, local.port)
            if routed is not None:
                return routed
        return self.race_endpoint

    def _allocate_room_tick_ms(self) -> int:
        """Return a non-zero tick that is unique across active rooms."""
        candidate = int(time.monotonic() * 1000.0) & 0xFFFFFFFF
        if candidate == 0:
            candidate = 1
        used = {
            int(game.created_tick_ms) & 0xFFFFFFFF
            for game in self._games.values()
            if int(game.created_tick_ms) != 0
        }
        while candidate in used or candidate == 0:
            candidate = (candidate + 1) & 0xFFFFFFFF
            if candidate == 0:
                candidate = 1
        return candidate

    def _allocate_descriptor_handle_base(self, capacity: int) -> int:
        """Reserve one ten-spaced descriptor-handle range for a room."""
        slots = max(2, min(64, int(capacity)))
        width = slots * 10
        base = max(10, int(self._next_descriptor_handle))
        highest = base + (slots - 1) * 10
        if highest > 0xFFFFFFFF:
            base = 100
            highest = base + (slots - 1) * 10

        active_ranges = tuple(
            (
                int(game.descriptor_handle_base),
                int(game.descriptor_handle_base)
                + (
                    max(
                        2,
                        int(game.session.capacity),
                        int(game.descriptor_slot_capacity),
                    )
                    - 1
                )
                * 10,
            )
            for game in self._games.values()
            if int(game.descriptor_handle_base) > 0
        )
        while any(not (highest < start or base > end) for start, end in active_ranges):
            base += 10
            highest = base + (slots - 1) * 10
            if highest > 0xFFFFFFFF:
                base = 100
                highest = base + (slots - 1) * 10
        self._next_descriptor_handle = base + width
        return base


    def _player_id(self, identity: Identity, used_ids: set[int]) -> int:
        if self.player_id_resolver is None:
            candidate = 1
        else:
            candidate = int(self.player_id_resolver(identity))
            if not 0 < candidate <= MAX_CARBON_WIRE_PLAYER_ID:
                raise ValueError(
                    f"Carbon player id out of range for {identity.persona}: {candidate}"
                )
        while candidate in used_ids:
            candidate = (
                1 if candidate >= MAX_CARBON_WIRE_PLAYER_ID else candidate + 1
            )
        return candidate

    @staticmethod
    def _ugid(gid: str) -> str:
        # GID allocation restarts with the process, but UGID identifies the
        # concrete room instance to the stock client.  Deriving it only from
        # GID made the first room after every restart reuse the same identity,
        # allowing cached Challenge event details (track/tier) to leak into a
        # newly created room with the same numeric GID.
        del gid
        return str(uuid4())

    def create(
        self,
        identity: Identity,
        fields: dict[str, str] | None = None,
        *,
        server_hosted: bool = False,
        play_now_request: PlayNowRequest | None = None,
    ) -> CarbonGame:
        values = dict(fields or {})
        request = play_now_request or parse_request(values)

        if server_hosted:
            dedicated = resolved_dedicated_properties(values, request)
            max_players = _positive_int(
                dedicated.get("B-U-max_online_player", "8"),
                8,
                minimum=2,
                maximum=8,
            )
        else:
            max_players = _positive_int(
                values.get("MAX-PLAYERS", values.get("B-U-max_online_player", "8")),
                8,
                minimum=2,
                maximum=8,
            )
            version = values.get("B-U-version", values.get("B-version", "298_prod_server+22012b18"))
            raw_type = values.get("B-U-game_type", "1")
            game_type = raw_type if raw_type in {"0", "1", "2"} else "1"
            dedicated = {
                "B-version": str(version),
                "B-U-version": str(version),
                "B-U-max_online_player": str(max_players),
                "B-U-matchmaking_state": str(values.get("B-U-matchmaking_state", "0")),
                "B-U-game_type": str(game_type),
                "B-U-game_mode": str(values.get("B-U-game_mode", "1")),
                "B-U-skill": str(values.get("B-U-skill", "0")),
            }

        with self._lock:
            self._next_gid += 1
            gid = str(self._next_gid)
            host_identity = (
                Identity(
                    "nfsdevserver",
                    "nfsdevserver",
                    self.server_huid,
                    self.server_huid,
                )
                if server_hosted
                else identity
            )
            session = self.sessions.create_game(
                257,
                host_identity.user_id,
                capacity=max_players,
                min_players=2,
                include_owner=not server_hosted,
            )
            properties = {
                "N": values.get("N", "NFSC_WH_63" if server_hosted else f"LOCALCarbon-{gid}"),
                "J": values.get("J", values.get("JOIN", "O")) or "O",
                "PW": values.get("PW", "0"),
                "PL": "PC",
                "V": "1.0",
                "TYPE": "G",
                "I": self.race_endpoint.host,
                "P": str(self.race_endpoint.port),
                "INT-IP": self.race_endpoint.host,
                "INT-PORT": str(self.race_endpoint.port),
                **dedicated,
            }
            if server_hosted:
                properties.pop("INT-IP", None)
                properties.pop("INT-PORT", None)

            # Preserve explicit Theater properties, but never allow a set-valued
            # PlayNow filter (for example 0|2) to overwrite resolved GDAT.
            for key, value in values.items():
                if key.startswith("B-") and "|" not in str(value):
                    properties[key] = str(value)
            properties.update(dedicated)
            for key in ("B-U-ranked", "B-ranked", "ranked", "RANKED", "QROptionsRankedMode"):
                if key in values:
                    properties[key] = str(values[key])
            if server_hosted and properties.get("B-U-game_type") == "2":
                # Keep Challenge identity authoritative even when a direct
                # caller supplies stale B-U fields after PlayNow resolution.
                # A concrete cs.* event is presentation state, not a request
                # to turn the room back into a normal Unranked allocation.
                properties.update(CHALLENGE_ROOM_IDENTITY_PROPERTIES)
                challenge_event = selected_challenge_event(properties, prefix="B-U-")
                if challenge_event is not None:
                    properties["B-U-game_mode"] = RACE_PROPERTY_GAME_MODE[
                        challenge_event[0]
                    ]

            participants: dict[int, CarbonParticipant] = {}
            if not server_hosted:
                participants[identity.user_id] = CarbonParticipant(
                    identity,
                    self._player_id(identity, set()),
                )
            coop_match_help_type = None
            if server_hosted and properties.get("B-U-game_type") == "2":
                requested_help = request.requested_help_type
                if requested_help in {"1", "2", "3"}:
                    coop_match_help_type = requested_help
            game = CarbonGame(
                gid=gid,
                ugid=self._ugid(gid),
                lobby_id="257",
                session=session,
                host=host_identity,
                properties=properties,
                participants=participants,
                server_hosted=server_hosted,
                allocator_user_id=identity.user_id if server_hosted else None,
                coop_match_help_type=coop_match_help_type,
                # The stock Unranked Quick Search includes game types 1|2 and
                # state 1.  A waiting Challenge requester publishes type 2,
                # state 0 and is matched through the co-op state bridge, so it
                # must remain discoverable until Ready closes the room.
                quick_join_locked=(
                    server_hosted
                    and properties.get("B-U-game_type") == "2"
                    and not self.challenge_quick_join_before_ready
                ),
                created_tick_ms=self._allocate_room_tick_ms(),
                descriptor_handle_base=self._allocate_descriptor_handle_base(
                    8
                    if server_hosted
                    and properties.get("B-U-game_type") == "2"
                    else max_players
                ),
                descriptor_slot_capacity=(
                    8
                    if server_hosted
                    and properties.get("B-U-game_type") == "2"
                    else max_players
                ),
            )
            self._games[gid] = game
            log.info(
                "Carbon directory created game: gid=%s kind=%s requester=%s host=%s host_user_id=%d "
                "game_type=%s state=%s help_type=%s mode=%s players=%d/%d name=%s",
                gid,
                "dedicated" if server_hosted else "client-hosted",
                identity.persona,
                host_identity.persona,
                host_identity.user_id,
                properties.get("B-U-game_type", "?"),
                properties.get("B-U-matchmaking_state", "?"),
                properties.get("B-U-help_type", "?"),
                properties.get("B-U-game_mode", "?"),
                len(participants),
                max_players,
                properties.get("N", ""),
            )
            return game

    def set_authoritative_capacity(
        self,
        gid: str,
        capacity: int,
        *,
        reason: str,
    ) -> int | None:
        """Apply a host-published capacity while preserving live members."""

        with self._lock:
            game = self._games.get(str(gid))
            if game is None:
                return None
            requested = int(capacity)
            applied = min(
                8,
                max(2, len(game.participants), requested),
            )
            previous = int(game.session.capacity)
            if not self.sessions.resize_game(game.session.game_id, applied):
                return None
            game.properties["B-U-max_online_player"] = str(applied)
            if previous != applied:
                log.info(
                    "Carbon directory authoritative capacity applied: "
                    "gid=%s requested=%d applied=%d previous=%d players=%d "
                    "reason=%s",
                    game.gid,
                    requested,
                    applied,
                    previous,
                    len(game.participants),
                    reason,
                )
            return applied

    def list(self, lobby_id: str = "257") -> list[CarbonGame]:
        with self._lock:
            return [game for game in self._games.values() if game.lobby_id == str(lobby_id)]

    def status_snapshot(self) -> list[dict[str, object]]:
        """Return a sanitized room snapshot for the read-only web status."""
        with self._lock:
            snapshot: list[dict[str, object]] = []
            for game in sorted(self._games.values(), key=lambda value: int(value.gid)):
                invite = game.invite_fields()
                personas = [
                    str(participant.identity.persona).strip()
                    for participant in game.participants.values()
                    if str(participant.identity.persona).strip()
                ]
                snapshot.append(
                    {
                        "id": str(game.gid),
                        "name": str(game.properties.get("N", "") or ""),
                        "host": (
                            "Dedicated Server"
                            if game.server_hosted
                            else str(game.host.persona or "")
                        ),
                        "server_hosted": bool(game.server_hosted),
                        "players": len(game.participants),
                        "capacity": int(game.session.capacity),
                        "personas": personas,
                        "game_mode": str(invite.get("game_mode", "") or ""),
                        "game_type": str(invite.get("game_type", "") or ""),
                        "track": str(invite.get("track", "") or ""),
                        "matchmaking_state": str(
                            game.properties.get("B-U-matchmaking_state", "") or ""
                        ),
                        "quick_join_locked": bool(game.quick_join_locked),
                        "challenge_ready": bool(game.challenge_ready),
                    }
                )
            return snapshot

    def get(self, gid: str) -> CarbonGame | None:
        with self._lock:
            return self._games.get(str(gid))

    def retire(self, gid: str, *, reason: str) -> bool:
        """Remove one room and its neutral session through one cleanup path."""
        with self._lock:
            game = self._games.pop(str(gid), None)
            if game is None:
                return False
            participant_count = len(game.participants)
            for participant in tuple(game.participants.values()):
                self.sessions.leave_game(
                    game.session.game_id,
                    participant.identity.user_id,
                )
            game.participants.clear()
            self.sessions.close_game(game.session.game_id)
            log.info(
                "Carbon directory retired game: gid=%s reason=%s players=%d "
                "kind=%s",
                game.gid,
                reason,
                participant_count,
                "dedicated" if game.server_hosted else "client-hosted",
            )
            return True

    def set_quick_join_locked(
        self,
        gid: str,
        locked: bool,
        *,
        reason: str,
    ) -> bool:
        """Publish the GameManager room-access gate to PlayNow matching."""
        with self._lock:
            game = self._games.get(str(gid))
            if game is None:
                return False
            desired = bool(locked)
            changed = game.quick_join_locked != desired
            game.quick_join_locked = desired
            if changed:
                log.info(
                    "Carbon directory Quick Join gate: gid=%s locked=%d reason=%s "
                    "game_type=%s state=%s players=%d/%d",
                    game.gid,
                    int(desired),
                    reason,
                    game.properties.get("B-U-game_type", "?"),
                    game.properties.get("B-U-matchmaking_state", "?"),
                    len(game.participants),
                    game.session.capacity,
                )
            return changed

    def set_challenge_ready(
        self,
        gid: str,
        ready: bool,
        *,
        reason: str,
    ) -> bool:
        """Apply the configured post-Ready policy to a Challenge room.

        Before Ready a deployment may expose a dedicated game_type=2 room to
        Unranked Quick Search through the asymmetric state-0/state-1 co-op
        PlayNow bridge. After Ready it may close that gate or deliberately keep
        it open; the two phases are configured independently.
        """
        with self._lock:
            game = self._games.get(str(gid))
            if (
                game is None
                or str(game.properties.get("B-U-game_type", "")) != "2"
            ):
                return False
            desired_ready = bool(ready)
            desired_locked = not (
                self.challenge_quick_join_after_ready
                if desired_ready
                else self.challenge_quick_join_before_ready
            )
            changed = (
                game.challenge_ready != desired_ready
                or game.quick_join_locked != desired_locked
            )
            game.challenge_ready = desired_ready
            game.quick_join_locked = desired_locked
            if changed:
                log.info(
                    "Carbon directory Challenge Quick Join phase: "
                    "gid=%s ready=%d after_ready_enabled=%d locked=%d "
                    "reason=%s players=%d/%d",
                    game.gid,
                    int(desired_ready),
                    int(self.challenge_quick_join_after_ready),
                    int(desired_locked),
                    reason,
                    len(game.participants),
                    game.session.capacity,
                )
            return changed

    def find_for_persona(self, persona: str) -> CarbonGame | None:
        wanted = str(persona or "").strip().casefold()
        if not wanted:
            return None
        with self._lock:
            for game in reversed(tuple(self._games.values())):
                if any(
                    participant.identity.persona.casefold() == wanted
                    for participant in game.participants.values()
                ):
                    return game
        return None

    def invite_fields_for_persona(self, persona: str) -> dict[str, str]:
        """Snapshot one participant's room for an outgoing Messenger invite."""

        game = self.find_for_persona(persona)
        return game.invite_fields() if game is not None else {}

    def messenger_snapshot(self) -> dict[str, dict[str, object]]:
        """Return current per-persona room state for an external Messenger."""
        snapshot: dict[str, dict[str, object]] = {}
        with self._lock:
            for game in self._games.values():
                details = game.invite_fields()
                for participant in game.participants.values():
                    identity = participant.identity
                    snapshot[identity.persona.casefold()] = {
                        "persona": identity.persona,
                        "session_id": game.gid,
                        "inviteable": True,
                        "invite_join_complete": (
                            identity.user_id in game.completed_invite_entries
                        ),
                        "details": details,
                    }
        return snapshot

    def mark_invite_entry_complete(self, gid: str, user_id: int) -> bool:
        """Record that an invited participant's Theater EGEG was written."""

        with self._lock:
            game = self._games.get(str(gid))
            if game is None:
                return False
            participant = game.participants.get(int(user_id))
            if participant is None:
                return False
            before = len(game.completed_invite_entries)
            game.completed_invite_entries.add(participant.identity.user_id)
            return len(game.completed_invite_entries) != before

    def resolve_play_now(
        self,
        identity: Identity,
        fields: dict[str, str],
    ) -> CarbonPlayNowResolution | None:
        request = parse_request(fields)

        if request.is_find:
            candidates: list[tuple[float, CarbonGame]] = []
            inspected = 0
            rejected_client_hosted = 0
            rejected_locked = 0
            rejected_full = 0
            rejected_member = 0
            rejected_allocator = 0
            rejected_strict = 0
            rejected_fit = 0
            with self._lock:
                for game in self._games.values():
                    inspected += 1
                    if not game.server_hosted:
                        rejected_client_hosted += 1
                        continue
                    if game.quick_join_locked:
                        rejected_locked += 1
                        continue
                    if len(game.participants) >= game.session.capacity:
                        rejected_full += 1
                        continue
                    if identity.user_id in game.participants:
                        rejected_member += 1
                        continue
                    if game.allocator_user_id == identity.user_id:
                        # A failed PlayNow attempt can leave an allocated room
                        # without an EGAM participant. Do not feed that stale
                        # room back to the same requester on the next probe.
                        rejected_allocator += 1
                        continue
                    match_properties = _match_properties(game)
                    coop_bridge = is_coop_state_bridge(request, match_properties)
                    if coop_bridge and not game.participants:
                        # Do not send a helper to an allocated co-op server
                        # before the Career/Challenge requester has completed
                        # EGAM and is actually present in the room.
                        rejected_strict += 1
                        continue
                    if not strict_match(request, match_properties):
                        rejected_strict += 1
                        continue
                    fit = match_fit(fields, match_properties)
                    if fit is None or fit < request.fit_threshold:
                        rejected_fit += 1
                        continue
                    candidates.append((fit, game))
            log.info(
                "Carbon PlayNow search: persona=%s inspected=%d accepted=%d rejected_client=%d "
                "rejected_locked=%d rejected_full=%d "
                "rejected_member=%d rejected_allocator=%d rejected_strict=%d rejected_fit=%d",
                identity.persona,
                inspected,
                len(candidates),
                rejected_client_hosted,
                rejected_locked,
                rejected_full,
                rejected_member,
                rejected_allocator,
                rejected_strict,
                rejected_fit,
            )
            if not candidates:
                return None
            # Highest fit wins; lower GID is a stable tie-breaker.
            fit, game = max(candidates, key=lambda item: (item[0], -int(item[1].gid)))
            log.info(
                "Carbon PlayNow selected existing game: persona=%s gid=%s fit=%.4f game_type=%s state=%s help_type=%s players=%d/%d coop_bridge=%d",
                identity.persona,
                game.gid,
                fit,
                game.properties.get("B-U-game_type", "?"),
                game.properties.get("B-U-matchmaking_state", "?"),
                game.properties.get("B-U-help_type", "?"),
                len(game.participants),
                game.session.capacity,
                int(is_coop_state_bridge(request, _match_properties(game))),
            )
            return CarbonPlayNowResolution(game, fit, False)

        if request.creates_dedicated:
            # Carbon has a second helper-side co-op flow which arrives as a
            # direct resetServer(type 0/1, state 0, ABSTAIN) rather than a
            # preceding findServer(0|2/1|2).  Pair that narrow signature with
            # an active waiting game_type=2 requester before allocating a new
            # dedicated room.
            if is_direct_coop_helper_reset(fields, request):
                candidates: list[CarbonGame] = []
                with self._lock:
                    for game in self._games.values():
                        if not game.server_hosted:
                            continue
                        if game.quick_join_locked:
                            continue
                        if game.properties.get("B-U-game_type") != "2":
                            continue
                        if game.properties.get("B-U-matchmaking_state") != "0":
                            continue
                        if game.coop_match_help_type not in {"1", "2", "3"}:
                            continue
                        if request.version and game.properties.get("B-U-version") != request.version:
                            continue
                        if game.allocator_user_id == identity.user_id:
                            continue
                        if identity.user_id in game.participants:
                            continue
                        if len(game.participants) != 1:
                            continue
                        if len(game.participants) >= game.session.capacity:
                            continue
                        candidates.append(game)
                if candidates:
                    # Oldest waiting requester wins, matching a FIFO dedicated
                    # pool and avoiding a random room when several assists wait.
                    game = min(candidates, key=lambda candidate: int(candidate.gid))
                    fit = match_fit(fields, _match_properties(game), reject_incompatible=False)
                    resolved_fit = 1.0 if fit is None else fit
                    log.info(
                        "Carbon PlayNow direct co-op reset bridge: persona=%s gid=%s fit=%.4f "
                        "request_type=%s request_state=%s room_help_type=%s players=%d/%d",
                        identity.persona,
                        game.gid,
                        resolved_fit,
                        request.concrete_game_type,
                        "|".join(sorted(request.matchmaking_states)) or "<any>",
                        game.coop_match_help_type or game.properties.get("B-U-help_type", "?"),
                        len(game.participants),
                        game.session.capacity,
                    )
                    return CarbonPlayNowResolution(game, resolved_fit, False)

            # Ordinary resetServer is an allocation operation. It must use one
            # concrete type and must not revive an unrelated old room.
            if request.concrete_game_type not in {"0", "1", "2"}:
                log.warning(
                    "Carbon PlayNow dedicated allocation rejected: persona=%s session_type=%s concrete_type=%s allowed_types=%s",
                    identity.persona,
                    request.session_type or "<missing>",
                    request.concrete_game_type or "<none>",
                    "|".join(sorted(request.allowed_game_types)) or "<any>",
                )
                return None
            game = self.create(
                identity,
                fields,
                server_hosted=True,
                play_now_request=request,
            )
            fit = match_fit(fields, game.properties, reject_incompatible=False)
            resolved_fit = 1.0 if fit is None else fit
            log.info(
                "Carbon PlayNow allocated dedicated game: persona=%s gid=%s fit=%.4f game_type=%s state=%s help_type=%s",
                identity.persona,
                game.gid,
                resolved_fit,
                game.properties.get("B-U-game_type", "?"),
                game.properties.get("B-U-matchmaking_state", "?"),
                game.properties.get("B-U-help_type", "?"),
            )
            return CarbonPlayNowResolution(game, resolved_fit, True)

        if request.session_type == "create":
            game = self.create(identity, fields)
            return CarbonPlayNowResolution(game, 1.0, True)
        log.warning(
            "Carbon PlayNow ignored request: persona=%s session_type=%s allowed_types=%s concrete_type=%s",
            identity.persona,
            request.session_type or "<missing>",
            "|".join(sorted(request.allowed_game_types)) or "<any>",
            request.concrete_game_type or "<none>",
        )
        return None

    def match_or_create(self, identity: Identity, fields: dict[str, str]) -> CarbonGame | None:
        """Compatibility wrapper for callers which need only the selected game."""
        resolution = self.resolve_play_now(identity, fields)
        return resolution.game if resolution is not None else None

    def enter(
        self,
        gid: str,
        identity: Identity,
        *,
        internal_ip: str = "0.0.0.0",
        internal_port: int = 0,
        invite_remote_player_id: int = 0,
        invite_entry: bool = False,
    ) -> CarbonParticipant | None:
        with self._lock:
            game = self._games.get(str(gid))
            if game is None:
                log.warning(
                    "Carbon directory enter rejected: persona=%s user_id=%d gid=%s reason=GAME_NOT_FOUND",
                    identity.persona,
                    identity.user_id,
                    gid,
                )
                return None

            # Moving to a newly selected room implicitly retires the player's
            # membership in older rooms.  ECNL is not guaranteed when Carbon
            # abandons a PlayNow attempt, so relying only on the explicit leave
            # packet leaves self-owned dedicated rows matchable forever.
            for previous in tuple(self._games.values()):
                if previous.gid == game.gid or identity.user_id not in previous.participants:
                    continue
                stale = previous.participants.pop(identity.user_id)
                self.sessions.leave_game(previous.session.game_id, stale.identity.user_id)
                if not previous.participants:
                    self._games.pop(previous.gid, None)

            existing = game.participants.get(identity.user_id)
            if existing is not None:
                # CGAM creates the host before Theater has received EGAM's
                # R-INT-IP/R-INT-PORT. Preserve the assigned PID but refresh
                # the endpoint here; otherwise the GM roster falls back to
                # the rebroadcaster-facing address and advertises the wrong
                # local endpoint to the retail client.
                refreshed = CarbonParticipant(
                    existing.identity,
                    existing.player_id,
                    str(internal_ip),
                    int(internal_port),
                    int(invite_remote_player_id or existing.invite_remote_player_id),
                    existing.entered_at,
                )
                game.participants[identity.user_id] = refreshed
                return refreshed
            if (
                game.quick_join_locked
                and game.allocator_user_id != identity.user_id
                and not bool(invite_entry)
            ):
                log.warning(
                    "Carbon directory enter rejected: persona=%s user_id=%d gid=%s "
                    "reason=QUICK_JOIN_LOCKED invite=%d",
                    identity.persona,
                    identity.user_id,
                    game.gid,
                    int(bool(invite_entry)),
                )
                return None
            if not self.sessions.join_game(game.session.game_id, identity.user_id):
                log.warning(
                    "Carbon directory enter rejected: persona=%s user_id=%d gid=%s reason=SESSION_FULL players=%d/%d",
                    identity.persona,
                    identity.user_id,
                    game.gid,
                    len(game.participants),
                    game.session.capacity,
                )
                return None
            used_ids = {participant.player_id for participant in game.participants.values()}
            player_id = self._player_id(identity, used_ids)
            participant = CarbonParticipant(
                identity,
                player_id,
                str(internal_ip),
                int(internal_port),
                int(invite_remote_player_id),
            )
            game.participants[identity.user_id] = participant
            log.info(
                "Carbon directory participant entered: persona=%s user_id=%d gid=%s pid=%d internal=%s:%d players=%d/%d",
                identity.persona,
                identity.user_id,
                game.gid,
                player_id,
                internal_ip,
                internal_port,
                len(game.participants),
                game.session.capacity,
            )
            return participant

    def ticket(self, game: CarbonGame, participant: CarbonParticipant) -> str:
        seed = f"carbon-ticket:{game.gid}:{participant.player_id}:{participant.identity.user_id}"
        raw = int.from_bytes(md5(seed.encode("ascii")).digest()[:4], "big")
        return str(1_000_000_000 + (raw % 9_000_000_000))

    def resolve_ticket(self, ticket: str) -> CarbonTicketResolution | None:
        """Resolve only tickets currently belonging to an entered participant."""
        candidate = str(ticket)
        with self._lock:
            for game in self._games.values():
                for participant in game.participants.values():
                    if self.ticket(game, participant) == candidate:
                        return CarbonTicketResolution(game, participant)
        return None

    def leave(
        self,
        gid: str,
        user_id: int,
        *,
        reason: str = "unspecified",
    ) -> bool:
        """Remove a participant and retire a coordinator-owned Carbon game."""
        with self._lock:
            game = self._games.get(str(gid))
            if game is None or int(user_id) not in game.participants:
                log.warning(
                    "Carbon directory leave ignored: gid=%s user_id=%d reason=%s",
                    gid,
                    int(user_id),
                    "GAME_NOT_FOUND" if game is None else "NOT_IN_GAME",
                )
                return False
            participant = game.participants.pop(int(user_id))
            game.completed_invite_entries.discard(participant.identity.user_id)
            self.sessions.leave_game(game.session.game_id, participant.identity.user_id)
            # A dedicated room's advertised host is the server identity, but
            # its lifecycle is owned by the client that caused PlayNow to
            # allocate it.  Treating only game.host as the owner leaves the
            # old room and every guest UDP confirmation window alive when
            # that allocator exits and immediately creates another room.
            coordinator_user_id = (
                game.allocator_user_id
                if game.server_hosted and game.allocator_user_id is not None
                else game.host.user_id
            )
            coordinator_left = (
                participant.identity.user_id == int(coordinator_user_id)
            )
            retired = coordinator_left or not game.participants
            if retired:
                for remaining in tuple(game.participants.values()):
                    self.sessions.leave_game(game.session.game_id, remaining.identity.user_id)
                self._games.pop(game.gid, None)
            if coordinator_left:
                log.info(
                    "Carbon directory host exited: gid=%s persona=%s "
                    "user_id=%d reason=%s remaining=%d retired=1",
                    game.gid,
                    participant.identity.persona,
                    participant.identity.user_id,
                    str(reason),
                    len(game.participants),
                )
            log.info(
                "Carbon directory participant left: gid=%s persona=%s "
                "user_id=%d role=%s reason=%s retired=%s remaining=%d",
                game.gid,
                participant.identity.persona,
                participant.identity.user_id,
                "host" if coordinator_left else "guest",
                str(reason),
                int(retired),
                len(game.participants),
            )
            return True
