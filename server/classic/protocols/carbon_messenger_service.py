"""Minimal Carbon EA Messenger service with retail-compatible liveness.

The retail client opens this TCP service after FESL Hello/Login.  The server is
responsible for sending periodic ``PING`` frames; the client answers with its
own ``PING``.  Keeping this channel absent or silent can make the frontend
consider the online session lost even while FESL/Theater sockets remain open.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from threading import Lock, RLock
from typing import Callable, Mapping

from classic.ea.messenger import EAMessengerFrame as FESLFrame
from classic.ea.social import Presence, SocialRow, SocialService, canonical_persona
from classic.protocols.carbon_messenger_ipc import (
    CarbonIPCForcedLogoff,
    CarbonIPCIdentity as Identity,
    CarbonMessengerIPCState as IdentityStore,
)


CARBON_TITLE = "Need for Speed Carbon"
CARBON_RESOURCE = "eagames/NFS-2007"
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
    forced_logoff_notice_sent: bool = False
    ping_responses: int = 0
    show: str = "CHAT"
    status: str = "en%3dPlaying Need for Speed Carbon"
    presence_attr: str = ""
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
    """Small authoritative subset needed by the Carbon frontend."""

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
        if "*" in wanted:
            # The original server converted any asterisk search into one
            # case-insensitive SQL LIKE substring query.
            needle = wanted.replace("*", "").casefold()
            return not needle or needle in display.casefold()
        return display.casefold() == wanted.casefold()

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
            # exact-or-asterisk semantics and privacy rules locally.
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

    @staticmethod
    def _social_presence_fields(row: SocialRow) -> tuple[tuple[str, str], ...]:
        presence = row.presence or Presence()
        show = presence.show if row.online else "AWAY"
        fields: list[tuple[str, str]] = [
            ("STAT", presence.stat),
            ("PROD", presence.product),
            ("TITL", presence.title),
            ("SHOW", show or ("CHAT" if row.online else "AWAY")),
            ("USER", row.user),
        ]
        attr = row.attr or presence.attr
        if attr:
            fields.append(("ATTR", attr))
        return tuple(fields)

    def _presence_from_social_row(
        self,
        identity: Identity,
        row: SocialRow,
        *,
        subscription_id: str | None = None,
    ) -> FESLFrame:
        presence = row.presence or Presence()
        show = presence.show if row.online else "AWAY"
        status = presence.stat or "en%3dOnline"
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
        attr = row.attr or presence.attr
        if attr:
            fields["ATTR"] = attr
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
            target = canonical_persona(values.get("USER", ""))
            identity = self._resolve_identity(target)
            command = str(verb or "").upper()
            if identity is None:
                return False
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
                return connection.deliver(self._presence_from_social_row(identity, row))
            if command == "ROST":
                return connection.deliver(
                    self._roster_frame(
                        identity,
                        values.get("ID", "-1"),
                        attr=("AT" if values.get("ATTR", "AT") in {"", "B"} else values.get("ATTR", "AT")),
                    )
                )
            if command == "RNOT":
                return connection.deliver(
                    self._roster_change_frame(
                        identity,
                        values.get("CHNG", "A"),
                        attr=("AT" if values.get("ATTR", "AT") in {"", "B"} else values.get("ATTR", "AT")),
                    )
                )
            return False

        return send

    def _notify_social_presence(self, persona: str) -> None:
        if self.social is None:
            return
        for relation in self.social.snapshot(persona, "B"):
            row = self.social.presence_row(relation.user, persona)
            if row is not None:
                self.social.deliver(
                    relation.user,
                    "PGET",
                    self._social_presence_fields(row),
                )

    def sync_session(self, connection: MessengerConnection) -> None:
        forced_logoff = self.identities.forced_logoff(connection.session_token)
        if forced_logoff is not None:
            if connection.forced_logoff_notice_sent:
                connection.authenticated = False
                connection.close_requested = True
            else:
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

    def begin_forced_logoff(
        self,
        connection: MessengerConnection,
        token: str,
        forced_logoff: CarbonIPCForcedLogoff,
    ) -> FESLFrame:
        """Build the retail asynchronous admin notice for a displaced login."""

        connection.identity = forced_logoff.identity
        connection.session_token = str(token or "")
        # Let the adapter's post-send poll perform the bounded protocol close
        # only after the ADMN frame itself has reached the socket.
        connection.authenticated = True
        connection.forced_logoff_notice_sent = True
        log.warning(
            "Carbon Messenger forced logoff sent: persona=%s type=%s "
            "action=client-native-error",
            forced_logoff.identity.persona,
            forced_logoff.reason,
        )
        return self._forced_logoff_frame(forced_logoff.reason)

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
            self._notify_social_presence(connection.identity.persona)
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
        identity = connection.identity
        if identity is None:
            return
        key = identity.persona.casefold()
        with self._lock:
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
            self.social.unregister_control(connection.connection_id)
            self.social.unregister_lobby(connection.connection_id)
            self._notify_social_presence(identity.persona)
            return
        # No roster mutations exist without an authoritative social graph.

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
            candidates = self.social.game_player_snapshot(
                connection.identity.persona,
                "carbon",
            )
        elif tag in {"A", "ALL"}:
            candidates = (
                *self.social.snapshot(connection.identity.persona, "B"),
                *self.social.game_player_snapshot(connection.identity.persona, "carbon"),
                *self.social.snapshot(connection.identity.persona, "I"),
            )
        else:
            # Carbon's client-side tabs split one LIST=B response by ATTR:
            # AT=friend, R/P=request, D=live same-game player.
            candidates = (
                *self.social.snapshot(connection.identity.persona, "B"),
                *self.social.game_player_snapshot(connection.identity.persona, "carbon"),
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
        for row in self.social.game_player_snapshot(owner, "carbon"):
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
        fields: dict[str, object] = {
            "STAT": f'"{connection.status}"',
            "TIID": "0",
            "TITL": f'"{CARBON_TITLE}"',
        }
        # create&invitejoin frame 106 binds the initial buddy presence to the
        # RGET auto-subscription.  Later unsolicited presence changes omit ID.
        if subscription_id is not None:
            fields["ID"] = subscription_id
        fields.update(
            {
                "SHOW": connection.show,
                "CHNG": "1",
                "USER": f"{connection.identity.persona}@messaging.ea.com/{CARBON_RESOURCE}",
            }
        )
        if connection.presence_attr:
            fields["ATTR"] = connection.presence_attr
        return cls._reply("PGET", fields)

    def dispatch(self, frame: FESLFrame, connection: MessengerConnection) -> list[FESLFrame]:
        fields = frame.fields
        command = frame.command.upper()
        request_id = fields.get("ID", "0")
        handlers = {
            "AUTH": self._dispatch_auth,
            "PING": self._dispatch_ping,
            "RGET": self._dispatch_roster_get,
            "EPGT": self._dispatch_endpoint_get,
            "PSET": self._dispatch_presence_set,
            "GINV": self._dispatch_game_invite,
            "GRSP": self._dispatch_game_response,
            "GRVK": self._dispatch_game_revoke,
            "RADM": self._dispatch_roster_add,
            "RADD": self._dispatch_roster_add,
            "RSET": self._dispatch_roster_add,
            "RRSP": self._dispatch_roster_response,
            "RDEM": self._dispatch_roster_remove,
            "RDEL": self._dispatch_roster_remove,
            "RREM": self._dispatch_roster_remove,
            "BLCK": self._dispatch_block,
            "BLOK": self._dispatch_block,
            "RBLK": self._dispatch_block,
            "RBLO": self._dispatch_block,
            "UBLK": self._dispatch_unblock,
            "UBLO": self._dispatch_unblock,
            "UNBL": self._dispatch_unblock,
            "BDEL": self._dispatch_unblock,
            "USCH": self._dispatch_user_search,
            "PDEL": self._dispatch_presence_delete,
            "DISC": self._dispatch_disconnect,
        }
        handler = handlers.get(command)
        if handler is not None:
            return handler(command, fields, request_id, connection)
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
        user = f"{identity.persona}@messaging.ea.com/{CARBON_RESOURCE}"
        return [
            self._reply(
                "AUTH",
                {
                    "TIID": "0",
                    # Retail Carbon AUTH preserves the quotes around TITL.
                    # The Messenger frontend uses this title metadata when it
                    # resolves the localized game-invite description.
                    "TITL": f'"{CARBON_TITLE}"',
                    "ID": request_id,
                    "USER": user,
                },
            )
        ]

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
                ",".join(identity.persona for identity, row in buddies if row.online) or "none",
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
                if row.online or row.request:
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
        return [
            self._reply(
                "EPGT",
                {"ID": request_id, "ENAB": "F", "ADDR": ""},
            )
        ]

    def _dispatch_presence_set(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
        show_was_present = "SHOW" in fields
        connection.show = fields.get("SHOW", connection.show)
        connection.status = fields.get("STAT", connection.status).strip('\"')
        if connection.identity is not None:
            if "ATTR" in fields:
                connection.presence_attr = fields.get("ATTR", "")
            elif connection.show.upper() == "GAME" and self.is_inviteable(connection.identity):
                # The retail host publishes ATTR=J while it owns a
                # joinable room. Some local clients omit ATTR from the
                # PSET that changes SHOW to GAME; deriving it from the
                # authoritative Theater membership preserves the same UI
                # contract instead of losing the invite button.
                connection.presence_attr = "J"
            elif show_was_present and connection.show.upper() != "GAME":
                connection.presence_attr = ""
            if self.social is not None:
                self.social.set_presence(
                    connection.identity.persona,
                    show=connection.show,
                    stat=connection.status,
                    product="NFS-CONSOLE-2007",
                    title=CARBON_TITLE,
                    attr=connection.presence_attr,
                )
                self._notify_social_presence(connection.identity.persona)
                peer_count = len(self.social.snapshot(connection.identity.persona, "B"))
            else:
                presence = self._presence_frame(connection)
                peers = self._online_peers(connection)
                for peer in peers:
                    peer.enqueue(presence)
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

    def _dispatch_presence_delete(
        self,
        command: str,
        fields: dict[str, str],
        request_id: str,
        connection: MessengerConnection,
    ) -> list[FESLFrame]:
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
