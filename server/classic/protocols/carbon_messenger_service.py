"""Carbon EA Messenger presence, direct delivery and retail-compatible liveness.

The retail client opens this TCP service after FESL Hello/Login.  The server is
responsible for sending periodic ``PING`` frames; the client answers with its
own ``PING``.  Keeping this channel absent or silent can make the frontend
consider the online session lost even while FESL/Theater sockets remain open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import sqlite3
from threading import Lock, RLock
from typing import Callable, Mapping

from common.public_presence import (
    clear_carbon_public_presence,
    set_carbon_public_presence,
    website_appear_offline,
)
from classic.ea.messenger import EAMessengerFrame as FESLFrame
from classic.ea.social import Presence, SocialRow, SocialService, canonical_persona
from classic.protocols.carbon_messenger_ipc import (
    CarbonIPCForcedLogoff,
    CarbonIPCIdentity as Identity,
    CarbonMessengerIPCState as IdentityStore,
)


CARBON_TITLE = "Need for Speed Carbon"
CARBON_RESOURCE = "eagames/NFS-2007"
# NFSC.exe 0x939810, FESL SDK 2.9.0.0 messengerservice.cpp. Registration
# includes both request verbs and receive-only messages; it is not a router.
RETAIL_ASYNC_COMMANDS = frozenset("USER PGET RNOT ROST GNOT RECV ADMN".split())
RETAIL_REQUEST_HANDLERS = {
    "AUTH": "_dispatch_auth", "DISC": "_dispatch_disconnect",
    "USCH": "_dispatch_user_search", "PADD": "_dispatch_presence_add",
    "PDEL": "_dispatch_presence_delete", "PSET": "_dispatch_presence_set",
    "RADD": "_dispatch_roster_add", "RADM": "_dispatch_roster_add",
    "RRSP": "_dispatch_roster_response", "RDEL": "_dispatch_roster_remove",
    "RDEM": "_dispatch_roster_remove", "RGET": "_dispatch_roster_get",
    "MLST": "_dispatch_compatibility", "TCKL": "_dispatch_compatibility",
    "GINV": "_dispatch_game_invite", "GRSP": "_dispatch_game_response",
    "GRVK": "_dispatch_game_revoke", "SEND": "_dispatch_send",
    "BRDC": "_dispatch_compatibility", "EPST": "_dispatch_endpoint_set",
    "EPGT": "_dispatch_endpoint_get", "PING": "_dispatch_ping",
}
# Preserve deployed aliases without claiming they belong to the retail registry.
_LEGACY_REQUEST_HANDLERS = {
    "RSET": "_dispatch_roster_add", "RREM": "_dispatch_roster_remove",
    **dict.fromkeys(("BLCK", "BLOK", "RBLK", "RBLO"), "_dispatch_block"),
    **dict.fromkeys(("UBLK", "UBLO", "UNBL", "BDEL"), "_dispatch_unblock"),
}
_PRESENCE_STATES = frozenset("CHAT AWAY XA DND DISC GAME PASS".split())
_PRESENCE_FIELDS = frozenset(
    "RSRC DOMN RICH ATTR EXTR SESS PROD STAT CHNG GROUP UID GSTR TYPE HOST ERRS NOREPLY".split()
)
_MESSAGE_FIELDS = frozenset(
    "TYPE SUBJ BODY SECS RSRC DOMN RICH ATTR EXTR SESS PROD STAT CHNG GROUP UID GSTR ERRS NOREPLY".split()
)
_INVITE_GAME_TYPE_LABELS = {
    "0": "Ranked",
    "1": "Unranked",
    "2": "Career Challenge",
}
_INVITE_GAME_MODE_LABELS = {
    "0": "Sprint",
    "1": "Circuit",
    "5": "Speedtrap",
    "13": "Canyon Duel",
    "14": "Pursuit Tag",
    "15": "Knockout",
}
# The captured Silver Challenge publishes car_tier=2.  Unknown values are
# deliberately left unlabeled instead of guessing at a client-local mapping.
_INVITE_CHALLENGE_TIER_LABELS = {"2": "Silver"}
log = logging.getLogger(__name__)


@dataclass(eq=False)
class MessengerConnection:
    identity: Identity | None = None
    connection_id: str = ""
    client_ip: str = "127.0.0.1"
    session_token: str = ""
    authenticated: bool = False
    close_requested: bool = False
    forced_logoff_reason: str = ""
    forced_logoff_notice_sent: bool = False
    ping_responses: int = 0
    show: str = "CHAT"
    presence_ready: bool = False
    status: str = "en%3dPlaying Need for Speed Carbon"
    presence_attr: str = ""
    presence_fields: dict[str, str] = field(default_factory=dict)
    subscriptions: set[str] = field(default_factory=set)
    subscription_presence: dict[str, SocialRow | None] = field(default_factory=dict)
    suppressed_presence: set[str] = field(default_factory=set)
    endpoint_enabled: bool = False
    endpoint_address: str = ""
    pending: list[FESLFrame] = field(default_factory=list, repr=False)
    pending_lock: Lock = field(default_factory=Lock, repr=False)
    after_send_callbacks: list[Callable[[], None]] = field(default_factory=list, repr=False)
    sender: Callable[[FESLFrame], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def enqueue(self, frame: FESLFrame) -> None:
        with self.pending_lock:
            self.pending.append(frame)

    def drain(self) -> list[FESLFrame]:
        with self.pending_lock:
            frames = list(self.pending)
            self.pending.clear()
        return frames

    def defer_after_send(self, callback: Callable[[], None]) -> None:
        """Run a cross-channel action only after this connection's reply is sent."""
        with self.pending_lock:
            self.after_send_callbacks.append(callback)

    def run_after_send(self) -> None:
        with self.pending_lock:
            callbacks = tuple(self.after_send_callbacks)
            self.after_send_callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                log.exception("Carbon Messenger deferred after-send action failed")

    def deliver(self, frame: FESLFrame) -> bool:
        """Write an urgent cross-channel push, or queue it in unit contexts."""

        sender = self.sender
        if sender is None:
            self.enqueue(frame)
            return True
        return bool(sender(frame))


class CarbonMessengerService:
    """Retail request handlers backed by the existing connection/social registry."""

    def __init__(
        self,
        identities: IdentityStore,
        *,
        is_inviteable: Callable[[Identity], bool] | None = None,
        invite_details: Callable[[Identity], dict[str, str]] | None = None,
        known_identities: Callable[[], tuple[Identity, ...]] | None = None,
        social: SocialService | None = None,
        identity_resolver: Callable[[str], Identity | None] | None = None,
    ) -> None:
        self.identities = identities
        self.is_inviteable = is_inviteable or (lambda _identity: False)
        self.invite_details = invite_details or (lambda _identity: {})
        self.known_identities = known_identities or (lambda: ())
        self.social = social
        self.identity_resolver = identity_resolver
        self._lock = RLock()
        self._connections: dict[str, set[MessengerConnection]] = {}
        self._pending_invite_completions: dict[str, tuple[str, str]] = {}
        self._pending_invite_revokes: set[str] = set()

    @staticmethod
    def _persona(value: str) -> str:
        text = str(value or "").strip()
        if "@" in text:
            text = text.split("@", 1)[0]
        if "/" in text:
            text = text.split("/", 1)[0]
        return text

    @staticmethod
    def _invite_game_string(details: Mapping[str, str]) -> str:
        """Build the optional retail-supported GNOT ``GSTR`` extension.

        NFSC stores GSTR in a 256-byte buffer.  Keep the extension ASCII,
        single-line and below the terminating NUL rather than allowing room
        metadata to alter the Messenger frame shape.
        """

        game_type = str(details.get("game_type", "") or "").strip()
        game_mode = str(details.get("game_mode", "") or "").strip()
        car_tier = str(details.get("car_tier", "") or "").strip()
        track = str(details.get("track", "") or "").strip()

        parts = [_INVITE_GAME_TYPE_LABELS.get(game_type, "Online Race")]
        if game_type == "2":
            tier = _INVITE_CHALLENGE_TIER_LABELS.get(car_tier)
            if tier:
                parts.append(tier)
        mode = _INVITE_GAME_MODE_LABELS.get(game_mode)
        if mode:
            parts.append(mode)
        if track and track.upper() != "ABSTAIN":
            parts.append(track)

        text = " - ".join(parts)
        text = text.replace("\r", " ").replace("\n", " ").replace("\x00", " ")
        text = text.encode("ascii", "replace")[:255].decode("ascii")
        return " ".join(text.split())

    def _resolve_identity(self, persona: object) -> Identity | None:
        display = canonical_persona(persona)
        if not display:
            return None
        if self.identity_resolver is not None:
            identity = self.identity_resolver(display)
            if identity is not None:
                return identity
        resolver = getattr(self.identities, "identity_for_persona", None)
        if callable(resolver):
            identity = resolver(display)
            if identity is not None:
                return identity
        for identity in self.known_identities():
            if identity.persona.casefold() == display.casefold():
                return identity
        return None

    @staticmethod
    def _search_limit(fields: Mapping[str, str]) -> int:
        try:
            return max(1, min(100, int(fields.get("MAXR", "5") or "5")))
        except (TypeError, ValueError):
            return 5

    @staticmethod
    def _search_matches(persona: str, query: str) -> bool:
        display = canonical_persona(persona)
        wanted = canonical_persona(query)
        if not display or not wanted:
            return False
        # Match Classic's case-insensitive substring search. Preserve the
        # existing optional asterisk syntax for Carbon clients.
        needle = wanted.replace("*", "").casefold()
        return not needle or needle in display.casefold()

    def _search_personas(
        self,
        connection: MessengerConnection,
        fields: Mapping[str, str],
    ) -> tuple[str, ...]:
        if connection.identity is None:
            return ()
        query = fields.get("USER", "")
        if not canonical_persona(query):
            return ()
        limit = self._search_limit(fields)
        owner = connection.identity.persona
        selected: dict[str, str] = {}

        if self.social is not None:
            # The shared directory is authoritative for persisted personas.
            # Use the literal part as an index hint, then enforce Carbon's
            # substring semantics and privacy rules locally.
            hint = canonical_persona(query).replace("*", "")
            candidates = self.social.search(owner, hint, 100)
            for row in candidates:
                if self.social.is_blocked(owner, row.user):
                    continue
                if self._search_matches(row.user, query):
                    selected.setdefault(row.user.casefold(), row.user)
        else:
            for identity in self.known_identities():
                if identity.persona.casefold() == owner.casefold():
                    continue
                if self._search_matches(identity.persona, query):
                    selected.setdefault(identity.persona.casefold(), identity.persona)

        return tuple(selected[key] for key in sorted(selected))[:limit]

    def _search_replies(
        self,
        connection: MessengerConnection,
        fields: Mapping[str, str],
        request_id: str,
    ) -> list[FESLFrame]:
        personas = self._search_personas(connection, fields)
        replies = [self._reply("USCH", {"ID": request_id, "SIZE": str(len(personas))})]
        replies.extend(
            self._reply(
                "USER",
                {"ID": request_id, "RSRC": CARBON_RESOURCE, "USER": persona},
            )
            for persona in personas
        )
        log.info(
            "Carbon Messenger user search: persona=%s query=%s max=%d results=%s",
            connection.identity.persona if connection.identity is not None else "<unauthenticated>",
            canonical_persona(fields.get("USER", "")) or "<empty>",
            self._search_limit(fields),
            ",".join(personas) or "none",
        )
        return replies

    def _appears_offline(self, persona: str) -> bool:
        # DISC is the retail client's Appear Offline setting.  Hide a new
        # connection until its first PSET, then project only its buddy
        # presence; the real lobby session remains available for gameplay.
        if any((not peer.presence_ready or peer.show == "DISC") and self._available(peer)
               for peer in self._targets(persona)):
            return True
        database = self.social.database if self.social is not None else None
        if database is None:
            return False
        try:
            return website_appear_offline(database, persona)
        except (OSError, sqlite3.Error):
            # A temporary read failure must not expose a private account.
            log.exception("Could not read website visibility for %s", persona)
            return True

    def _record_public_presence(self, connection: MessengerConnection, show: str) -> None:
        database = self.social.database if self.social is not None else None
        if database is None or connection.identity is None:
            return
        try:
            set_carbon_public_presence(
                database, connection.identity.persona, connection.connection_id, show,
            )
        except (OSError, sqlite3.Error):
            log.exception("Could not update Carbon public presence for %s", connection.identity.persona)

    def _clear_public_presence(self, connection: MessengerConnection) -> None:
        database = self.social.database if self.social is not None else None
        if database is None or connection.identity is None:
            return
        try:
            clear_carbon_public_presence(
                database, connection.identity.persona, connection.connection_id,
            )
        except (OSError, sqlite3.Error):
            log.exception("Could not clear Carbon public presence for %s", connection.identity.persona)

    def _social_presence_fields(self, row: SocialRow) -> tuple[tuple[str, str], ...]:
        presence = row.presence or Presence()
        hidden = row.online and self._appears_offline(row.user)
        show = "DISC" if hidden else (presence.show if row.online else "AWAY")
        fields: list[tuple[str, str]] = [
            ("STAT", "" if hidden else presence.stat),
            ("PROD", "" if hidden else presence.product),
            ("TITL", "" if hidden else presence.title),
            ("SHOW", show or ("CHAT" if row.online else "AWAY")),
            ("USER", row.user),
        ]
        attr = "" if hidden else row.attr or presence.attr
        if attr:
            fields.append(("ATTR", attr))
        return tuple(fields)

    def _presence_from_social_row(
        self,
        identity: Identity,
        row: SocialRow,
        *,
        subscription_id: str | None = None,
        extra_fields: Mapping[str, str] | None = None,
    ) -> FESLFrame:
        presence = row.presence or Presence()
        hidden = row.online and self._appears_offline(identity.persona)
        show = "DISC" if hidden else (presence.show if row.online else "AWAY")
        status = "" if hidden else (presence.stat or "en%3dOnline")
        title = presence.title or CARBON_TITLE
        fields: dict[str, object] = {
            "STAT": f'"{status.strip(chr(34))}"',
            "TIID": "0",
            "TITL": f'"{title.strip(chr(34))}"',
        }
        if subscription_id is not None:
            fields["ID"] = subscription_id
        fields.update(
            {
                "SHOW": show or ("CHAT" if row.online else "AWAY"),
                "CHNG": "1",
                "USER": f"{identity.persona}@messaging.ea.com/{CARBON_RESOURCE}",
            }
        )
        attr = "" if hidden else row.attr or presence.attr
        if attr:
            fields["ATTR"] = attr
        if extra_fields is None and not hidden:
            with self._lock:
                extra_fields = next((dict(peer.presence_fields) for peer in self._targets(identity.persona)
                                     if self._available(peer)), {})
        if not hidden:
            fields.update({key: value for key, value in (extra_fields or {}).items() if key in _PRESENCE_FIELDS})
        return self._reply("PGET", fields)

    @staticmethod
    def _carbon_roster_attr(row: SocialRow) -> str:
        """Translate generic social state into Carbon's roster ATTR values.

        The shared graph uses ``B`` for an offline friend, while retail Carbon
        keeps persisted buddies as ``AT`` whether they are online or offline.
        """
        if row.friend:
            return "AT"
        if row.blocked:
            return "B"
        if row.request == "incoming":
            return "R"
        if row.request == "outgoing":
            return "P"
        return row.attr or "AT"

    def _social_sender(self, connection: MessengerConnection):
        def send(verb: str, fields: tuple[tuple[str, str], ...]) -> bool:
            values = {str(key): str(value) for key, value in fields}
            target = self._persona(values.get("USER", ""))
            if not self._available(connection):
                return False
            command = str(verb or "").upper()
            if command in {"RECV", "PGET"} and self._blocked(connection.identity.persona, target):
                return False
            if command == "RECV":
                # Shared delivery supplies the authenticated sender in USER.
                # Do not require an online Carbon IPC identity for a web/MW peer.
                message = {key: value for key, value in values.items() if key in _MESSAGE_FIELDS}
                message.update(USER=values.get("USER", target), TYPE=values.get("TYPE", "C"),
                               SUBJ=values.get("SUBJ", ""), BODY=values.get("BODY", values.get("T", "")),
                               SECS=values.get("SECS", "0"))
                return connection.deliver(self._push("RECV", message))
            with self._lock:
                if command == "PGET" and target.casefold() in connection.suppressed_presence:
                    return False
            identity = self._resolve_identity(target)
            if identity is None:
                return False
            roster_attr = values.get("ATTR", "AT") or "AT"
            if roster_attr == "B" and self.social is not None and connection.identity is not None:
                relation = self.social.presence_row(connection.identity.persona, target)
                if relation is not None and relation.friend and not relation.blocked:
                    roster_attr = "AT"
            if command == "PGET":
                presence = Presence(
                    show=values.get("SHOW", "CHAT"),
                    stat=values.get("STAT", ""),
                    product=values.get("PROD", ""),
                    title=values.get("TITL", ""),
                    attr=values.get("ATTR", ""),
                )
                row = SocialRow(
                    user=identity.persona,
                    online=presence.show.upper() != "AWAY",
                    friend=True,
                    attr=values.get("ATTR", "AT"),
                    presence=presence,
                )
                delivered = connection.deliver(self._presence_from_social_row(identity, row, extra_fields=values))
                if delivered and self.social is not None:
                    current = self.social.presence_row(connection.identity.persona, target)
                    with self._lock:
                        if target.casefold() in connection.subscriptions:
                            connection.subscription_presence[target.casefold()] = current
                return delivered
            if command == "ROST":
                return connection.deliver(
                    self._roster_frame(
                        identity,
                        values.get("ID", "-1"),
                        attr=roster_attr,
                    )
                )
            if command == "RNOT":
                return connection.deliver(
                    self._roster_change_frame(
                        identity,
                        values.get("CHNG", "A"),
                        attr=roster_attr,
                    )
                )
            return False

        return send

    def _notify_social_presence(self, persona: str, extra_fields: Mapping[str, str] | None = None) -> None:
        if self.social is None:
            return
        viewers = {relation.user for relation in self.social.snapshot(persona, "B")}
        with self._lock:
            connections = tuple(item for group in self._connections.values() for item in group)
            subscribers = [item for item in connections
                           if item.identity is not None and persona.casefold() in item.subscriptions]
            sources = self._connections.get(persona.casefold(), ())
            extra = (dict(extra_fields) if extra_fields is not None else
                     next((dict(item.presence_fields) for item in sources if item.authenticated), {}))
        for viewer in {item.identity.persona for item in connections if item.identity is not None}:
            for row in self.social.recent_player_snapshot(viewer, "carbon"):
                if row.user.casefold() == persona.casefold():
                    viewers.add(viewer)
        for viewer in viewers:
            if self._blocked(viewer, persona):
                continue
            row = self.social.presence_row(viewer, persona)
            if row is not None:
                values = dict(self._social_presence_fields(row))
                if not self._appears_offline(persona):
                    values.update(extra)
                self.social.deliver(viewer, "PGET", tuple(values.items()))
        implicit = {viewer.casefold() for viewer in viewers}
        for subscriber in subscribers:
            # Explicit non-friend subscriptions belong to this connection only.
            # Do not fan them out to other resources of the same persona.
            viewer = subscriber.identity.persona
            if viewer.casefold() in implicit or self._blocked(viewer, persona):
                continue
            row = self.social.presence_row(viewer, persona)
            if row is not None:
                values = dict(self._social_presence_fields(row))
                if not self._appears_offline(persona):
                    values.update(extra)
                self._social_sender(subscriber)("PGET", tuple(values.items()))

    def _blocked(self, source: str, target: str) -> bool:
        return self.social is not None and (
            self.social.is_blocked(source, target) or self.social.is_blocked(target, source)
        )

    def _available(self, connection: MessengerConnection) -> bool:
        return bool(connection.identity is not None and connection.authenticated
                    and not connection.close_requested and not connection.forced_logoff_reason
                    and not connection.forced_logoff_notice_sent
                    and self.identities.forced_logoff(connection.session_token) is None)

    def sync_session(self, connection: MessengerConnection) -> None:
        if connection.forced_logoff_reason:
            # A duplicate newcomer first receives a normal AUTH reply and the
            # read-only RGET/EPGT bootstrap.  The adapter queues ADMN/DUPL
            # after PSET proves the parallel Theater bootstrap also completed.
            return
        forced_logoff = self.identities.forced_logoff(connection.session_token)
        if forced_logoff is not None:
            if not connection.forced_logoff_notice_sent:
                connection.enqueue(self._forced_logoff_frame(forced_logoff.reason))
                connection.forced_logoff_notice_sent = True
                log.warning(
                    "Carbon Messenger forced logoff sent: persona=%s type=%s "
                    "action=client-native-error",
                    forced_logoff.identity.persona,
                    forced_logoff.reason,
                )
            return
        if connection.identity is None:
            return
        self._release_invite_completion_if_ready(connection.identity.persona)
        if self.social is None or not connection.connection_id:
            return
        session_id = self.identities.session_id_for_persona(connection.identity.persona)
        self.social.set_game_session(
            connection.connection_id,
            connection.identity.persona,
            "carbon",
            session_id,
        )
        # Carbon publishes changes immediately above. Other shared dialects
        # publish to their own friends; poll explicit non-friend subscriptions
        # through the existing adapter tick without changing SocialService.
        with self._lock:
            watched = [(target, connection.subscription_presence.get(target))
                       for target in connection.subscriptions if target not in self._connections]
        for target, previous in watched:
            row = self.social.presence_row(connection.identity.persona, target)
            if row is not None and row != previous:
                self._social_sender(connection)("PGET", self._social_presence_fields(row))

    def begin_forced_logoff(
        self,
        connection: MessengerConnection,
        token: str,
        forced_logoff: CarbonIPCForcedLogoff,
        request_id: str,
    ) -> FESLFrame:
        """Authenticate a rejected newcomer before its native admin notice."""

        connection.identity = forced_logoff.identity
        connection.session_token = str(token or "")
        connection.authenticated = True
        connection.forced_logoff_reason = str(forced_logoff.reason or "DUPL").upper()
        connection.forced_logoff_notice_sent = False
        log.warning(
            "Carbon Messenger duplicate AUTH prelude sent: persona=%s type=%s "
            "action=wait-for-communicator-bootstrap",
            forced_logoff.identity.persona,
            connection.forced_logoff_reason,
        )
        return self._auth_success_frame(forced_logoff.identity, request_id)

    def finish_forced_logoff(self, connection: MessengerConnection) -> FESLFrame:
        """Build the retail async notice after AUTH has become client-visible."""

        reason = str(connection.forced_logoff_reason or "DUPL").upper()
        connection.forced_logoff_notice_sent = True
        log.warning(
            "Carbon Messenger forced logoff sent: persona=%s type=%s "
            "action=client-native-error",
            connection.identity.persona if connection.identity is not None else "<unknown>",
            reason,
        )
        return self._forced_logoff_frame(reason)

    def _register(self, connection: MessengerConnection) -> None:
        assert connection.identity is not None
        key = connection.identity.persona.casefold()
        with self._lock:
            peers = [
                item
                for persona, connections in self._connections.items()
                if persona != key
                for item in connections
                if item.identity is not None
            ]
            self._connections.setdefault(key, set()).add(connection)
        if self.social is not None:
            connection_id = connection.connection_id or f"carbon-messenger:{id(connection):x}"
            connection.connection_id = connection_id
            self._record_public_presence(connection, "PENDING")
            self.social.register_lobby(
                connection_id,
                connection.identity.account_name,
                connection.identity.persona,
                connection.client_ip or "127.0.0.1",
                game_id="carbon",
                session_token=connection.session_token,
            )
            self.social.register_control(
                connection_id,
                connection.client_ip or "127.0.0.1",
                connection.identity.persona,
                self._social_sender(connection),
                game_id="carbon",
            )
            self.social.set_presence(
                connection.identity.persona,
                show=connection.show,
                stat=connection.status,
                product="NFS-CONSOLE-2007",
                title=CARBON_TITLE,
                attr=connection.presence_attr,
            )
            self.sync_session(connection)
            # Retail sends PSET after AUTH/RGET/EPGT.  Wait for that setting
            # before announcing online presence to buddies.
            log.info(
                "Carbon Messenger registered in shared social graph: persona=%s friends=%d",
                connection.identity.persona,
                len(self.social.snapshot(connection.identity.persona, "B")),
            )
            return
        # Without a durable SocialService there is no authoritative buddy
        # relation.  Do not reinterpret every known or online account as a
        # persisted friend; standalone mode therefore exposes an empty roster.
        log.info(
            "Carbon Messenger registered without social graph: persona=%s",
            connection.identity.persona,
        )

    def disconnect(self, connection: MessengerConnection) -> None:
        if connection.forced_logoff_reason:
            # The rejected newcomer was deliberately never registered.  Its
            # persona matches the winner, so normal disconnect cleanup would
            # otherwise erase the original client's invite/social state.
            return
        identity = connection.identity
        if identity is None:
            return
        key = identity.persona.casefold()
        with self._lock:
            connection.authenticated = False
            connection.subscriptions.clear()
            connection.subscription_presence.clear()
            connection.suppressed_presence.clear()
            connection.endpoint_enabled = False
            connection.endpoint_address = ""
            connections = self._connections.get(key)
            if connections is not None:
                connections.discard(connection)
                if not connections:
                    self._connections.pop(key, None)
            peers = [
                item
                for persona, active in self._connections.items()
                if persona != key
                for item in active
                if item.identity is not None
            ]
            self._pending_invite_completions.pop(key, None)
            self._pending_invite_revokes.discard(key)
            for guest_key, pending in tuple(self._pending_invite_completions.items()):
                if pending[0].casefold() == key:
                    self._pending_invite_completions.pop(guest_key, None)
                    self._pending_invite_revokes.discard(guest_key)
        if self.social is not None:
            self._clear_public_presence(connection)
            self.social.unregister_control(connection.connection_id)
            self.social.unregister_lobby(connection.connection_id)
            self._notify_social_presence(identity.persona)
            return
        # No roster mutations exist without an authoritative social graph.
        self._notify_subscribers(connection, offline=not self._targets(identity.persona))

    def _notify_subscribers(self, source: MessengerConnection, *, offline: bool = False) -> None:
        if source.identity is None:
            return
        key = source.identity.persona.casefold()
        with self._lock:
            frame = self._presence_frame(source)
            if offline:
                values = frame.fields
                values["SHOW"] = "AWAY"
                frame = self._reply("PGET", values)
            targets = [peer for group in self._connections.values() for peer in group
                       if key in peer.subscriptions and key not in peer.suppressed_presence]
        for peer in targets:
            if self._available(peer):
                peer.deliver(frame)

    def _queue_invite_revoke(self, guest: str, host: str, session: str) -> int:
        notification = self._push(
            "GNOT",
            {
                "HOST": host,
                "USER": host,
                "TYPE": "R",
                "SESS": session,
            },
        )
        targets = self._targets(guest)
        return sum(1 for peer in targets if peer.deliver(notification))

    def _release_invite_completion_if_ready(self, guest: str) -> int:
        """Release GNOT R only after Theater confirms the invited EGEG."""

        guest_name = self._persona(guest)
        guest_key = guest_name.casefold()
        with self._lock:
            pending = self._pending_invite_completions.get(guest_key)
            revoke_requested = guest_key in self._pending_invite_revokes
        if pending is None or not revoke_requested:
            return 0
        host, session = pending
        guest_gid = self.identities.session_id_for_persona(guest_name)
        host_gid = self.identities.session_id_for_persona(host)
        if not guest_gid or guest_gid != host_gid:
            return 0
        if not self.identities.invite_join_complete(guest_name, guest_gid):
            return 0
        with self._lock:
            current = self._pending_invite_completions.get(guest_key)
            if current != pending or guest_key not in self._pending_invite_revokes:
                return 0
            self._pending_invite_completions.pop(guest_key, None)
            self._pending_invite_revokes.discard(guest_key)
        delivered = self._queue_invite_revoke(guest_name, host, session)
        log.info(
            "Carbon Messenger invite completion released: guest=%s host=%s "
            "gid=%s delivered=%d barrier=theater-egeg",
            guest_name,
            host,
            guest_gid,
            delivered,
        )
        return delivered

    def _targets(self, persona: str) -> list[MessengerConnection]:
        with self._lock:
            return list(self._connections.get(self._persona(persona).casefold(), set()))

    def _online_peers(self, connection: MessengerConnection) -> list[MessengerConnection]:
        own = connection.identity.persona.casefold() if connection.identity is not None else ""
        with self._lock:
            return [
                item
                for persona, connections in sorted(self._connections.items())
                if persona != own
                for item in connections
                if item.identity is not None
            ]

    def _buddy_snapshot(
        self,
        connection: MessengerConnection,
    ) -> list[tuple[Identity, MessengerConnection | None]]:
        del connection
        return []

    def _social_roster_snapshot(
        self,
        connection: MessengerConnection,
        list_tag: str = "B",
    ) -> list[tuple[Identity, SocialRow]]:
        if self.social is None or connection.identity is None:
            return []
        tag = str(list_tag or "B").strip().upper()
        if tag == "I":
            candidates = self.social.snapshot(connection.identity.persona, "I")
        elif tag in {"P", "PLAYER", "PLAYERS"}:
            candidates = self.social.recent_player_snapshot(
                connection.identity.persona,
                "carbon",
            )
        elif tag in {"A", "ALL"}:
            candidates = (
                *self.social.snapshot(connection.identity.persona, "B"),
                *self.social.recent_player_snapshot(connection.identity.persona, "carbon"),
                *self.social.snapshot(connection.identity.persona, "I"),
            )
        else:
            # Carbon's client-side tabs split one LIST=B response by ATTR:
            # AT=friend, R/P=request, D=recently encountered player.
            candidates = (
                *self.social.snapshot(connection.identity.persona, "B"),
                *self.social.recent_player_snapshot(connection.identity.persona, "carbon"),
            )
        result: list[tuple[Identity, SocialRow]] = []
        seen: set[str] = set()
        for row in candidates:
            key = row.user.casefold()
            if key in seen:
                continue
            identity = self._resolve_identity(row.user)
            if identity is not None:
                seen.add(key)
                result.append((identity, row))
        return result

    @staticmethod
    def _relation_target(fields: Mapping[str, str]) -> str:
        for key in ("USER", "PERS", "NAME", "TARGET", "TARG", "TO"):
            value = fields.get(key, "")
            if value:
                return CarbonMessengerService._persona(value)
        return ""

    @staticmethod
    def _relation_accepts(value: object) -> bool:
        text = str(value or "").strip().upper()
        if text in {"0", "N", "NO", "F", "FALSE", "D", "DECLINE", "DENY", "REJECT", "REJECTED"}:
            return False
        if text in {"1", "Y", "YES", "T", "TRUE", "A", "ACCEPT", "ACCEPTED", "OK"}:
            return True
        return bool(text)

    def _relation_ack(
        self,
        command: str,
        fields: Mapping[str, str],
        request_id: str,
        target: str,
    ) -> FESLFrame:
        # Retail Carbon echoes RADM's ID/PRES/LRSC/USER fields verbatim.
        # Keep the same shape for the related roster mutation commands.
        reply: dict[str, object] = {"ID": request_id}
        for key in ("PRES", "LRSC", "LIST", "ANSW"):
            if key in fields:
                reply[key] = fields[key]
        if target:
            reply["USER"] = target
        return self._reply(command, reply)

    def _relation_row(
        self,
        owner: str,
        target: str,
    ) -> tuple[Identity, SocialRow] | None:
        if self.social is None:
            return None
        row = self.social.presence_row(owner, target)
        if row is None:
            return None
        identity = self._resolve_identity(row.user)
        if identity is None:
            return None
        return identity, row

    def _relation_frames_for_current(
        self,
        owner: str,
        target: str,
        *,
        request_id: str = "-1",
    ) -> list[FESLFrame]:
        resolved = self._relation_row(owner, target)
        if resolved is None:
            return []
        identity, row = resolved
        attr = self._carbon_roster_attr(row)
        frames = [self._roster_change_frame(identity, "A", attr=attr)]
        frames.append(self._roster_frame(identity, request_id, attr=attr))
        frames.append(self._presence_from_social_row(identity, row))
        return frames

    def _deliver_relation_row(self, owner: str, target: str) -> int:
        if self.social is None:
            return 0
        resolved = self._relation_row(owner, target)
        if resolved is None:
            return 0
        _identity, row = resolved
        attr = self._carbon_roster_attr(row)
        delivered = 0
        delivered += self.social.deliver(
            owner, "RNOT", (("CHNG", "A"), ("USER", row.user), ("ATTR", attr))
        )
        delivered += self.social.deliver(
            owner,
            "ROST",
            (("ID", "-1"), ("USER", row.user), ("ATTR", attr)),
        )
        delivered += self.social.deliver(
            owner, "PGET", self._social_presence_fields(row)
        )
        return delivered

    def _deliver_relation_delete(
        self,
        owner: str,
        target: str,
        attr: str,
    ) -> int:
        if self.social is None:
            return 0
        return self.social.deliver(
            owner,
            "RNOT",
            (("CHNG", "D"), ("USER", target), ("ATTR", attr)),
        )

    def _same_session_row(self, owner: str, target: str) -> SocialRow | None:
        if self.social is None:
            return None
        wanted = target.casefold()
        for row in self.social.recent_player_snapshot(owner, "carbon"):
            if row.user.casefold() == wanted:
                return row
        return None

    def _restore_session_player_for_current(
        self, owner: str, target: str
    ) -> list[FESLFrame]:
        row = self._same_session_row(owner, target)
        if row is None:
            return []
        identity = self._resolve_identity(row.user)
        if identity is None:
            return []
        return [
            self._roster_change_frame(identity, "A", attr="D"),
            self._roster_frame(identity, "-1", attr="D"),
            self._presence_from_social_row(identity, row),
        ]

    def _restore_session_player_for_peer(self, owner: str, target: str) -> int:
        if self.social is None:
            return 0
        row = self._same_session_row(owner, target)
        if row is None:
            return 0
        delivered = self.social.deliver(
            owner, "RNOT", (("CHNG", "A"), ("USER", row.user), ("ATTR", "D"))
        )
        delivered += self.social.deliver(
            owner, "PGET", self._social_presence_fields(row)
        )
        return delivered

    def _handle_roster_add(
        self,
        connection: MessengerConnection,
        command: str,
        fields: Mapping[str, str],
        request_id: str,
    ) -> list[FESLFrame]:
        target = self._relation_target(fields)
        replies = [self._relation_ack(command, fields, request_id, target)]
        if self.social is None or connection.identity is None or not target:
            return replies
        owner = connection.identity.persona
        result = self.social.request_friend(owner, target)
        delivered = 0
        if result.accepted and result.reason in {"requested", "accepted", "already_friends", "already_pending"}:
            # RADM itself updates the sender's pending row in retail.  The
            # target still needs an unsolicited RNOT/ROST/PGET sequence.
            delivered += self._deliver_relation_row(target, owner)
            if result.reason in {"accepted", "already_friends"}:
                replies.extend(self._relation_frames_for_current(owner, target))
        log.info(
            "Carbon Messenger friend add: owner=%s target=%s result=%s changed=%d delivered=%d",
            owner,
            target or "<missing>",
            result.reason,
            int(result.changed),
            delivered,
        )
        return replies

    def _handle_roster_response(
        self,
        connection: MessengerConnection,
        command: str,
        fields: Mapping[str, str],
        request_id: str,
    ) -> list[FESLFrame]:
        target = self._relation_target(fields)
        replies = [self._relation_ack(command, fields, request_id, target)]
        if self.social is None or connection.identity is None or not target:
            return replies
        owner = connection.identity.persona
        accepted = self._relation_accepts(
            fields.get("ANSW", fields.get("ANSWER", fields.get("ACPT", "")))
        )
        result = self.social.respond_friend(owner, target, accepted)
        delivered = 0
        if result.accepted and result.changed:
            if accepted:
                replies.extend(self._relation_frames_for_current(owner, target))
                delivered += self._deliver_relation_row(target, owner)
            else:
                identity = self._resolve_identity(target)
                if identity is not None:
                    replies.append(
                        self._roster_change_frame(identity, "D", attr="R")
                    )
                delivered += self._deliver_relation_delete(target, owner, "P")
                replies.extend(self._restore_session_player_for_current(owner, target))
                delivered += self._restore_session_player_for_peer(target, owner)
        log.info(
            "Carbon Messenger friend response: owner=%s requester=%s accepted=%d result=%s changed=%d delivered=%d",
            owner,
            target or "<missing>",
            int(accepted),
            result.reason,
            int(result.changed),
            delivered,
        )
        return replies

    def _handle_roster_remove(
        self,
        connection: MessengerConnection,
        command: str,
        fields: Mapping[str, str],
        request_id: str,
    ) -> list[FESLFrame]:
        target = self._relation_target(fields)
        replies = [self._relation_ack(command, fields, request_id, target)]
        if self.social is None or connection.identity is None or not target:
            return replies
        owner = connection.identity.persona
        before_owner = self.social.presence_row(owner, target)
        before_target = self.social.presence_row(target, owner)
        result = self.social.remove_friend(owner, target)
        delivered = 0
        if result.accepted and result.changed:
            identity = self._resolve_identity(target)
            if identity is not None:
                replies.append(
                    self._roster_change_frame(
                        identity,
                        "D",
                        attr=self._carbon_roster_attr(before_owner) if before_owner is not None else "AT",
                    )
                )
            delivered += self._deliver_relation_delete(
                target,
                owner,
                self._carbon_roster_attr(before_target) if before_target is not None else "AT",
            )
            replies.extend(self._restore_session_player_for_current(owner, target))
            delivered += self._restore_session_player_for_peer(target, owner)
        log.info(
            "Carbon Messenger friend remove: owner=%s target=%s result=%s changed=%d delivered=%d",
            owner,
            target or "<missing>",
            result.reason,
            int(result.changed),
            delivered,
        )
        return replies

    def _handle_block(
        self,
        connection: MessengerConnection,
        command: str,
        fields: Mapping[str, str],
        request_id: str,
        *,
        blocked: bool,
    ) -> list[FESLFrame]:
        target = self._relation_target(fields)
        replies = [self._relation_ack(command, fields, request_id, target)]
        if self.social is None or connection.identity is None or not target:
            return replies
        owner = connection.identity.persona
        result = self.social.set_blocked(owner, target, blocked)
        if result.accepted and result.changed:
            identity = self._resolve_identity(target)
            if identity is not None:
                if blocked:
                    replies.extend(self._relation_frames_for_current(owner, target))
                else:
                    replies.append(self._roster_change_frame(identity, "D", attr="B"))
                    replies.extend(self._restore_session_player_for_current(owner, target))
            if blocked:
                self._deliver_relation_delete(target, owner, "AT")
            else:
                self._restore_session_player_for_peer(target, owner)
        log.info(
            "Carbon Messenger block update: owner=%s target=%s blocked=%d result=%s changed=%d",
            owner,
            target or "<missing>",
            int(blocked),
            result.reason,
            int(result.changed),
        )
        return replies

    def _roster_frame(
        self,
        identity: Identity,
        request_id: str,
        *,
        attr: str = "AT",
    ) -> FESLFrame:
        fields: dict[str, object] = {
            "USER": f"{identity.persona}@messaging.ea.com",
            "ID": request_id,
            "UID": str(self.identities.wire_player_id(identity)),
            "GROUP": "",
        }
        if attr:
            fields["ATTR"] = attr
        return self._reply("ROST", fields)

    @classmethod
    def _roster_change_frame(
        cls,
        identity: Identity,
        change: str,
        *,
        attr: str = "AT",
    ) -> FESLFrame:
        fields: dict[str, object] = {
            "CHNG": change,
            "USER": f"{identity.persona}@messaging.ea.com",
        }
        if attr:
            fields["ATTR"] = attr
        return cls._reply("RNOT", fields)

    @classmethod
    def _presence_frame(
        cls,
        connection: MessengerConnection,
        *,
        subscription_id: str | None = None,
    ) -> FESLFrame:
        assert connection.identity is not None
        hidden = not connection.presence_ready or connection.show == "DISC"
        status = "" if hidden else connection.status
        fields: dict[str, object] = {
            "STAT": f'"{status}"',
            "TIID": "0",
            "TITL": f'"{CARBON_TITLE}"',
        }
        # create&invitejoin frame 106 binds the initial buddy presence to the
        # RGET auto-subscription.  Later unsolicited presence changes omit ID.
        if subscription_id is not None:
            fields["ID"] = subscription_id
        fields.update(
            {
                "SHOW": "DISC" if hidden else connection.show,
                "CHNG": "1",
                "USER": f"{connection.identity.persona}@messaging.ea.com/{CARBON_RESOURCE}",
            }
        )
        if connection.presence_attr and not hidden:
            fields["ATTR"] = connection.presence_attr
        if not hidden:
            fields.update(connection.presence_fields)
        return cls._reply("PGET", fields)

    def dispatch(self, frame: FESLFrame, connection: MessengerConnection) -> list[FESLFrame]:
        fields = frame.fields
        command = frame.command.upper()
        request_id = fields.get("ID", "0")
        method = RETAIL_REQUEST_HANDLERS.get(command) or _LEGACY_REQUEST_HANDLERS.get(command)
        if method is not None:
            if command not in {"AUTH", "PING", "DISC"}:
                if not connection.authenticated or connection.identity is None:
                    return [self._reply(command, {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
                if connection.close_requested or connection.forced_logoff_notice_sent:
                    return []
                # Rejected duplicate sessions may finish read-only bootstrap,
                # but must never publish messages or change the winner's state.
                if connection.forced_logoff_reason and command not in {"RGET", "EPGT", "PSET"}:
                    return [self._reply(command, {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
            return getattr(self, method)(command, fields, request_id, connection)
        log.warning(
            "Carbon Messenger unhandled command: persona=%s command=%s id=%s fields=%s",
            connection.identity.persona
            if connection.identity is not None
            else "<unauthenticated>",
            command or "<missing>",
            request_id,
            ",".join(sorted(fields)) or "<none>",
        )
        return []

    def _dispatch_auth(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if connection.authenticated:
            return [self._reply("AUTH", {"ID": request_id, "ERR": "ALREADY_AUTHENTICATED"})]
        identity = self.identities.resolve_session(fields.get("LKEY", ""))
        if identity is None:
            return [self._reply("AUTH", {"ID": request_id, "ERR": "INVALID_SESSION"})]
        connection.identity = identity
        connection.session_token = fields.get("LKEY", "")
        connection.authenticated = True
        self._register(connection)
        log.info(
            "Carbon Messenger authenticated: persona=%s user_id=%d",
            identity.persona,
            identity.user_id,
        )
        return [self._auth_success_frame(identity, request_id)]

    @staticmethod
    def _auth_success_frame(identity: Identity, request_id: str) -> FESLFrame:
        user = f"{identity.persona}@messaging.ea.com/{CARBON_RESOURCE}"
        return CarbonMessengerService._reply(
            "AUTH",
            {
                "TIID": "0",
                # Retail Carbon AUTH preserves the quotes around TITL.
                # The Messenger frontend uses this title metadata when it
                # resolves the localized game-invite description.
                "TITL": f'"{CARBON_TITLE}"',
                "USER": user,
                "ID": request_id,
            },
        )

    def _dispatch_ping(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        connection.ping_responses += 1
        return []

    def _dispatch_roster_get(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        list_tag = fields.get("LIST", "B").upper() or "B"
        if self.social is not None:
            buddies = self._social_roster_snapshot(connection, list_tag)
            log.info(
                "Carbon Messenger shared roster snapshot: persona=%s list=%s entries=%s online=%s",
                connection.identity.persona if connection.identity is not None else "<unauthenticated>",
                list_tag,
                ",".join(identity.persona for identity, _row in buddies) or "none",
                ",".join(identity.persona for identity, row in buddies
                         if row.online and not self._appears_offline(identity.persona)) or "none",
            )
            replies = [self._reply("RGET", {"ID": request_id, "SIZE": str(len(buddies))})]
            for identity, row in buddies:
                replies.append(
                    self._roster_frame(
                        identity,
                        request_id,
                        attr=self._carbon_roster_attr(row),
                    )
                )
                if (row.online and not self._appears_offline(identity.persona)) or row.request:
                    replies.append(
                        self._presence_from_social_row(
                            identity,
                            row,
                            subscription_id="auto-subscribe%3a1",
                        )
                    )
            return replies
        buddies = self._buddy_snapshot(connection)
        log.info(
            "Carbon Messenger roster snapshot: persona=%s list=B buddies=%s online=%s",
            connection.identity.persona if connection.identity is not None else "<unauthenticated>",
            ",".join(identity.persona for identity, _peer in buddies) or "none",
            ",".join(identity.persona for identity, peer in buddies if peer is not None) or "none",
        )
        replies = [self._reply("RGET", {"ID": request_id, "SIZE": str(len(buddies))})]
        for identity, peer in buddies:
            replies.append(self._roster_frame(identity, request_id))
            if peer is not None:
                replies.append(
                    self._presence_frame(peer, subscription_id="auto-subscribe%3a1")
                )
        return replies

    def _dispatch_endpoint_get(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        with self._lock:
            result = {"ID": request_id, "ENAB": "T" if connection.endpoint_enabled else "F",
                      "ADDR": connection.endpoint_address if connection.endpoint_enabled else ""}
        return [self._reply("EPGT", result)]

    def _dispatch_presence_set(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if connection.forced_logoff_reason:
            return [self._reply("PSET", {"ID": request_id})]
        with self._lock:
            show = fields.get("SHOW", connection.show).upper()
            if show not in _PRESENCE_STATES:
                return [self._reply("PSET", {"ID": request_id, "ERR": "INVALID_PRESENCE"})]
            connection.show = show
            connection.presence_ready = True
            connection.status = fields.get("STAT", connection.status).strip('"')
            connection.presence_fields.update({key: value for key, value in fields.items() if key in _PRESENCE_FIELDS})
            if "ATTR" in fields:
                connection.presence_attr = fields["ATTR"]
            elif show == "GAME" and self.is_inviteable(connection.identity):
                # Preserve the existing authoritative joinable-room fallback.
                connection.presence_attr = "J"
            elif "SHOW" in fields and show != "GAME":
                connection.presence_attr = ""
            if connection.presence_attr:
                connection.presence_fields["ATTR"] = connection.presence_attr
            else:
                connection.presence_fields.pop("ATTR", None)
        if connection.identity is not None:
            if self.social is not None:
                self.social.set_presence(
                    connection.identity.persona,
                    show=connection.show,
                    stat=connection.status,
                    product=connection.presence_fields.get("PROD", "NFS-CONSOLE-2007"),
                    title=CARBON_TITLE,
                    attr=connection.presence_attr,
                )
                self._record_public_presence(connection, connection.show)
                self._notify_social_presence(connection.identity.persona, connection.presence_fields)
                peer_count = len(self.social.snapshot(connection.identity.persona, "B"))
            else:
                presence = self._presence_frame(connection)
                peers = self._online_peers(connection)
                for peer in peers:
                    with self._lock:
                        suppressed = connection.identity.persona.casefold() in peer.suppressed_presence
                    if not suppressed and self._available(peer):
                        peer.deliver(presence)
                peer_count = len(peers)
            log.info(
                "Carbon Messenger presence: persona=%s show=%s attr=%s peers=%d shared=%d",
                connection.identity.persona,
                connection.show or "-",
                connection.presence_attr or "-",
                peer_count,
                int(self.social is not None),
            )
        return [self._reply("PSET", {"ID": request_id})]

    def _dispatch_game_invite(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if connection.identity is None:
            return [self._reply("GINV", {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
        target = self._persona(fields.get("USER", ""))
        with self._lock:
            target_key = target.casefold()
            self._pending_invite_completions.pop(target_key, None)
            self._pending_invite_revokes.discard(target_key)
        details = {
            str(key): str(value)
            for key, value in self.invite_details(connection.identity).items()
            if str(value).strip()
            and str(key).upper() not in {"HOST", "USER", "TYPE", "SESS", "ID"}
        }
        game_string = self._invite_game_string(details)
        notification_fields = {
            "HOST": connection.identity.persona,
            "USER": connection.identity.persona,
            "TYPE": "I",
            "SESS": fields.get("SESS", "0"),
        }
        if game_string:
            notification_fields["GSTR"] = game_string
        notification = self._push(
            "GNOT",
            notification_fields,
        )
        targets = self._targets(target)
        for peer in targets:
            peer.enqueue(notification)
        log.info(
            "Carbon Messenger invite: from=%s target=%s delivered=%d "
            "game_type=%s game_mode=%s players=%s/%s collision=%s "
            "track=%s length=%s gstr=%r wire=envelope+gstr",
            connection.identity.persona,
            target or "<missing>",
            len(targets),
            details.get("game_type", "-"),
            details.get("game_mode", "-"),
            details.get("AP", "-"),
            details.get("MP", details.get("max_online_player", "-")),
            details.get("collision_detection", "-"),
            details.get("track", "-"),
            details.get("length", "-"),
            game_string,
        )
        return [self._reply("GINV", {"ID": request_id})]

    def _dispatch_game_response(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if connection.identity is None:
            return [self._reply("GRSP", {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
        host = self._persona(fields.get("USER", ""))
        accepted = fields.get("ANSW", "").upper() == "Y"
        host_notification = self._push(
            "GNOT",
            {
                "HOST": connection.identity.persona,
                "USER": connection.identity.persona,
                "TYPE": "A" if accepted else "R",
                "SESS": fields.get("SESS", "0"),
            },
        )
        targets = self._targets(host)
        for peer in targets:
            peer.enqueue(host_notification)
        if accepted:
            with self._lock:
                guest_key = connection.identity.persona.casefold()
                self._pending_invite_completions[guest_key] = (
                    host,
                    fields.get("SESS", "0"),
                )
                self._pending_invite_revokes.discard(guest_key)
        else:
            with self._lock:
                guest_key = connection.identity.persona.casefold()
                self._pending_invite_completions.pop(guest_key, None)
                self._pending_invite_revokes.discard(guest_key)
        log.info(
            "Carbon Messenger invite response: guest=%s host=%s accepted=%d "
            "delivered=%d guest_completion_armed=%d",
            connection.identity.persona,
            host or "<missing>",
            int(accepted),
            len(targets),
            int(accepted),
        )
        return [self._reply("GRSP", {"ID": request_id})]

    def _dispatch_game_revoke(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if connection.identity is None:
            return [self._reply("GRVK", {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
        guest = self._persona(fields.get("USER", ""))
        host = connection.identity.persona
        guest_key = guest.casefold()
        with self._lock:
            pending = self._pending_invite_completions.get(guest_key)
            pending_match = (
                pending is not None
                and pending[0].casefold() == host.casefold()
            )
            if pending_match:
                self._pending_invite_revokes.add(guest_key)
        if pending_match:
            connection.defer_after_send(
                lambda guest=guest: self._release_invite_completion_if_ready(guest)
            )
        log.info(
            "Carbon Messenger invite revoke accepted: host=%s guest=%s "
            "pending_match=%d barrier=wait-for-theater-egeg",
            host,
            guest or "<missing>",
            int(pending_match),
        )
        return [self._reply("GRVK", {"ID": request_id})]

    def _dispatch_roster_add(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._handle_roster_add(connection, command, fields, request_id)

    def _dispatch_roster_response(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._handle_roster_response(connection, command, fields, request_id)

    def _dispatch_roster_remove(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._handle_roster_remove(connection, command, fields, request_id)

    def _dispatch_block(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._handle_block(
            connection, command, fields, request_id, blocked=True
        )

    def _dispatch_unblock(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._handle_block(
            connection, command, fields, request_id, blocked=False
        )

    def _dispatch_user_search(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        return self._search_replies(connection, fields, request_id)

    def _dispatch_send(
        self, command: str, fields: dict[str, str], request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if not self._available(connection):
            return [self._reply(command, {"ID": request_id, "ERR": "NOT_AUTHENTICATED"})]
        target = self._persona(fields.get("USER", ""))
        message_type = fields.get("TYPE", "C")
        seconds = fields.get("SECS", "0")
        try:
            valid_seconds = (seconds.lstrip("+-").isascii() and seconds.lstrip("+-").isdigit()
                             and -0x80000000 <= int(seconds, 10) <= 0x7FFFFFFF)
        except ValueError:
            valid_seconds = False
        if not target or message_type not in {"C", "A"} or "BODY" not in fields or not valid_seconds:
            return [self._reply(command, {"ID": request_id, "ERR": "INVALID_REQUEST"})]
        source = connection.identity.persona
        if self._blocked(source, target):
            return [self._reply(command, {"ID": request_id, "ERR": "BLOCKED"})]
        values = {key: value for key, value in fields.items() if key in _MESSAGE_FIELDS}
        values.update(USER=source, TYPE=message_type, SUBJ=fields.get("SUBJ", ""),
                      BODY=fields["BODY"], SECS=seconds)
        if self.social is not None:
            delivered = self.social.deliver(target, "RECV", tuple(values.items()))
        else:
            notification = self._push("RECV", values)
            delivered = sum(peer.deliver(notification) for peer in self._targets(target) if self._available(peer))
        reply = {"ID": request_id}
        if not delivered:
            reply["ERR"] = "USER_OFFLINE"
        return [self._reply(command, reply)]

    def _dispatch_endpoint_set(
        self, command: str, fields: dict[str, str], request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        if fields.get("ENAB") not in {"T", "F"}:
            return [self._reply(command, {"ID": request_id, "ERR": "INVALID_REQUEST"})]
        with self._lock:
            connection.endpoint_enabled = fields["ENAB"] == "T"
            if "ADDR" in fields:
                connection.endpoint_address = fields["ADDR"]
        return [self._reply(command, {"ID": request_id})]

    def _dispatch_compatibility(
        self, command: str, fields: dict[str, str], request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        """SDK wire compatibility only; no inferred mail store or broadcast scope.

        MLST advertises an empty list, TCKL acknowledges a tickle. BRDC cannot
        safely choose recipients from USER/GROUP alone: explicitly reject it
        rather than silently broadcast to unrelated authenticated accounts.
        No gameplay, invitation, subscription or heartbeat state is changed.
        """
        reply = {"ID": request_id}
        reply.update({key: fields[key] for key in ("USER", "GROUP", "LRSC") if key in fields})
        if command == "MLST":
            if not fields.get("USER"):
                reply["ERR"] = "INVALID_REQUEST"
            else:
                reply["SIZE"] = "0"
        elif command == "BRDC":
            reply["ERR"] = "NOT_SUPPORTED"
        return [self._reply(command, reply)]

    def _dispatch_presence_add(
        self, command: str, fields: dict[str, str], request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        target = self._persona(fields.get("USER", ""))
        if not target:
            return [self._reply(command, {"ID": request_id, "ERR": "INVALID_REQUEST"})]
        if self._blocked(connection.identity.persona, target):
            return [self._reply(command, {"ID": request_id, "ERR": "BLOCKED"})]
        with self._lock:
            connection.subscriptions.add(target.casefold())
            connection.suppressed_presence.discard(target.casefold())
            peers = [peer for peer in self._targets(target) if self._available(peer)]
            presence = None
        replies = [self._reply(command, {"ID": request_id})]
        if self.social is not None:
            identity = self._resolve_identity(target)
            if identity is not None:
                row = self.social.presence_row(connection.identity.persona, target)
                presence = self._presence_from_social_row(
                    identity, row or SocialRow(user=target, online=False), subscription_id=request_id,
                )
        if presence is None and peers:
            presence = self._presence_frame(peers[0], subscription_id=request_id)
        if presence is None:
            identity = self._resolve_identity(target)
            if identity is not None:
                presence = self._presence_from_social_row(
                    identity, SocialRow(user=target, online=False), subscription_id=request_id,
                )
        if presence is not None:
            replies.append(presence)
        if self.social is not None:
            row = self.social.presence_row(connection.identity.persona, target)
            with self._lock:
                connection.subscription_presence[target.casefold()] = row
        return replies

    def _dispatch_presence_delete(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        target = self._persona(fields.get("USER", ""))
        if not target:
            return [self._reply(command, {"ID": request_id, "ERR": "INVALID_REQUEST"})]
        with self._lock:
            connection.subscriptions.discard(target.casefold())
            connection.subscription_presence.pop(target.casefold(), None)
            # Also opt out of implicit RGET friend/recent-player updates.
            connection.suppressed_presence.add(target.casefold())
        return [
            self._reply(
                "PDEL",
                {
                    "ID": request_id,
                    "STAT": "OK",
                    "RESULT": "OK",
                },
            )
        ]

    def _dispatch_disconnect(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        connection.close_requested = True
        return []

    @staticmethod
    def ping_frame() -> FESLFrame:
        return FESLFrame.from_fields("PING", {}, transaction=0)

    @staticmethod
    def _forced_logoff_frame(reason: str) -> FESLFrame:
        return FESLFrame.from_fields(
            "ADMN",
            {"TYPE": str(reason or "DUPL").upper(), "SECS": "0"},
            transaction=0x80000000,
            trailing_newline=True,
        )

    @staticmethod
    def _reply(command: str, fields: dict[str, object]) -> FESLFrame:
        return FESLFrame.from_fields(
            command,
            fields,
            transaction=0,
            trailing_newline=True,
        )

    @staticmethod
    def _push(command: str, fields: dict[str, object]) -> FESLFrame:
        return FESLFrame.from_fields(
            command,
            fields,
            transaction=0x80000000,
            trailing_newline=True,
        )
