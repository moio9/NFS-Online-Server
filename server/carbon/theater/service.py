"""Clean Carbon Theater connection and empty-directory service."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from threading import Lock, RLock
import time
from typing import Callable

from carbon.accounts.identity import Identity, IdentityStore
from carbon.fesl.frame import FESLFrame
from carbon.theater.directory import CarbonGameDirectory


log = logging.getLogger(__name__)


@dataclass(eq=False)
class TheaterConnection:
    identity: Identity | None = None
    peer_ip: str = "0.0.0.0"
    peer_port: int = 0
    selected_gid: str = ""
    announced_gdet_gid: str = ""
    invite_host_persona: str = ""
    pending_invite_completion_gid: str = ""
    close_requested: bool = False
    pending: list[FESLFrame] = field(default_factory=list, repr=False)
    pending_lock: Lock = field(default_factory=Lock, repr=False)
    sender: Callable[[FESLFrame], bool] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    connection_id: str = ""
    close_reason: str = ""
    forced_logoff_reason: str = ""
    session_token: str = ""

    def enqueue(self, frame: FESLFrame) -> None:
        with self.pending_lock:
            self.pending.append(frame)

    def drain(self, *, force: bool = False) -> list[FESLFrame]:
        del force  # retained for source compatibility; delivery is causal now.
        with self.pending_lock:
            frames = list(self.pending)
            self.pending.clear()
        return frames

    def deliver(self, frame: FESLFrame) -> bool:
        sender = self.sender
        return bool(sender(frame)) if sender is not None else False


class CarbonTheaterService:
    def __init__(
        self,
        identities: IdentityStore,
        games: CarbonGameDirectory,
        *,
        clock: Callable[[], float] | None = None,
        leave_handler: Callable[[str, int], bool | None] | None = None,
    ) -> None:
        self.identities = identities
        self.games = games
        self.clock = clock or time.time
        self.leave_handler = leave_handler
        self._connection_lock = RLock()
        self._connections: dict[str, set[TheaterConnection]] = {}

    def _register(self, connection: TheaterConnection) -> None:
        if connection.identity is None:
            return
        key = connection.identity.persona.casefold()
        with self._connection_lock:
            self._connections.setdefault(key, set()).add(connection)

    def disconnect(self, connection: TheaterConnection) -> None:
        identity = connection.identity
        if identity is None:
            return
        key = identity.persona.casefold()
        with self._connection_lock:
            connections = self._connections.get(key)
            if connections is None:
                return
            connections.discard(connection)
            if not connections:
                self._connections.pop(key, None)

    def complete_invite_entry(self, connection: TheaterConnection) -> bool:
        """Publish an invited join only after its EGEG batch was written."""

        identity = connection.identity
        gid = connection.pending_invite_completion_gid
        if identity is None or not gid:
            return False
        connection.pending_invite_completion_gid = ""
        completed = self.games.mark_invite_entry_complete(gid, identity.user_id)
        log.info(
            "Carbon Theater invite entry completion published: "
            "persona=%s gid=%s completed=%d barrier=egeg-sent",
            identity.persona,
            gid,
            int(completed),
        )
        return completed

    def complete_forced_logoff_bootstrap(
        self,
        connection: TheaterConnection,
    ) -> bool:
        """Publish the duplicate GLST write barrier to shared Messenger."""

        if not connection.forced_logoff_reason or not connection.session_token:
            return False
        completed = self.identities.mark_forced_logoff_theater_ready(
            connection.session_token
        )
        if completed:
            log.warning(
                "Carbon Theater duplicate bootstrap completed: persona=%s "
                "action=release-messenger-dupl",
                connection.identity.persona
                if connection.identity is not None
                else "<unauthenticated>",
            )
        return completed

    def dispatch(self, frame: FESLFrame, connection: TheaterConnection) -> list[FESLFrame]:
        fields = frame.fields
        command = frame.command
        transaction_id = fields.get("TID", "0")
        handlers = {
            "CONN": self._dispatch_conn,
            "USER": self._dispatch_user,
            "ECHO": self._dispatch_echo,
            "PING": self._dispatch_liveness,
            "KEEP": self._dispatch_liveness,
            "LLST": self._dispatch_lobby_list,
            "GLST": self._dispatch_game_list,
            "PCNT": self._dispatch_player_count,
            "GDAT": self._dispatch_game_data,
            "CGAM": self._dispatch_create_game,
            "EGAM": self._dispatch_enter_game,
            "ECNL": self._dispatch_leave_game,
        }
        handler = handlers.get(command)
        if handler is None:
            log.warning(
                "Carbon Theater unhandled command: peer=%s:%d persona=%s command=%s tid=%s keys=%s",
                connection.peer_ip,
                connection.peer_port,
                connection.identity.persona if connection.identity is not None else "<unauthenticated>",
                command,
                transaction_id,
                ",".join(sorted(fields))[:500],
            )
            return []
        return handler(command, fields, transaction_id, connection)

    def _dispatch_conn(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        log.info(
            "Carbon Theater CONN: peer=%s:%d tid=%s prot=%s",
            connection.peer_ip,
            connection.peer_port,
            transaction_id,
            fields.get("PROT", "0"),
        )
        return [
            self._reply(
                "CONN",
                {
                    "TIME": str(int(self.clock())),
                    "TID": transaction_id,
                    "activityTimeoutSecs": "900",
                    "PROT": fields.get("PROT", "0"),
                },
            )
        ]

    def _dispatch_user(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        token = fields.get("LKEY", fields.get("lkey", ""))
        identity = self.identities.resolve_session(token)
        if identity is None:
            forced_logoff = self.identities.resolve_forced_logoff(token)
            if forced_logoff is not None:
                identity, forced_logoff_reason = forced_logoff
                # The newcomer has already been assigned a temporary LKEY so
                # Messenger can deliver Carbon's retail ADMN/TYPE=DUPL error.
                # An earlier Theater INVALID_SESSION can win the client error
                # race and hide the native ADMN/DUPL -204 notice.  Complete the
                # Theater USER bootstrap, but do not register this rejected
                # connection or grant it ownership of the active account.
                connection.identity = identity
                connection.forced_logoff_reason = forced_logoff_reason
                connection.session_token = token
                log.warning(
                    "Carbon Theater duplicate USER bootstrap accepted: "
                    "peer=%s:%d tid=%s persona=%s forced_logoff=%s "
                    "action=read-only-until-messenger-native-error",
                    connection.peer_ip,
                    connection.peer_port,
                    transaction_id,
                    identity.persona,
                    forced_logoff_reason,
                )
                return [
                    self._reply(
                        "USER",
                        {"NAME": identity.persona, "TID": transaction_id},
                    )
                ]
            log.warning(
                "Carbon Theater USER rejected: peer=%s:%d tid=%s reason=INVALID_SESSION",
                connection.peer_ip,
                connection.peer_port,
                transaction_id,
            )
            return [self._reply("USER", {"TID": transaction_id, "ERR": "INVALID_SESSION"})]
        connection.identity = identity
        self._register(connection)
        log.info(
            "Carbon Theater USER authenticated: peer=%s:%d tid=%s persona=%s user_id=%d profile_id=%d",
            connection.peer_ip,
            connection.peer_port,
            transaction_id,
            identity.persona,
            identity.user_id,
            identity.profile_id,
        )
        return [self._reply("USER", {"NAME": identity.persona, "TID": transaction_id})]

    def _dispatch_echo(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        return [
            self._reply(
                "ECHO",
                {
                    "PORT": str(connection.peer_port),
                    "TID": transaction_id or "1",
                    "IP": connection.peer_ip,
                    "TXN": "ECHO",
                    "ERR": "0",
                    "TYPE": fields.get("TYPE", "1"),
                },
            )
        ]

    def _dispatch_liveness(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        reply = {"TID": transaction_id or "1"}
        if "HKEEP" in fields:
            reply["HKEEP"] = fields["HKEEP"]
        return [self._reply(command, reply)]

    def _dispatch_lobby_list(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        lobby_id = "257"
        game_count = len(self.games.list(lobby_id))
        log.info(
            "Carbon Theater LLST: persona=%s tid=%s lobby=%s games=%d",
            connection.identity.persona if connection.identity is not None else "<unauthenticated>",
            transaction_id,
            lobby_id,
            game_count,
        )
        return [
            self._reply("LLST", {"TID": transaction_id, "NUM-LOBBIES": "1"}),
            self._reply(
                "LDAT",
                {
                    "PASSING": str(game_count),
                    "NAME": "Internet",
                    "LOCALE": "en_US",
                    "TID": transaction_id,
                    "MAX-GAMES": "10000",
                    "NUM-GAMES": str(game_count),
                    "FAVORITE-GAMES": "0",
                    "FAVORITE-PLAYERS": "0",
                    "LID": lobby_id,
                },
            ),
        ]

    def _dispatch_game_list(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        lobby_id = fields.get("LID", "257") or "257"
        games = self.games.list(lobby_id)
        log.info(
            "Carbon Theater GLST: persona=%s tid=%s lobby=%s games=%d gids=%s",
            connection.identity.persona if connection.identity is not None else "<unauthenticated>",
            transaction_id,
            lobby_id,
            len(games),
            ",".join(game.gid for game in games) or "none",
        )
        replies = [
            self._reply(
                "GLST",
                {
                    "TID": transaction_id,
                    "LID": lobby_id,
                    "LOBBY-NUM-GAMES": str(len(games)),
                    "LOBBY-MAX-GAMES": "10000",
                    "NUM-GAMES": str(len(games)),
                },
            )
        ]
        replies.extend(self._reply("GDAT", {**game.row(), "TID": transaction_id}) for game in games)
        return replies

    def _dispatch_player_count(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        lobby_id = fields.get("LID", "257") or "257"
        return [
            self._reply(
                "PCNT",
                {
                    "COUNT": str(len(self.games.list(lobby_id))),
                    "TID": transaction_id,
                    "LID": lobby_id,
                },
            )
        ]

    def _dispatch_game_data(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        gid = fields.get("GID", "")
        lookup_user = fields.get("USER", "")
        game = self.games.get(gid) if gid else self.games.find_for_persona(lookup_user)
        if game is None:
            log.warning(
                "Carbon Theater GDAT miss: persona=%s tid=%s gid=%s user=%s",
                connection.identity.persona if connection.identity is not None else "<unauthenticated>",
                transaction_id,
                gid or "<missing>",
                lookup_user or "<missing>",
            )
            return []
        gid = game.gid
        connection.selected_gid = gid
        log.info(
            "Carbon Theater GDAT hit: persona=%s tid=%s gid=%s lookup_user=%s host=%s game_type=%s state=%s players=%d/%d",
            connection.identity.persona if connection.identity is not None else "<unauthenticated>",
            transaction_id,
            gid,
            lookup_user or "-",
            game.host.persona,
            game.properties.get("B-U-game_type", "?"),
            game.properties.get("B-U-matchmaking_state", "?"),
            len(game.participants),
            game.session.capacity,
        )
        game_row = game.row()
        if lookup_user:
            # This is a pre-accept room preview, not an entered-player count.
            # Retail Carbon advertises AP=0 here so the stock GDET handler
            # correlates the following room-details reply with this GDAT.  Keep
            # the real directory population untouched; only project the wire
            # row used by the invited client.
            game_row = {
                **game_row,
                "AP": "0",
                "JP": str(len(game.participants)),
                "QP": "0",
            }
        replies = [self._reply("GDAT", {**game_row, "TID": transaction_id})]
        if lookup_user:
            # GDET belongs to the same invite-preview transaction.  With the
            # retail AP=0 preview above, the unmodified client forwards these
            # details to the invitation popup and keeps its normal join path.
            replies.append(
                self._reply(
                    "GDET",
                    {
                        "TID": transaction_id,
                        "UGID": game.ugid,
                        "LID": game.lobby_id,
                        "GID": gid,
                    },
                )
            )
            connection.announced_gdet_gid = gid
            connection.invite_host_persona = lookup_user
            log.info(
                "Carbon Theater invite GDET announced: persona=%s gid=%s "
                "inviter=%s ugid=%s correlated_tid=%s "
                "advertised_ap=0 advertised_jp=%d",
                connection.identity.persona if connection.identity is not None else "<unauthenticated>",
                gid,
                lookup_user,
                game.ugid,
                transaction_id,
                len(game.participants),
            )
        return replies

    def _dispatch_create_game(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        if connection.identity is None:
            log.warning(
                "Carbon Theater CGAM rejected: peer=%s:%d tid=%s reason=NOT_AUTHENTICATED",
                connection.peer_ip,
                connection.peer_port,
                transaction_id,
            )
            return [self._reply("CGAM", {"TID": transaction_id, "ERR": "NOT_AUTHENTICATED"})]
        log.info(
            "Carbon Theater CGAM request: persona=%s tid=%s name=%s max=%s join=%s game_type=%s state=%s mode=%s",
            connection.identity.persona,
            transaction_id,
            fields.get("N", "<missing>"),
            fields.get("MAX-PLAYERS", fields.get("B-U-max_online_player", "<missing>")),
            fields.get("J", fields.get("JOIN", "<missing>")),
            fields.get("B-U-game_type", "<missing>"),
            fields.get("B-U-matchmaking_state", "<missing>"),
            fields.get("B-U-game_mode", "<missing>"),
        )
        game = self.games.create(connection.identity, fields)
        log.info(
            "Carbon Theater CGAM created: persona=%s tid=%s gid=%s ugid=%s host_id=%d game_type=%s state=%s players=%d/%d",
            connection.identity.persona,
            transaction_id,
            game.gid,
            game.ugid,
            game.host.user_id,
            game.properties.get("B-U-game_type", "?"),
            game.properties.get("B-U-matchmaking_state", "?"),
            len(game.participants),
            game.session.capacity,
        )
        return [
            self._reply(
                "CGAM",
                {
                    "TID": transaction_id,
                    "MAX-PLAYERS": str(game.session.capacity),
                    "EKEY": self.games.ekey,
                    "UGID": game.ugid,
                    "JOIN": game.properties.get("J", "O"),
                    "SECRET": self.games.ekey,
                    "LID": game.lobby_id,
                    "J": game.properties.get("J", "O"),
                    "GID": game.gid,
                },
            )
        ]

    def _dispatch_enter_game(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        if connection.identity is None:
            log.warning(
                "Carbon Theater EGAM rejected: peer=%s:%d tid=%s reason=NOT_AUTHENTICATED",
                connection.peer_ip,
                connection.peer_port,
                transaction_id,
            )
            return [self._reply("EGAM", {"TID": transaction_id, "ERR": "NOT_AUTHENTICATED"})]
        gid = fields.get("GID", "") or connection.selected_gid
        game = self.games.get(gid)
        requested_uid = fields.get("R-UID", fields.get("UID", ""))
        internal_ip = fields.get("R-INT-IP", fields.get("INT-IP", "0.0.0.0"))
        internal_port = _positive_port(
            fields.get("R-INT-PORT", fields.get("PORT", "0"))
        )
        requested_participant = None
        if game is not None and requested_uid:
            try:
                requested_player_id = int(requested_uid)
            except ValueError:
                requested_player_id = -1
            requested_participant = next(
                (
                    participant
                    for participant in game.participants.values()
                    if participant.player_id == requested_player_id
                ),
                None,
            )
        log.info(
            "Carbon Theater EGAM request: persona=%s tid=%s gid=%s int=%s:%s "
            "requested_uid=%s resolved_remote=%s",
            connection.identity.persona,
            transaction_id,
            gid or "<missing>",
            internal_ip,
            internal_port,
            requested_uid or "-",
            (
                f"{requested_participant.identity.persona}:{requested_participant.player_id}"
                if requested_participant is not None
                else "-"
            ),
        )
        if game is None:
            log.warning(
                "Carbon Theater EGAM rejected: persona=%s tid=%s gid=%s reason=GAME_NOT_FOUND",
                connection.identity.persona,
                transaction_id,
                gid or "<missing>",
            )
            return [self._reply("EGAM", {"TID": transaction_id, "LID": "257", "GID": gid, "ERR": "GAME_NOT_FOUND"})]
        invite_entry = (
            connection.announced_gdet_gid == gid
            and bool(connection.invite_host_persona)
        )
        participant = self.games.enter(
            gid,
            connection.identity,
            internal_ip=internal_ip,
            internal_port=internal_port,
            invite_remote_player_id=(
                requested_participant.player_id
                if requested_participant is not None
                and connection.announced_gdet_gid == gid
                else 0
            ),
            invite_entry=invite_entry,
        )
        if participant is None:
            log.warning(
                "Carbon Theater EGAM rejected: persona=%s tid=%s gid=%s reason=GAME_FULL players=%d/%d",
                connection.identity.persona,
                transaction_id,
                gid,
                len(game.participants),
                game.session.capacity,
            )
            return [self._reply("EGAM", {"TID": transaction_id, "LID": game.lobby_id, "GID": gid, "ERR": "GAME_FULL"})]
        connection.pending_invite_completion_gid = gid if invite_entry else ""
        ticket = self.games.ticket(game, participant)
        race_endpoint = self.games.race_endpoint_for(
            participant.internal_ip,
            connection.peer_ip,
        )
        log.info(
            "Carbon Theater EGAM entered: persona=%s tid=%s gid=%s pid=%d ticket=%s host=%s host_id=%d players=%d/%d endpoint=%s:%d endpoint_scope=%s",
            connection.identity.persona,
            transaction_id,
            gid,
            participant.player_id,
            ticket,
            game.host.persona,
            game.host.user_id,
            len(game.participants),
            game.session.capacity,
            race_endpoint.host,
            race_endpoint.port,
            "local" if race_endpoint != self.games.race_endpoint else "public",
        )
        replies: list[FESLFrame] = []
        if connection.announced_gdet_gid != gid:
            # Normal PlayNow/CGAM receives GDET at entry. Invite entry has
            # already received it after GDAT USER=<host>, so do not send a
            # duplicate. Retail GDET and EGEG both use UGID.
            replies.append(
                self._reply(
                    "GDET",
                    {"UGID": game.ugid, "LID": game.lobby_id, "GID": gid},
                )
            )
        replies.extend(
            [
                self._reply("EGAM", {"TID": transaction_id, "LID": game.lobby_id, "GID": gid}),
                self._reply(
                "EGEG",
                {
                    "PL": "PC",
                    "TICKET": ticket,
                    "PID": str(participant.player_id),
                    "P": str(race_endpoint.port),
                    # HUID must identify the actual game host.  Dedicated
                    # PlayNow rooms use nfsdevserver (799270239), while a
                    # normal CGAM room must use the creating player's ID.
                    # Using one global dedicated HUID for both paths makes
                    # GDAT.HU and EGEG.HUID disagree and Carbon abandons the
                    # room during entry.
                    "HUID": str(game.host.user_id),
                    "INT-PORT": str(race_endpoint.port),
                    "INT-IP": race_endpoint.host,
                    "UGID": game.ugid,
                    "I": race_endpoint.host,
                    "LID": game.lobby_id,
                    "GID": gid,
                    "EKEY": self.games.ekey,
                },
                ),
            ]
        )
        connection.announced_gdet_gid = ""
        connection.invite_host_persona = ""
        return replies

    def _dispatch_leave_game(
        self,
        command: str,
        fields: dict[str, str],
        transaction_id: str,
        connection: TheaterConnection,
    ) -> list[FESLFrame]:
        gid = fields.get("GID", "")
        if connection.identity is not None:
            user_id = int(connection.identity.user_id)
            # During an active race Carbon may send ECNL while it changes
            # frontend/result state.  It is not reliable proof that the
            # UDP racer has abandoned the event.  Give the race transport
            # first refusal so it can defer removal until the result
            # boundary; otherwise PlayerLeft tears down the remaining
            # peer's ProtoTunnel stream mid-finish.
            deferred = bool(
                self.leave_handler(str(gid), user_id)
                if self.leave_handler is not None
                else False
            )
            removed = (
                False
                if deferred
                else self.games.leave(
                    gid,
                    user_id,
                    reason="theater-ecnl",
                )
            )
            log.info(
                "Carbon Theater ECNL: persona=%s tid=%s gid=%s removed=%s deferred=%s",
                connection.identity.persona,
                transaction_id,
                gid or "<missing>",
                int(removed),
                int(deferred),
            )
        return [
            self._reply(
                "ECNL",
                {
                    "TID": transaction_id,
                    "LID": fields.get("LID", "257") or "257",
                    "GID": gid,
                },
            )
        ]

    @staticmethod
    def _reply(command: str, fields: dict[str, object]) -> FESLFrame:
        # Theater transactions use TID fields and a zero outer transaction word.
        return FESLFrame.from_fields(command, fields, transaction=0)


def _positive_port(value: object) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return port if 0 <= port <= 65_535 else 0
