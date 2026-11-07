"""Experimental one-client Xbox-to-password FESL gateway over loopback.

The game supplies no password in XBL360Login. This gateway authenticates the
user's existing Carbon account and maps only this connection's Xbox lookup to
that authenticated session. It does not prove or persist XUID ownership.
Both TCP endpoints are loopback-only; remote use requires a separate secured
tunnel to a compatible server. Do not use with an untrusted local environment.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import getpass
import re
import select
import socket
import sys
import warnings

from carbon.fesl.frame import FESLFrame, FESLFrameError, FESLStreamDecoder

_XUID = re.compile(r"[A-Za-z0-9$+_-]{1,64}\Z")


class GatewayError(ValueError):
    pass


@dataclass(frozen=True)
class _Action:
    destination: str
    frame: FESLFrame = field(repr=False)


class XboxPasswordBridge:
    """Translate one local client's login without trusting its XUID."""

    def __init__(self, account: str, password: str, *, local_services: tuple[int, int] | None = None,
                 on_session=None) -> None:
        for value in (account, password):
            if not value or len(value) > 256 or any(ch in value for ch in "\r\n\t\x00"):
                raise GatewayError("invalid account or password format")
            try:
                value.encode("latin-1")
            except UnicodeEncodeError as exc:
                raise GatewayError("account or password is not supported by FESL") from exc
        self._account = account
        self._password: str | None = password
        self._pending: tuple[int, str] | None = None
        self._xuid: str | None = None
        self._login_reply: FESLFrame | None = None
        self._logged_in = False
        if local_services is not None and (len(local_services) != 2 or
                any(not isinstance(port, int) or not 1024 <= port <= 65535 for port in local_services)):
            raise GatewayError('invalid local service ports')
        self._local_services = local_services
        self._on_session = on_session
        self._session_cleanup = None

    @staticmethod
    def _fields(transaction: int, fields: dict[str, str]) -> FESLFrame:
        return FESLFrame.from_fields("acct", fields, transaction=transaction)

    def client_frame(self, frame: FESLFrame) -> tuple[_Action, ...]:
        if frame.command != "acct":
            return (_Action("upstream", frame),)
        fields = frame.fields
        op = fields.get("TXN", "")
        if op == "GetUserIdsByXuids":
            count = fields.get("xuids.[]", "0")
            if count == "0":
                return (_Action("client", self._fields(frame.transaction, {"TXN": op, "userIds.[]": "0"})),)
            xuid = fields.get("xuids.0", "")
            if count != "1" or not _XUID.fullmatch(xuid):
                raise GatewayError("unsupported Xbox identity lookup")
            if self._login_reply is not None:
                if xuid != self._xuid:
                    return (_Action("client", self._fields(frame.transaction, {"TXN": op, "userIds.[]": "0"})),)
                return (_Action("client", self._lookup_reply(frame.transaction, xuid)),)
            if self._pending is not None or self._password is None:
                raise GatewayError("overlapping Xbox identity lookup")
            self._pending = (frame.transaction, xuid)
            login = self._fields(frame.transaction, {
                "TXN": "Login", "name": self._account, "password": self._password,
                "clientPlatform": "xbox360",
            })
            self._password = None
            return (_Action("upstream", login),)
        if op == "XBL360Login":
            if self._login_reply is None or fields.get("xuid") != self._xuid or self._logged_in:
                raise GatewayError("Xbox login does not match authenticated local session")
            result = dict(self._login_reply.fields)
            result.pop("clientPlatform", None)
            result["TXN"] = "XBL360Login"
            self._logged_in = True
            return (_Action("client", self._fields(frame.transaction, result)),)
        if op == "XBL360UpdateAccount":
            if not self._logged_in or fields.get("xuid") != self._xuid:
                raise GatewayError("Xbox account update is not bound to the session")
            # The password-authenticated account already exists. Do not store
            # contact information from the unrelated Xbox profile.
            return (_Action("client", self._fields(frame.transaction, {"TXN": op})),)
        if op in {"Login", "AddAccount", "XBL360AddAccount"}:
            raise GatewayError("unexpected account operation")
        return (_Action("upstream", frame),)

    def upstream_frame(self, frame: FESLFrame) -> tuple[_Action, ...]:
        if (self._local_services is not None and frame.command == 'fsys'
                and frame.fields.get('TXN') == 'Hello'):
            fields = dict(frame.fields)
            fields.update(theaterIp='127.0.0.1', theaterPort=str(self._local_services[0]),
                          messengerIp='127.0.0.1', messengerPort=str(self._local_services[1]))
            return (_Action('client', FESLFrame.from_fields('fsys', fields, transaction=frame.transaction)),)
        if self._pending is None or frame.command != "acct" or frame.fields.get("TXN") != "Login":
            return (_Action("client", frame),)
        transaction, xuid = self._pending
        if frame.transaction != transaction:
            raise GatewayError("account login response transaction mismatch")
        fields = frame.fields
        try:
            user_id = int(fields.get("userId", ""))
        except ValueError as exc:
            raise GatewayError("account authentication failed") from exc
        if (not 0 < user_id <= 0xFFFFFFFF or not fields.get("lkey")
                or fields.get("clientPlatform") != "xbox360"):
            raise GatewayError("account authentication or Xbox platform negotiation failed")
        if self._on_session is not None:
            try:
                self._session_cleanup = self._on_session(fields['lkey'])
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                raise GatewayError('secure race transport unavailable') from exc
        self._pending = None
        self._xuid = xuid
        self._login_reply = frame
        return (_Action("client", self._lookup_reply(transaction, xuid)),)

    def _lookup_reply(self, transaction: int, xuid: str) -> FESLFrame:
        assert self._login_reply is not None
        login = self._login_reply.fields
        user_id = login["userId"]
        name = login.get("displayName", login.get("name", ""))
        if any(ch in name for ch in "\r\n\t\x00"):
            raise GatewayError("invalid authenticated persona")
        return self._fields(transaction, {
            "TXN": "GetUserIdsByXuids", "xuids.[]": "1", "xuids.0": xuid,
            "userIds.[]": "1", "userIds.0": user_id, "userInfo.[]": "1",
            "userInfo.0.xuid": xuid, "userInfo.0.userId": user_id,
            "userInfo.0.userName": name, f"userIds.{{{xuid}}}.userId": user_id,
        })

    def close(self) -> None:
        self._password = None
        self._login_reply = None
        self._pending = None
        cleanup, self._session_cleanup = self._session_cleanup, None
        if cleanup is not None:
            cleanup()


def serve_once(listen_port: int, upstream_port: int, account: str, password: str,
               *, local_services: tuple[int, int] | None = None, on_session=None,
               on_listening=None) -> None:
    if not 1 <= listen_port <= 65535 or not 1 <= upstream_port <= 65535 or listen_port == upstream_port:
        raise GatewayError("invalid local port")
    bridge = XboxPasswordBridge(account, password, local_services=local_services, on_session=on_session)
    del password
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", listen_port))
            listener.listen(1)
            if on_listening is not None:
                on_listening()
            print("Local bridge is waiting for the game.", flush=True)
            client, _ = listener.accept()
            with client, socket.create_connection(("127.0.0.1", upstream_port), timeout=5) as upstream:
                client.settimeout(5)
                buffers = {client: FESLStreamDecoder(), upstream: FESLStreamDecoder()}
                while True:
                    readable, _, _ = select.select([client, upstream], [], [], 300)
                    if not readable:
                        raise GatewayError("connection timed out")
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        for packet in buffers[source].feed(data):
                            actions = bridge.client_frame(packet) if source is client else bridge.upstream_frame(packet)
                            for action in actions:
                                (client if action.destination == "client" else upstream).sendall(action.frame.encode())
    finally:
        bridge.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Experimental local Carbon Xbox bridge")
    parser.add_argument("--listen-port", type=int, default=18221)
    parser.add_argument("--upstream-port", type=int, default=18230)
    parser.add_argument('--theater-local-port', type=int)
    parser.add_argument('--messenger-local-port', type=int)
    parser.add_argument('--race-url')
    parser.add_argument('--race-listen-port', type=int)
    parser.add_argument('--game-udp-port', type=int)
    args = parser.parse_args()
    try:
        if not sys.stdin.isatty():
            raise GatewayError("an interactive terminal is required")
        account = input("Carbon account: ").strip()
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Password: ")
        if (args.theater_local_port is None) != (args.messenger_local_port is None):
            raise GatewayError('both local service ports are required')
        local_services = ((args.theater_local_port, args.messenger_local_port)
                          if args.theater_local_port is not None else None)
        race_values = (args.race_url, args.race_listen_port, args.game_udp_port)
        if any(value is not None for value in race_values) and (
                not all(value is not None for value in race_values) or local_services is None):
            raise GatewayError('race transport and both local services must be configured together')
        on_session = None
        if args.race_url is not None:
            from carbon.race_client import OriginalRaceClient
            def on_session(token):
                race = OriginalRaceClient(args.race_url, token, listen_port=args.race_listen_port,
                                          client_port=args.game_udp_port)
                race.start()
                return race.close
        serve_once(args.listen_port, args.upstream_port, account, password,
                   local_services=local_services, on_session=on_session)
    except (GatewayError, FESLFrameError, OSError, EOFError, getpass.GetPassWarning) as exc:
        print(f"Bridge stopped: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
