"""Carbon dialect coverage for the shared U2/MW/Carbon Messenger hub."""

from __future__ import annotations

from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from common.accounts import SQLiteAccountDatabase, SQLiteSessionRegistry
from common.enforcement import (
    AccountPolicyEvent,
    LiveAccountConnectionRegistry,
)
from carbon.accounts.sqlite_backend import SQLiteCredentialStore, SQLiteIdentityStore
from carbon.core.config import Endpoint as CarbonEndpoint
from carbon.fesl.frame import FESLFrame as CarbonFESLFrame
from carbon.fesl.service import CarbonEndpoints, CarbonFESLService, FESLConnection
from carbon.messenger_ipc import CarbonMessengerIPCPublisher
from carbon.theater.directory import CarbonGameDirectory
from classic.core.catalog import GameId
from classic.ea.messenger import (
    EAMessengerFrame,
    EAMessengerHub,
    EAMessengerStreamDecoder,
)
from classic.ea.multiplex import ClassicEndpointMultiplexer
from classic.ea.social import SocialService
from classic.protocols.carbon_messenger import CarbonMessengerAdapter
from classic.protocols.carbon_messenger_service import RETAIL_ASYNC_COMMANDS, RETAIL_REQUEST_HANDLERS
from classic.protocols.carbon_messenger_ipc import (
    CarbonIPCIdentity,
    CarbonMessengerIPCState,
)
from classic.protocols.control import ClassicControlProfile, ClassicControlService
from classic.protocols.messenger import ClassicMessengerAdapter


class ManualClock:
    def __init__(self, value: float = 1000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def bridge_payload() -> dict[str, object]:
    return {
        "version": 1,
        "game": "carbon",
        "kind": "snapshot",
        "instance_id": "carbon-test-instance",
        "sessions": {
            "driver-key.": {
                "account_name": "driver",
                "persona": "Driver",
                "profile_id": 101,
                "user_id": 101,
                "wire_player_id": 1101,
            },
            "guest-key.": {
                "account_name": "guest",
                "persona": "Guest",
                "profile_id": 202,
                "user_id": 202,
                "wire_player_id": 1202,
            },
        },
        "known_identities": [
            {
                "account_name": "driver",
                "persona": "Driver",
                "profile_id": 101,
                "user_id": 101,
                "wire_player_id": 1101,
            },
            {
                "account_name": "guest",
                "persona": "Guest",
                "profile_id": 202,
                "user_id": 202,
                "wire_player_id": 1202,
            },
        ],
        "rooms": {
            "driver": {
                "persona": "Driver",
                "session_id": "carbon-room-1",
                "inviteable": True,
                "details": {
                    "game_type": "2",
                    "game_mode": "1",
                    "car_tier": "2",
                    "track": "cs.8.1",
                    "AP": "2",
                    "MP": "8",
                },
            },
            "guest": {
                "persona": "Guest",
                "session_id": "carbon-room-1",
                "inviteable": True,
                "invite_join_complete": False,
                "details": {"game_mode": "1", "AP": "2", "MP": "8"},
            },
        },
    }


class CarbonSharedMessengerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = ManualClock()
        self.state = CarbonMessengerIPCState(max_age_seconds=5, clock=self.clock)
        self.state.apply(bridge_payload())
        self.adapter = CarbonMessengerAdapter(
            self.state,
            heartbeat_interval=30,
            auth_ipc_wait=0.25,
        )

    @staticmethod
    def auth(token: str) -> EAMessengerFrame:
        return EAMessengerFrame.from_fields(
            "AUTH",
            {
                "LKEY": token,
                "PROD": "America",
                "VERS": "2.0",
                "PRES": "nfs-pc",
                "RSRC": "/EAGAMES/NFS-2007",
                "ID": "1",
            },
            transaction=0,
        )

    def test_dialect_selection_does_not_claim_u2_auth(self) -> None:
        carbon = self.auth("driver-key.")
        self.assertTrue(self.adapter.matches(carbon, ("127.0.0.1", 1000)))
        u2 = EAMessengerFrame.from_fields(
            "AUTH",
            {"LKEY": "stock-key", "PRES": "1", "PROD": "NFS-CONSOLE-2005"},
            transaction=0,
        )
        self.assertFalse(self.adapter.matches(u2, ("127.0.0.1", 1001)))
        social = SocialService()
        social.register_lobby(
            "mw-lobby",
            "mw",
            "MwDriver",
            "127.0.0.1",
            game_id=GameId.MOST_WANTED.value,
        )
        mw = ClassicMessengerAdapter(
            ClassicControlService(
                social,
                profile=ClassicControlProfile.for_game(GameId.MOST_WANTED),
            ),
            GameId.MOST_WANTED,
        )
        hub = EAMessengerHub([mw, self.adapter])
        self.assertIs(self.adapter, hub._select(carbon, ("127.0.0.1", 1000)))

    def test_active_session_ignores_duplicate_forced_logoff_marker(self) -> None:
        pushed: list[bytes] = []
        context = self.adapter.open(
            ("127.0.0.1", 1999),
            lambda wire: pushed.append(wire) or True,
            now=10.0,
        )
        self.adapter.dispatch(self.auth("driver-key."), context, now=10.0)
        replacement = bridge_payload()
        duplicate = dict(replacement["sessions"]["driver-key."])
        replacement["forced_logoffs"] = {
            "duplicate-key.": {
                **duplicate,
                "reason": "DUPL",
                "theater_ready": False,
            },
        }
        self.state.apply(replacement)

        wires = self.adapter.poll(context, now=10.1)

        self.assertEqual(wires, [])
        self.assertFalse(context.connection.close_requested)
        self.assertTrue(context.connection.authenticated)

    def test_duplicate_disconnect_preserves_winner_invite_state(self) -> None:
        winner = self.adapter.open(
            ("127.0.0.1", 1998),
            lambda _wire: True,
            now=19.0,
        )
        self.adapter.dispatch(self.auth("driver-key."), winner, now=19.0)
        self.adapter.service._pending_invite_completions["guest"] = (
            "driver",
            "carbon-room-1",
        )
        self.adapter.service._pending_invite_revokes.add("guest")

        replacement = bridge_payload()
        duplicate = dict(replacement["sessions"]["driver-key."])
        replacement["forced_logoffs"] = {
            "duplicate-key.": {**duplicate, "reason": "DUPL"},
        }
        self.state.apply(replacement)
        newcomer = self.adapter.open(
            ("127.0.0.1", 1999),
            lambda _wire: True,
            now=19.1,
        )
        self.adapter.dispatch(self.auth("duplicate-key."), newcomer, now=19.1)

        self.adapter.close(newcomer)

        self.assertIn(
            winner.connection,
            self.adapter.service._connections["driver"],
        )
        self.assertEqual(
            self.adapter.service._pending_invite_completions["guest"],
            ("driver", "carbon-room-1"),
        )
        self.assertIn("guest", self.adapter.service._pending_invite_revokes)
        self.assertTrue(winner.connection.authenticated)

    def test_duplicate_messenger_waits_for_pset_and_theater_glst(self) -> None:
        replacement = bridge_payload()
        duplicate = dict(replacement["sessions"]["driver-key."])
        replacement["forced_logoffs"] = {
            "duplicate-key.": {**duplicate, "reason": "DUPL"},
        }
        self.state.apply(replacement)
        context = self.adapter.open(
            ("127.0.0.1", 2000),
            lambda _wire: True,
            now=20.0,
        )

        auth_wires = self.adapter.dispatch(
            self.auth("duplicate-key."), context, now=20.0
        )

        auth_frame = EAMessengerStreamDecoder().feed(auth_wires[0])[0]
        self.assertEqual(auth_frame.command, "AUTH")
        self.assertEqual(auth_frame.word, 0)
        self.assertEqual(auth_frame.fields["ID"], "1")
        self.assertNotIn("ERR", auth_frame.fields)
        self.assertEqual(
            auth_frame.payload,
            b'TIID=0\nTITL="Need for Speed Carbon"\n'
            b'USER=Driver@messaging.ea.com/eagames/NFS-2007\nID=1\n\x00',
        )
        self.assertTrue(context.connection.authenticated)
        self.assertEqual(self.adapter.poll(context, now=20.1), [])
        self.assertFalse(context.connection.close_requested)

        bootstrap = (
            ("RGET", {"LIST": "B", "ID": "2"}, 20.2),
            ("RGET", {"LIST": "I", "ID": "3"}, 20.3),
            ("EPGT", {"ID": "4"}, 20.4),
        )
        for command, fields, now in bootstrap:
            bootstrap_wires = self.adapter.dispatch(
                EAMessengerFrame.from_fields(command, fields, transaction=0),
                context,
                now=now,
            )
            bootstrap_frames = [
                EAMessengerStreamDecoder().feed(wire)[0]
                for wire in bootstrap_wires
            ]
            self.assertEqual(
                [frame.command for frame in bootstrap_frames],
                [command],
            )
            self.assertFalse(context.connection.forced_logoff_notice_sent)

        pset_wires = self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "PSET", {"SHOW": "CHAT", "ID": "5"}, transaction=0
            ),
            context,
            now=20.5,
        )
        pset_frame = EAMessengerStreamDecoder().feed(pset_wires[0])[0]
        self.assertEqual(pset_frame.command, "PSET")
        self.assertEqual(pset_frame.fields, {"ID": "5"})
        self.assertFalse(context.connection.forced_logoff_notice_sent)

        # PSET alone is too early: the retail client sends it before Theater
        # GLST finishes.  No ADMN is released until Carbon publishes that
        # cross-process write barrier.
        self.adapter.after_send(context)
        self.assertEqual(self.adapter.poll(context, now=20.6), [])
        replacement["forced_logoffs"]["duplicate-key."]["theater_ready"] = True
        self.state.apply(replacement)
        self.assertEqual(self.adapter.poll(context, now=20.7), [])
        self.assertEqual(self.adapter.poll(context, now=21.6), [])
        wires = self.adapter.poll(context, now=21.7)
        frames = [EAMessengerStreamDecoder().feed(wire)[0] for wire in wires]
        self.assertEqual([frame.command for frame in frames], ["ADMN"])
        self.assertEqual(frames[0].word, 0x80000000)
        self.assertEqual(frames[0].fields, {"TYPE": "DUPL", "SECS": "0"})
        self.assertEqual(frames[0].fields["TYPE"], "DUPL")
        self.assertEqual(
            wires[0].hex(),
            "41444d4e800000000000001e"
            "545950453d4455504c0a534543533d300a00",
        )
        self.assertEqual(self.adapter.poll(context, now=21.8), [])
        self.assertFalse(context.connection.close_requested)
        self.assertEqual(self.adapter.poll(context, now=23.8), [])
        self.assertTrue(context.connection.close_requested)
        self.assertIsNotNone(self.state.resolve_session("driver-key."))

    def test_fesl_duplicate_pipeline_reaches_messenger_as_admn_dupl(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(
                root / "accounts.sqlite3",
                root / "users",
            )
            credentials = SQLiteCredentialStore(database)
            credentials.create_account("Driver", "secret", persona="RaceDriver")
            tokens = iter(("active-key.", "duplicate-key."))
            identities = SQLiteIdentityStore(
                database,
                token_factory=lambda: next(tokens),
            )
            sessions = SQLiteSessionRegistry(
                database,
                game="carbon",
                server_id="carbon-duplicate-test",
                lease_seconds=120,
            )
            fesl = CarbonFESLService(
                CarbonEndpoints("127.0.0.1", 13505, "127.0.0.1", 18215),
                identities,
                credentials=credentials,
                authentication_mode="password",
                active_sessions=sessions,
            )
            first = FESLConnection(connection_id="carbon-first")
            second = FESLConnection(connection_id="carbon-second")
            login_fields = {
                "TXN": "Login",
                "name": "Driver",
                "password": "secret",
            }
            active_reply = fesl.dispatch(
                CarbonFESLFrame.from_fields(
                    "acct",
                    login_fields,
                    transaction=1,
                ),
                first,
            )[0]
            publisher = CarbonMessengerIPCPublisher(
                CarbonEndpoint("127.0.0.1", 13506),
                secret="test-secret",
                identities=identities,
                games=CarbonGameDirectory(
                    CarbonEndpoint("127.0.0.1", 19119),
                    player_id_resolver=identities.wire_player_id,
                ),
            )
            payload = publisher.snapshot()
            payload.update(
                {
                    "kind": "snapshot",
                    "instance_id": "carbon-duplicate-test",
                }
            )
            state = CarbonMessengerIPCState(
                max_age_seconds=5,
                clock=lambda: 20.0,
            )
            state.apply(payload)
            adapter = CarbonMessengerAdapter(
                state,
                heartbeat_interval=30,
                auth_ipc_wait=0.25,
            )
            displaced = adapter.open(
                ("127.0.0.1", 2001),
                lambda _wire: True,
                now=20.0,
            )
            displaced_auth = adapter.dispatch(
                self.auth(active_reply.fields["lkey"]),
                displaced,
                now=20.0,
            )
            self.assertEqual(
                EAMessengerStreamDecoder().feed(displaced_auth[0])[0].command,
                "AUTH",
            )

            duplicate_reply = fesl.dispatch(
                CarbonFESLFrame.from_fields(
                    "acct",
                    login_fields,
                    transaction=2,
                ),
                second,
            )[0]
            takeover_payload = publisher.snapshot()
            takeover_payload.update(
                {
                    "kind": "snapshot",
                    "instance_id": "carbon-duplicate-test",
                }
            )
            state.apply(takeover_payload)

            wires = adapter.poll(displaced, now=20.1)
            frames = [EAMessengerStreamDecoder().feed(wire)[0] for wire in wires]

            self.assertEqual(active_reply.fields["lkey"], "active-key.")
            self.assertEqual(duplicate_reply.fields["lkey"], "duplicate-key.")
            self.assertNotIn("errorCode", duplicate_reply.fields)
            self.assertEqual([frame.command for frame in frames], ["ADMN"])
            self.assertEqual(frames[0].word, 0x80000000)
            self.assertEqual(frames[0].fields, {"TYPE": "DUPL", "SECS": "0"})
            self.assertFalse(displaced.connection.close_requested)
            self.assertIsNone(state.resolve_session("active-key."))
            self.assertIsNotNone(state.forced_logoff("active-key."))
            self.assertIsNotNone(state.resolve_session("duplicate-key."))

            newcomer = adapter.open(
                ("127.0.0.1", 2002),
                lambda _wire: True,
                now=20.2,
            )
            newcomer_wires = adapter.dispatch(
                self.auth(duplicate_reply.fields["lkey"]),
                newcomer,
                now=20.2,
            )
            newcomer_auth = EAMessengerStreamDecoder().feed(newcomer_wires[0])[0]
            self.assertEqual(newcomer_auth.command, "AUTH")
            self.assertNotIn("ERR", newcomer_auth.fields)
            self.assertTrue(newcomer.connection.authenticated)
            self.assertEqual(adapter.poll(displaced, now=22.2), [])
            self.assertTrue(displaced.connection.close_requested)
            self.assertEqual(
                sessions.session_for("Driver").connection_id,
                "carbon-second",
            )

    def test_live_carbon_policy_close_drains_through_buffered_multiplexer(self) -> None:
        registry = LiveAccountConnectionRegistry(name="messenger-policy-test")
        hub = EAMessengerHub(
            [self.adapter],
            connection_timeout=3.0,
            poll_interval=0.05,
            live_connections=registry,
        )

        def unexpected_web(*_args) -> None:
            raise AssertionError("EA Messenger AUTH was routed to the web handler")

        multiplexer = ClassicEndpointMultiplexer(
            hub.handle_connection,
            unexpected_web,
            sniff_timeout=0.05,
        )
        server, client = socket.socketpair()
        stop_event = Event()

        def run_server() -> None:
            try:
                multiplexer.handle_connection(
                    server,
                    ("127.0.0.1", 4500),
                    stop_event,
                )
            finally:
                server.close()

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        try:
            client.settimeout(2.0)
            client.sendall(self.auth("driver-key.").encode())
            decoder = EAMessengerStreamDecoder()
            frames: list[EAMessengerFrame] = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                frame.command == "AUTH" for frame in frames
            ):
                frames.extend(decoder.feed(client.recv(8192)))
            self.assertTrue(any(frame.command == "AUTH" for frame in frames))
            self.assertEqual(len(registry), 1)

            result = registry.enforce(
                AccountPolicyEvent(1, 1, "driver", "ban", 1.0)
            )
            self.assertEqual(result.matched, 1)
            self.assertEqual(result.notified, 0)
            self.assertEqual(result.closing, 1)

            client.settimeout(0.25)
            with self.assertRaises(socket.timeout):
                client.recv(8192)

            client.settimeout(3.0)
            try:
                closed = client.recv(8192)
            except ConnectionResetError:
                closed = b""
            self.assertEqual(closed, b"")
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(registry), 0)
        finally:
            stop_event.set()
            client.close()
            server.close()
            thread.join(timeout=1.0)

    def test_live_carbon_kick_delivers_native_boot_before_close(self) -> None:
        registry = LiveAccountConnectionRegistry(name="messenger-kick-test")
        hub = EAMessengerHub(
            [self.adapter],
            connection_timeout=3.0,
            poll_interval=0.05,
            live_connections=registry,
        )
        multiplexer = ClassicEndpointMultiplexer(
            hub.handle_connection,
            lambda *_args: self.fail("AUTH was routed to the web handler"),
            sniff_timeout=0.05,
        )
        server, client = socket.socketpair()
        stop_event = Event()

        def run_server() -> None:
            try:
                multiplexer.handle_connection(server, ("127.0.0.1", 4501), stop_event)
            finally:
                server.close()

        thread = Thread(target=run_server, daemon=True)
        thread.start()
        try:
            client.settimeout(2.0)
            client.sendall(self.auth("driver-key.").encode())
            decoder = EAMessengerStreamDecoder()
            frames: list[EAMessengerFrame] = []
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not any(
                frame.command == "AUTH" for frame in frames
            ):
                frames.extend(decoder.feed(client.recv(8192)))
            self.assertEqual(len(registry), 1)
            self.assertTrue(any(frame.command == "AUTH" for frame in frames))

            result = registry.enforce(
                AccountPolicyEvent(2, 1, "driver", "kick", 2.0)
            )
            self.assertEqual((result.matched, result.notified, result.closing), (1, 1, 1))
            boot = decoder.feed(client.recv(8192))
            self.assertEqual(len(boot), 1)
            self.assertEqual(boot[0].command, "ADMN")
            self.assertEqual(boot[0].word, 0x80000000)
            self.assertEqual(boot[0].fields, {"TYPE": "BOOT", "SECS": "0"})

            client.settimeout(0.25)
            with self.assertRaises(socket.timeout):
                client.recv(8192)
            client.settimeout(3.0)
            try:
                closed = client.recv(8192)
            except ConnectionResetError:
                closed = b""
            self.assertEqual(closed, b"")
            thread.join(timeout=1.0)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(registry), 0)
        finally:
            stop_event.set()
            client.close()
            server.close()
            thread.join(timeout=1.0)

    def test_auth_roster_presence_and_invite_use_carbon_wire_shape(self) -> None:
        driver_push: list[bytes] = []
        guest_push: list[bytes] = []
        now = 50.0
        guest = self.adapter.open(
            ("127.0.0.1", 2001),
            lambda wire: guest_push.append(wire) or True,
            now=now,
        )
        driver = self.adapter.open(
            ("127.0.0.1", 2002),
            lambda wire: driver_push.append(wire) or True,
            now=now,
        )

        guest_auth = self.adapter.dispatch(self.auth("guest-key."), guest, now=now)
        driver_auth = self.adapter.dispatch(self.auth("driver-key."), driver, now=now)
        self.assertEqual(len(guest_auth), 1)
        self.assertEqual(len(driver_auth), 1)
        auth_frame = self._decode(driver_auth[0])
        self.assertEqual(auth_frame.fields["USER"], "Driver@messaging.ea.com/eagames/NFS-2007")
        self.assertEqual(auth_frame.fields["TITL"], '"Need for Speed Carbon"')

        roster = self.adapter.dispatch(
            EAMessengerFrame.from_fields("RGET", {"LIST": "B", "ID": "2"}, transaction=0),
            driver,
            now=now,
        )
        decoded = [self._decode(wire) for wire in roster]
        self.assertEqual([frame.command for frame in decoded], ["RGET"])
        self.assertEqual(decoded[0].fields["SIZE"], "0")

        presence = self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "PSET",
                {"SHOW": "GAME", "STAT": '"en%3dPlaying Need for Speed Carbon"', "ID": "7"},
                transaction=0,
            ),
            driver,
            now=now,
        )
        self.assertEqual(self._decode(presence[0]).fields, {"ID": "7"})
        self.assertEqual(driver.connection.presence_attr, "J")

        invite = self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "GINV",
                {"USER": "Guest", "SESS": "0", "ID": "11"},
                transaction=0,
            ),
            driver,
            now=now,
        )
        self.assertEqual(self._decode(invite[0]).fields, {"ID": "11"})
        guest_push.extend(self.adapter.poll(guest, now=now + 0.1))
        delivered = [self._decode(wire) for wire in guest_push]
        gnot = [frame for frame in delivered if frame.command == "GNOT"][-1]
        self.assertEqual(gnot.word, 0x80000000)
        self.assertTrue(gnot.payload.endswith(b"\n\x00"))
        self.assertEqual(
            gnot.fields,
            {
                "HOST": "Driver",
                "USER": "Driver",
                "TYPE": "I",
                "SESS": "0",
                "GSTR": "Career Challenge - Silver - Circuit - cs.8.1",
            },
        )

        self.adapter.close(driver)
        self.adapter.close(guest)

    def test_invite_revoke_waits_for_theater_egeg_completion(self) -> None:
        driver_push: list[bytes] = []
        guest_push: list[bytes] = []
        guest = self.adapter.open(
            ("127.0.0.1", 2101),
            lambda wire: guest_push.append(wire) or True,
            now=50.0,
        )
        driver = self.adapter.open(
            ("127.0.0.1", 2102),
            lambda wire: driver_push.append(wire) or True,
            now=50.0,
        )
        self.adapter.dispatch(self.auth("guest-key."), guest, now=50.0)
        self.adapter.dispatch(self.auth("driver-key."), driver, now=50.0)

        self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "GRSP",
                {"USER": "Driver", "ANSW": "Y", "SESS": "0", "ID": "6"},
                transaction=0,
            ),
            guest,
            now=51.0,
        )
        self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "GRVK",
                {"USER": "Guest", "SESS": "0", "ID": "7"},
                transaction=0,
            ),
            driver,
            now=51.1,
        )
        self.adapter.after_send(driver)
        before = [self._decode(wire) for wire in guest_push]
        self.assertFalse(
            any(
                frame.command == "GNOT" and frame.fields.get("TYPE") == "R"
                for frame in before
            )
        )

        completed = bridge_payload()
        completed["rooms"]["guest"]["invite_join_complete"] = True
        self.state.apply(completed)
        self.adapter.poll(guest, now=51.2)
        after = [self._decode(wire) for wire in guest_push]
        revokes = [
            frame
            for frame in after
            if frame.command == "GNOT" and frame.fields.get("TYPE") == "R"
        ]
        self.assertEqual(len(revokes), 1)
        self.assertEqual(revokes[0].fields["HOST"], "Driver")

        self.adapter.close(driver)
        self.adapter.close(guest)

    def test_stale_ipc_state_rejects_session_after_grace_period(self) -> None:
        self.clock.value += 60
        adapter = CarbonMessengerAdapter(self.state, auth_ipc_wait=0.25)
        context = adapter.open(("127.0.0.1", 3000), lambda _wire: True, now=1.0)
        self.assertEqual(adapter.dispatch(self.auth("driver-key."), context, now=1.0), [])
        replies = adapter.poll(context, now=2.0)
        self.assertEqual(self._decode(replies[0]).fields["ERR"], "INVALID_SESSION")

    def test_shared_adapter_handles_bootstrap_presence_and_ping(self) -> None:
        context = self.adapter.open(
            ("127.0.0.1", 3001),
            lambda _wire: True,
            now=1.0,
        )
        auth = self.adapter.dispatch(self.auth("driver-key."), context, now=1.0)
        self.assertEqual(self._decode(auth[0]).command, "AUTH")
        for command, fields, expected in (
            ("RGET", {"ID": "2", "LIST": "B"}, {"ID": "2", "SIZE": "0"}),
            ("EPGT", {"ID": "4"}, {"ID": "4", "ENAB": "F", "ADDR": ""}),
            ("PSET", {"ID": "5", "SHOW": "CHAT"}, {"ID": "5"}),
            ("USCH", {"ID": "6", "USER": "Nobody"}, {"ID": "6", "SIZE": "0"}),
        ):
            replies = self.adapter.dispatch(
                EAMessengerFrame.from_fields(command, fields, transaction=0),
                context,
                now=2.0,
            )
            self.assertEqual(self._decode(replies[0]).fields, expected)
        self.assertEqual(
            self.adapter.dispatch(
                EAMessengerFrame.from_fields("PING", {}, transaction=0),
                context,
                now=3.0,
            ),
            [],
        )
        self.assertEqual(context.connection.ping_responses, 1)
        heartbeat = self.adapter.poll(context, now=40.0)
        self.assertEqual(self._decode(heartbeat[-1]).command, "PING")
        self.adapter.close(context)

    def test_shared_adapter_acknowledges_presence_delete(self) -> None:
        context = self.adapter.open(("127.0.0.1", 3002), lambda _wire: True, now=1.0)
        self.adapter.dispatch(self.auth("driver-key."), context, now=1.0)
        replies = self.adapter.dispatch(
            EAMessengerFrame.from_fields(
                "PDEL",
                {"ID": "13", "USER": "OtherDriver"},
                transaction=0,
            ),
            context,
            now=2.0,
        )
        self.assertEqual(
            self._decode(replies[0]).fields,
            {"ID": "13", "STAT": "OK", "RESULT": "OK"},
        )
        self.adapter.close(context)

    def test_sqlite_social_graph_limits_carbon_roster_to_real_friends(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            database.create_account("stranger", "pw", persona="Stranger")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )
            self.assertTrue(social.request_friend("Driver", "Guest").accepted)
            self.assertTrue(social.respond_friend("Guest", "Driver", True).accepted)

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state,
                social=social,
                identity_resolver=resolver,
                auth_ipc_wait=0.25,
            )
            guest_push: list[bytes] = []
            guest = adapter.open(
                ("127.0.0.1", 4101),
                lambda wire: guest_push.append(wire) or True,
                now=10.0,
            )
            driver = adapter.open(("127.0.0.1", 4102), lambda _wire: True, now=10.0)
            adapter.dispatch(self.auth("guest-key."), guest, now=10.0)
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)
            adapter.dispatch(
                EAMessengerFrame.from_fields("PSET", {"SHOW": "CHAT", "ID": "7"}, transaction=0),
                driver,
                now=10.0,
            )
            presence_pushes = [
                self._decode(wire)
                for wire in guest_push
                if self._decode(wire).command == "PGET"
            ]
            self.assertTrue(presence_pushes)
            self.assertEqual(
                presence_pushes[-1].fields["USER"],
                "Driver@messaging.ea.com/eagames/NFS-2007",
            )
            roster = adapter.dispatch(
                EAMessengerFrame.from_fields("RGET", {"LIST": "B", "ID": "5"}, transaction=0),
                driver,
                now=10.0,
            )
            decoded = [self._decode(wire) for wire in roster]
            roster_users = [frame.fields.get("USER", "") for frame in decoded if frame.command == "ROST"]
            self.assertEqual(roster_users, ["Guest@messaging.ea.com"])
            self.assertEqual(self._decode(roster[0]).fields["SIZE"], "1")
            adapter.close(driver)
            adapter.close(guest)

    def test_carbon_auth_keeps_presence_hidden_until_first_pset(self) -> None:
        social = SocialService()
        social.request_friend("Driver", "Guest")
        social.respond_friend("Guest", "Driver", True)
        adapter = CarbonMessengerAdapter(self.state, social=social)
        guest_push: list[bytes] = []
        guest = adapter.open(
            ("127.0.0.1", 4111),
            lambda wire: guest_push.append(wire) or True,
            now=10.0,
        )
        driver = adapter.open(("127.0.0.1", 4112), lambda _wire: True, now=10.0)
        self.addCleanup(adapter.close, guest)
        self.addCleanup(adapter.close, driver)
        adapter.dispatch(self.auth("guest-key."), guest, now=10.0)
        adapter.dispatch(
            EAMessengerFrame.from_fields("PSET", {"SHOW": "CHAT", "ID": "1"}, transaction=0),
            guest,
            now=10.0,
        )
        guest_push.clear()

        adapter.dispatch(self.auth("driver-key."), driver, now=10.0)
        self.assertFalse([self._decode(wire) for wire in guest_push
                          if self._decode(wire).command == "PGET"])
        roster = [self._decode(wire) for wire in adapter.dispatch(
            EAMessengerFrame.from_fields("RGET", {"LIST": "B", "ID": "2"}, transaction=0),
            guest,
            now=10.0,
        )]
        self.assertEqual([frame.command for frame in roster], ["RGET", "ROST"])
        self.assertEqual(roster[1].fields["ATTR"], "AT")
        subscription = [self._decode(wire) for wire in adapter.dispatch(
            EAMessengerFrame.from_fields("PADD", {"USER": "Driver", "ID": "3"}, transaction=0),
            guest,
            now=10.0,
        )]
        self.assertEqual([frame.command for frame in subscription], ["PADD", "PGET"])
        self.assertEqual(subscription[-1].fields["SHOW"], "DISC")

        adapter.dispatch(
            EAMessengerFrame.from_fields("PSET", {"SHOW": "CHAT", "ID": "4"}, transaction=0),
            driver,
            now=10.0,
        )
        updates = [self._decode(wire) for wire in guest_push
                   if self._decode(wire).command == "PGET"]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].fields["SHOW"], "CHAT")

    def test_offline_sqlite_friend_keeps_retail_carbon_at_attribute(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )
            social.request_friend("Driver", "Guest")
            social.respond_friend("Guest", "Driver", True)

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state,
                social=social,
                identity_resolver=resolver,
                auth_ipc_wait=0.25,
            )
            driver = adapter.open(("127.0.0.1", 4201), lambda _wire: True, now=10.0)
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)
            roster = adapter.dispatch(
                EAMessengerFrame.from_fields("RGET", {"LIST": "B", "ID": "6"}, transaction=0),
                driver,
                now=10.0,
            )
            decoded = [self._decode(wire) for wire in roster]
            self.assertEqual([frame.command for frame in decoded], ["RGET", "ROST"])
            self.assertEqual(decoded[1].fields["ATTR"], "AT")
            adapter.close(driver)

    def test_carbon_roster_splits_live_players_and_blocked_entries(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            database.create_account("blocked", "pw", persona="Blocked")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )
            social.set_blocked("Driver", "Blocked", True)

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state,
                social=social,
                identity_resolver=resolver,
                auth_ipc_wait=0.25,
            )
            guest = adapter.open(("127.0.0.1", 4301), lambda _wire: True, now=10.0)
            driver = adapter.open(("127.0.0.1", 4302), lambda _wire: True, now=10.0)
            adapter.dispatch(self.auth("guest-key."), guest, now=10.0)
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)

            player_roster = adapter.dispatch(
                EAMessengerFrame.from_fields("RGET", {"LIST": "B", "ID": "8"}, transaction=0),
                driver,
                now=10.0,
            )
            decoded = [self._decode(wire) for wire in player_roster]
            rows = [frame for frame in decoded if frame.command == "ROST"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].fields["USER"], "Guest@messaging.ea.com")
            self.assertEqual(rows[0].fields["ATTR"], "D")
            # Recent Players must remain available once the opponent leaves
            # and disconnects, including an explicitly requested player roster.
            adapter.close(guest)
            recent = adapter.dispatch(
                EAMessengerFrame.from_fields("RGET", {"LIST": "P", "ID": "recent"}, transaction=0),
                driver, now=10.0,
            )
            recent_rows = [self._decode(wire) for wire in recent if self._decode(wire).command == "ROST"]
            self.assertEqual([(frame.fields["USER"], frame.fields["ATTR"]) for frame in recent_rows],
                             [("Guest@messaging.ea.com", "D")])

            blocked_roster = adapter.dispatch(
                EAMessengerFrame.from_fields("RGET", {"LIST": "I", "ID": "9"}, transaction=0),
                driver,
                now=10.0,
            )
            blocked_decoded = [self._decode(wire) for wire in blocked_roster]
            blocked_rows = [frame for frame in blocked_decoded if frame.command == "ROST"]
            self.assertEqual(len(blocked_rows), 1)
            self.assertEqual(blocked_rows[0].fields["USER"], "Blocked@messaging.ea.com")
            self.assertEqual(blocked_rows[0].fields["ATTR"], "B")
            adapter.close(driver)
            adapter.close(guest)

    def test_carbon_usch_search_uses_sqlite_personas_and_capture_wire_shape(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            database.create_account("guest-two", "pw", persona="GuestTwo")
            database.create_account("blocked", "pw", persona="Blocked")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )
            self.assertTrue(social.set_blocked("Driver", "Blocked", True).accepted)

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state,
                social=social,
                identity_resolver=resolver,
                auth_ipc_wait=0.25,
            )
            driver = adapter.open(("127.0.0.1", 4401), lambda _wire: True, now=10.0)
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)

            exact = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "USCH",
                    {
                        "USER": "uEs",
                        "RSRC": "/eagames/NFS-2007",
                        "DIST": "F",
                        "MAXR": "5",
                        "ID": "7",
                    },
                    transaction=0,
                ),
                driver,
                now=10.0,
            )
            decoded = [self._decode(wire) for wire in exact]
            self.assertEqual([frame.command for frame in decoded], ["USCH", "USER", "USER"])
            self.assertEqual(decoded[0].fields, {"ID": "7", "SIZE": "2"})
            self.assertEqual(
                decoded[1].fields,
                {"ID": "7", "RSRC": "eagames/NFS-2007", "USER": "Guest"},
            )

            self.assertEqual(decoded[2].fields["USER"], "GuestTwo")

            wildcard = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "USCH", {"USER": "Guest*", "MAXR": "1", "ID": "8"}, transaction=0
                ),
                driver,
                now=10.0,
            )
            wildcard_decoded = [self._decode(wire) for wire in wildcard]
            self.assertEqual(wildcard_decoded[0].fields, {"ID": "8", "SIZE": "1"})
            self.assertEqual(wildcard_decoded[1].fields["USER"], "Guest")

            for query in ("Driver", "Blocked", "Missing"):
                empty = adapter.dispatch(
                    EAMessengerFrame.from_fields(
                        "USCH", {"USER": query, "MAXR": "5", "ID": "9"}, transaction=0
                    ),
                    driver,
                    now=10.0,
                )
                self.assertEqual(len(empty), 1)
                self.assertEqual(self._decode(empty[0]).fields, {"ID": "9", "SIZE": "0"})
            adapter.close(driver)

    def test_carbon_radm_rrsp_and_rdem_persist_friend_flow(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name,
                    record.persona,
                    record.profile_id,
                    record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state,
                social=social,
                identity_resolver=resolver,
                auth_ipc_wait=0.25,
            )
            driver_push: list[bytes] = []
            guest_push: list[bytes] = []
            driver = adapter.open(
                ("127.0.0.1", 4501),
                lambda wire: driver_push.append(wire) or True,
                now=10.0,
            )
            guest = adapter.open(
                ("127.0.0.1", 4502),
                lambda wire: guest_push.append(wire) or True,
                now=10.0,
            )
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)
            adapter.dispatch(self.auth("guest-key."), guest, now=10.0)
            driver_push.clear()
            guest_push.clear()

            request = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RADM",
                    {
                        "USER": "Guest",
                        "LRSC": "eagames",
                        "PRES": "Y",
                        "ID": "10",
                    },
                    transaction=0,
                ),
                driver,
                now=10.0,
            )
            request_reply = self._decode(request[0])
            self.assertEqual(request_reply.command, "RADM")
            self.assertEqual(
                request_reply.fields,
                {"ID": "10", "PRES": "Y", "LRSC": "eagames", "USER": "Guest"},
            )
            self.assertEqual(social.snapshot("Driver", "B")[0].request, "outgoing")
            self.assertEqual(social.snapshot("Guest", "B")[0].request, "incoming")
            guest_frames = [self._decode(wire) for wire in guest_push]
            self.assertTrue(
                any(
                    frame.command == "RNOT"
                    and frame.fields.get("ATTR") == "R"
                    and frame.fields.get("USER") == "Driver@messaging.ea.com"
                    for frame in guest_frames
                )
            )

            driver_push.clear()
            guest_push.clear()
            response = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RRSP",
                    {"USER": "Driver", "ANSW": "Y", "ID": "11"},
                    transaction=0,
                ),
                guest,
                now=10.1,
            )
            self.assertEqual(self._decode(response[0]).command, "RRSP")
            self.assertTrue(social.snapshot("Driver", "B")[0].friend)
            self.assertTrue(social.snapshot("Guest", "B")[0].friend)
            driver_frames = [self._decode(wire) for wire in driver_push]
            self.assertTrue(
                any(
                    frame.command == "RNOT"
                    and frame.fields.get("ATTR") == "AT"
                    and frame.fields.get("USER") == "Guest@messaging.ea.com"
                    for frame in driver_frames
                )
            )

            removed = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RDEM",
                    {"USER": "Guest", "LRSC": "eagames", "PRES": "Y", "ID": "12"},
                    transaction=0,
                ),
                driver,
                now=10.2,
            )
            self.assertEqual(self._decode(removed[0]).command, "RDEM")
            self.assertEqual(social.snapshot("Driver", "B"), ())
            self.assertEqual(social.snapshot("Guest", "B"), ())
            adapter.close(driver)
            adapter.close(guest)

    def test_carbon_friend_request_decline_and_block_commands(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            social = SocialService(
                database=database,
                persona_provider=lambda: tuple(item.persona for item in database.personas()),
            )

            def resolver(persona: str) -> CarbonIPCIdentity | None:
                record = database.identity_for_persona(persona, require_carbon_wire_id=True)
                if record is None:
                    return None
                return CarbonIPCIdentity(
                    record.account_name, record.persona, record.profile_id, record.user_id,
                    int(record.carbon_wire_player_id or 0),
                )

            adapter = CarbonMessengerAdapter(
                self.state, social=social, identity_resolver=resolver, auth_ipc_wait=0.25
            )
            driver = adapter.open(("127.0.0.1", 4601), lambda _wire: True, now=10.0)
            guest = adapter.open(("127.0.0.1", 4602), lambda _wire: True, now=10.0)
            adapter.dispatch(self.auth("driver-key."), driver, now=10.0)
            adapter.dispatch(self.auth("guest-key."), guest, now=10.0)
            adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RADM", {"USER": "Guest", "LRSC": "eagames", "PRES": "Y", "ID": "20"}, transaction=0
                ),
                driver, now=10.0,
            )
            declined = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RRSP", {"USER": "Driver", "ANSW": "N", "ID": "21"}, transaction=0
                ),
                guest, now=10.1,
            )
            self.assertEqual(self._decode(declined[0]).fields["ANSW"], "N")
            self.assertEqual(social.snapshot("Driver", "B"), ())
            self.assertEqual(social.snapshot("Guest", "B"), ())

            blocked = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "RBLK", {"USER": "Driver", "ID": "22"}, transaction=0
                ),
                guest, now=10.2,
            )
            self.assertEqual(self._decode(blocked[0]).command, "RBLK")
            self.assertTrue(social.is_blocked("Guest", "Driver"))
            unblocked = adapter.dispatch(
                EAMessengerFrame.from_fields(
                    "UBLK", {"USER": "Driver", "ID": "23"}, transaction=0
                ),
                guest, now=10.3,
            )
            self.assertEqual(self._decode(unblocked[0]).command, "UBLK")
            self.assertFalse(social.is_blocked("Guest", "Driver"))
            adapter.close(driver)
            adapter.close(guest)

    @staticmethod
    def _decode(wire: bytes) -> EAMessengerFrame:
        command = wire[:4].decode("latin-1")
        word = int.from_bytes(wire[4:8], "big")
        length = int.from_bytes(wire[8:12], "big")
        return EAMessengerFrame(command, word, wire[12:length])


class CarbonRetailMessengerCommandsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = CarbonMessengerIPCState(max_age_seconds=5, clock=ManualClock())
        self.state.apply(bridge_payload())
        self.social = SocialService()
        self.adapter = CarbonMessengerAdapter(self.state, social=self.social)
        self.pushes = {"Driver": [], "Guest": []}
        self.contexts = {}
        for port, (name, token) in enumerate((("Driver", "driver-key."), ("Guest", "guest-key.")), 8000):
            context = self.adapter.open(("127.0.0.1", port),
                                        lambda wire, name=name: self.pushes[name].append(wire) or True, now=1)
            self.adapter.dispatch(CarbonSharedMessengerTests.auth(token), context, now=1)
            self.adapter.dispatch(
                EAMessengerFrame.from_fields("PSET", {"SHOW": "CHAT", "ID": "bootstrap"}, transaction=0),
                context,
                now=1,
            )
            self.contexts[name] = context
            self.addCleanup(self.adapter.close, context)
        self.clear_pushes()

    def clear_pushes(self):
        for frames in self.pushes.values():
            frames.clear()

    def request(self, name, command, **fields):
        frame = EAMessengerFrame.from_fields(command, {"ID": "42", **fields}, transaction=0)
        return [CarbonSharedMessengerTests._decode(wire) for wire in
                self.adapter.dispatch(frame, self.contexts[name], now=2)]

    def pushed(self, name, command):
        return [frame for wire in self.pushes[name]
                if (frame := CarbonSharedMessengerTests._decode(wire)).command == command]

    def test_retail_command_registry_coverage_and_direction(self):
        registry = set("AUTH DISC USCH USER PADD PDEL PSET PGET RADD RADM RRSP RDEL RDEM RNOT RGET ROST MLST TCKL GINV GRSP GRVK GNOT SEND RECV BRDC EPST EPGT ADMN PING".split())
        self.assertEqual(set(RETAIL_REQUEST_HANDLERS) | RETAIL_ASYNC_COMMANDS, registry)
        self.assertFalse(set(RETAIL_REQUEST_HANDLERS) & RETAIL_ASYNC_COMMANDS)
        self.assertEqual(RETAIL_ASYNC_COMMANDS, set("USER PGET RNOT ROST GNOT RECV ADMN".split()))
        for command, method in RETAIL_REQUEST_HANDLERS.items():
            with self.subTest(command=command):
                self.assertTrue(callable(getattr(self.adapter.service, method)))
        for command in RETAIL_ASYNC_COMMANDS:
            with self.subTest(command=command):
                self.assertEqual(self.request("Driver", command, USER="Guest"), [])
        self.assertEqual(self.pushes, {"Driver": [], "Guest": []})

    def test_send_ack_and_async_recv_preserve_retail_fields(self):
        for message_type in ("C", "A"):
            with self.subTest(message_type=message_type):
                self.clear_pushes()
                reply = self.request("Driver", "SEND", USER="Guest@messaging.ea.com/eagames/NFS-2007",
                                     TYPE=message_type, SUBJ='"Race?"', BODY='"Hello = world"', SECS="12",
                                     RSRC="retail-resource", NOREPLY="T", EXTR="opaque")
                self.assertEqual(reply[0].fields, {"ID": "42"})
                self.assertEqual(reply[0].transaction, 0)
                received = self.pushed("Guest", "RECV")
                self.assertEqual(len(received), 1)
                self.assertEqual(received[0].transaction, 0x80000000)
                self.assertTrue(received[0].payload.endswith(b"\n\x00"))
                self.assertEqual(received[0].fields, {
                    "USER": "Driver", "TYPE": message_type, "SUBJ": '"Race?"',
                    "BODY": '"Hello = world"', "SECS": "12", "RSRC": "retail-resource",
                    "NOREPLY": "T", "EXTR": "opaque",
                })

    def test_send_uses_shared_delivery_for_non_carbon_recipient(self):
        events = []
        self.social.register_lobby("mw", "mw", "MwDriver", "127.0.0.2", game_id="most_wanted")
        self.social.register_control("mw-control", "127.0.0.2", "MwDriver",
                                     lambda verb, fields: not events.append((verb, dict(fields))))
        self.assertNotIn("ERR", self.request("Driver", "SEND", USER="MwDriver", TYPE="C", SUBJ="s", BODY="b", SECS="0")[0].fields)
        self.assertEqual(events, [("RECV", {"USER": "Driver", "TYPE": "C", "SUBJ": "s", "BODY": "b", "SECS": "0"})])
        self.assertEqual(self.social.deliver("Driver", "RECV", (("USER", "MwDriver"), ("BODY", "reply"))), 1)
        self.assertEqual(self.pushed("Driver", "RECV")[0].fields["USER"], "MwDriver")

    def test_send_respects_blocks_in_both_directions_and_offline(self):
        for owner, target in (("Driver", "Guest"), ("Guest", "Driver")):
            self.social.set_blocked(owner, target, True)
            self.assertEqual(self.request("Driver", "SEND", USER="Guest", BODY="hidden")[0].fields["ERR"], "BLOCKED")
            self.assertFalse(self.pushed("Guest", "RECV"))
            self.social.set_blocked(owner, target, False)
        self.adapter.close(self.contexts["Guest"])
        self.assertEqual(self.request("Driver", "SEND", USER="Guest", BODY="offline")[0].fields["ERR"], "USER_OFFLINE")

    def test_send_rejects_invalid_fields_without_delivery(self):
        for fields in ({"TYPE": "X"}, {"SECS": "bad"}, {"SECS": "1_0"}, {"SECS": "2147483648"}, {"USER": ""}):
            with self.subTest(fields=fields):
                reply = self.request("Driver", "SEND", **{"USER": "Guest", "BODY": "body", **fields})
                self.assertEqual(reply[0].fields["ERR"], "INVALID_REQUEST")
        self.assertFalse(self.pushed("Guest", "RECV"))
        self.assertNotIn("ERR", self.request("Driver", "SEND", USER="Guest", BODY="body", SECS="-1")[0].fields)
        self.assertEqual(self.pushed("Guest", "RECV")[0].fields["SECS"], "-1")

    def test_unauthenticated_requests_cannot_spoof_or_mutate(self):
        context = self.adapter.open(("127.0.0.1", 9000), lambda wire: True, now=1)
        for command in ("SEND", "BRDC", "PADD", "PDEL", "PSET", "EPST", "EPGT", "MLST", "TCKL"):
            frame = EAMessengerFrame.from_fields(command, {"ID": "x", "USER": "Guest", "BODY": "spoof", "ENAB": "T"})
            reply = self.adapter.dispatch(frame, context, now=2)
            self.assertEqual(CarbonSharedMessengerTests._decode(reply[0]).fields["ERR"], "NOT_AUTHENTICATED")
        self.assertFalse(context.connection.subscriptions)
        self.assertFalse(context.connection.endpoint_enabled)
        self.assertFalse(self.pushed("Guest", "RECV"))

    def test_duplicate_session_cannot_send_during_read_only_bootstrap(self):
        context = self.contexts["Driver"]
        context.connection.forced_logoff_reason = "DUPL"
        self.addCleanup(setattr, context.connection, "forced_logoff_reason", "")
        self.assertEqual(self.request("Driver", "SEND", USER="Guest", BODY="spoof")[0].fields["ERR"], "NOT_AUTHENTICATED")
        self.assertFalse(self.pushed("Guest", "RECV"))

    def test_presence_subscription_snapshot_changes_unsubscribe_and_resubscribe(self):
        replies = self.request("Guest", "PADD", USER="Driver@messaging.ea.com/eagames/NFS-2007")
        self.assertEqual([frame.command for frame in replies], ["PADD", "PGET"])
        self.assertEqual(replies[1].fields["SHOW"], "CHAT")
        self.request("Driver", "PSET", SHOW="DND", RICH="rich", EXTR="extra", SESS="s", DOMN="ea", PROD="custom")
        pushed = self.pushed("Guest", "PGET")
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0].fields["SHOW"], "DND")
        for key, value in {"RICH": "rich", "EXTR": "extra", "SESS": "s", "DOMN": "ea", "PROD": "custom"}.items():
            self.assertEqual(pushed[0].fields[key], value)
        self.request("Guest", "PDEL", USER="Driver")
        self.clear_pushes()
        self.request("Driver", "PSET", SHOW="XA")
        self.assertFalse(self.pushed("Guest", "PGET"))
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[1].fields["SHOW"], "XA")
        self.adapter.close(self.contexts["Driver"])
        self.assertEqual(self.pushed("Guest", "PGET")[-1].fields["SHOW"], "AWAY")

    def test_pdel_suppresses_implicit_friend_subscription_without_unfriending(self):
        self.social.request_friend("Driver", "Guest")
        self.social.respond_friend("Guest", "Driver", True)
        self.request("Guest", "RGET", LIST="B")
        self.request("Guest", "PDEL", USER="Driver")
        self.clear_pushes()
        self.request("Driver", "PSET", SHOW="AWAY")
        self.assertFalse(self.pushed("Guest", "PGET"))
        self.assertTrue(self.social.presence_row("Guest", "Driver").friend)

    def test_padd_offline_snapshot_and_blocked_subscription(self):
        self.adapter.close(self.contexts["Driver"])
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[1].fields["SHOW"], "AWAY")
        self.social.set_blocked("Driver", "Guest", True)
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[0].fields["ERR"], "BLOCKED")
        for command in ("PADD", "PDEL"):
            self.assertEqual(self.request("Guest", command)[0].fields["ERR"], "INVALID_REQUEST")

    def test_appear_offline_masks_friend_roster_and_restores_visibility(self):
        self.social.request_friend("Driver", "Guest")
        self.social.respond_friend("Guest", "Driver", True)
        self.request("Driver", "PSET", SHOW="GAME", ATTR="J", SESS="room-7", GSTR="Race")
        visible = self.request("Guest", "RGET", LIST="B")
        self.assertEqual([frame.command for frame in visible], ["RGET", "ROST", "PGET"])
        self.assertEqual(visible[-1].fields["SHOW"], "GAME")

        self.clear_pushes()
        self.assertEqual(self.request("Driver", "PSET", SHOW="DISC")[0].fields, {"ID": "42"})
        hidden_updates = self.pushed("Guest", "PGET")
        self.assertEqual(len(hidden_updates), 1)
        self.assertEqual(hidden_updates[0].fields["SHOW"], "DISC")
        self.assertNotEqual(hidden_updates[0].fields.get("ATTR"), "J")
        self.assertNotIn("SESS", hidden_updates[0].fields)
        self.assertNotIn("GSTR", hidden_updates[0].fields)
        hidden_roster = self.request("Guest", "RGET", LIST="B")
        self.assertEqual([frame.command for frame in hidden_roster], ["RGET", "ROST"])
        self.assertEqual(hidden_roster[1].fields["ATTR"], "AT")
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[-1].fields["SHOW"], "DISC")

        self.clear_pushes()
        self.assertEqual(self.request("Driver", "PSET", SHOW="GAME", ATTR="J")[0].fields, {"ID": "42"})
        self.assertEqual(self.pushed("Guest", "PGET")[-1].fields["SHOW"], "GAME")
        restored = self.request("Guest", "RGET", LIST="B")
        self.assertEqual([frame.command for frame in restored], ["RGET", "ROST", "PGET"])
        self.assertEqual(restored[-1].fields["SHOW"], "GAME")

    def test_carbon_show_is_shared_with_public_status_until_disconnect(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            database.create_account("driver", "pw", persona="Driver")
            social = SocialService(database=database)
            adapter = CarbonMessengerAdapter(self.state, social=social, auth_ipc_wait=0.25)
            context = adapter.open(("127.0.0.1", 4301), lambda _wire: True, now=10.0)
            adapter.dispatch(CarbonSharedMessengerTests.auth("driver-key."), context, now=10.0)

            def public_show() -> str | None:
                with database.connect() as connection:
                    row = connection.execute(
                        "SELECT show FROM carbon_public_presence WHERE persona='Driver'"
                    ).fetchone()
                return None if row is None else str(row["show"])

            self.assertEqual(public_show(), "PENDING")
            for show in ("DISC", "CHAT"):
                adapter.dispatch(
                    EAMessengerFrame.from_fields(
                        "PSET", {"SHOW": show, "ID": show}, transaction=0,
                    ),
                    context,
                    now=10.0,
                )
                self.assertEqual(public_show(), show)
            adapter.close(context)
            self.assertIsNone(public_show())

    def test_website_visibility_masks_carbon_buddies_without_ending_session(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            driver = database.create_account("driver", "pw", persona="Driver")
            database.create_account("guest", "pw", persona="Guest")
            with database.transaction() as connection:
                connection.execute(
                    "CREATE TABLE web_account_preferences("
                    "account_id INTEGER PRIMARY KEY, appear_online INTEGER NOT NULL DEFAULT 1)"
                )
                connection.execute(
                    "INSERT INTO web_account_preferences(account_id,appear_online) VALUES(?,1)",
                    (driver.account_id,),
                )
            social = SocialService(database=database)
            social.request_friend("Driver", "Guest")
            social.respond_friend("Guest", "Driver", True)
            adapter = CarbonMessengerAdapter(self.state, social=social, auth_ipc_wait=0.25)
            pushes = {"Driver": [], "Guest": []}
            contexts = {}
            try:
                for port, (name, token) in enumerate(
                    (("Driver", "driver-key."), ("Guest", "guest-key.")), 4301
                ):
                    context = adapter.open(
                        ("127.0.0.1", port),
                        lambda wire, name=name: pushes[name].append(wire) or True,
                        now=10.0,
                    )
                    contexts[name] = context
                    adapter.dispatch(CarbonSharedMessengerTests.auth(token), context, now=10.0)
                    adapter.dispatch(
                        EAMessengerFrame.from_fields(
                            "PSET", {"SHOW": "CHAT", "ID": "ready"}, transaction=0,
                        ),
                        context,
                        now=10.0,
                    )
                pushes["Guest"].clear()
                with database.transaction() as connection:
                    connection.execute(
                        "UPDATE web_account_preferences SET appear_online=0 WHERE account_id=?",
                        (driver.account_id,),
                    )
                adapter.service._notify_social_presence("Driver")
                updates = [
                    CarbonSharedMessengerTests._decode(wire) for wire in pushes["Guest"]
                    if CarbonSharedMessengerTests._decode(wire).command == "PGET"
                ]
                self.assertEqual(updates[-1].fields["SHOW"], "DISC")
                self.assertTrue(social.presence_row("Guest", "Driver").online)
                roster = [
                    CarbonSharedMessengerTests._decode(wire) for wire in adapter.dispatch(
                        EAMessengerFrame.from_fields(
                            "RGET", {"LIST": "B", "ID": "roster"}, transaction=0,
                        ),
                        contexts["Guest"],
                        now=10.0,
                    )
                ]
                self.assertEqual([frame.command for frame in roster], ["RGET", "ROST"])

                pushes["Guest"].clear()
                with database.transaction() as connection:
                    connection.execute(
                        "UPDATE web_account_preferences SET appear_online=1 WHERE account_id=?",
                        (driver.account_id,),
                    )
                adapter.service._notify_social_presence("Driver")
                restored = [
                    CarbonSharedMessengerTests._decode(wire) for wire in pushes["Guest"]
                    if CarbonSharedMessengerTests._decode(wire).command == "PGET"
                ]
                self.assertEqual(restored[-1].fields["SHOW"], "CHAT")
            finally:
                for context in contexts.values():
                    adapter.close(context)

    def test_appear_offline_masks_explicit_subscription_but_keeps_room_membership(self):
        driver_connection = self.contexts["Driver"].connection.connection_id
        guest_connection = self.contexts["Guest"].connection.connection_id
        self.social.set_game_session(driver_connection, "Driver", "carbon", "room-7")
        self.social.set_game_session(guest_connection, "Guest", "carbon", "room-7")
        self.assertEqual(
            [row.user for row in self.social.game_player_snapshot("Guest", "carbon")],
            ["Driver"],
        )
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[-1].fields["SHOW"], "CHAT")

        self.clear_pushes()
        self.request("Driver", "PSET", SHOW="DISC")
        self.assertEqual(self.pushed("Guest", "PGET")[-1].fields["SHOW"], "DISC")
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[-1].fields["SHOW"], "DISC")
        self.assertEqual(
            [row.user for row in self.social.game_player_snapshot("Guest", "carbon")],
            ["Driver"],
        )

        self.clear_pushes()
        self.request("Driver", "PSET", SHOW="CHAT")
        self.assertEqual(self.pushed("Guest", "PGET")[-1].fields["SHOW"], "CHAT")
        self.assertEqual(
            [row.user for row in self.social.game_player_snapshot("Guest", "carbon")],
            ["Driver"],
        )

    def test_all_retail_presence_states_and_game_preserve_extensions(self):
        self.request("Guest", "PADD", USER="Driver")
        extensions = dict(RSRC="r", DOMN="d", RICH="r", ATTR="J", EXTR="e", SESS="s",
                          PROD="p", STAT='"s"', CHNG="2", GROUP="g", UID="u", GSTR="g",
                          TYPE="t", HOST="h", ERRS="0", NOREPLY="T")
        for show in ("CHAT", "AWAY", "XA", "DND", "GAME"):
            with self.subTest(show=show):
                self.clear_pushes()
                self.assertEqual(self.request("Driver", "PSET", SHOW=show, **extensions)[0].fields, {"ID": "42"})
                values = self.pushed("Guest", "PGET")[0].fields
                self.assertEqual(values["SHOW"], show)
                for key, value in extensions.items():
                    self.assertEqual(values[key], value)
        self.assertEqual(self.request("Driver", "PSET", SHOW="BOGUS")[0].fields["ERR"], "INVALID_PRESENCE")
        self.assertEqual(self.contexts["Driver"].connection.show, "GAME")

    def test_endpoint_state_is_session_local_and_default_is_retail_compatible(self):
        self.assertEqual(self.request("Driver", "EPGT")[0].fields, {"ID": "42", "ENAB": "F", "ADDR": ""})
        self.assertEqual(self.request("Driver", "EPST", ENAB="T", ADDR="driver@example.test")[0].fields, {"ID": "42"})
        self.assertEqual(self.request("Driver", "EPGT")[0].fields["ADDR"], "driver@example.test")
        self.assertEqual(self.request("Guest", "EPGT")[0].fields["ENAB"], "F")
        self.request("Driver", "EPST", ENAB="F")
        self.assertEqual(self.request("Driver", "EPGT")[0].fields, {"ID": "42", "ENAB": "F", "ADDR": ""})
        self.request("Driver", "EPST", ENAB="T")
        self.assertEqual(self.request("Driver", "EPGT")[0].fields["ADDR"], "driver@example.test")
        self.assertEqual(self.request("Driver", "EPST", ENAB="bad")[0].fields["ERR"], "INVALID_REQUEST")
        self.adapter.close(self.contexts["Driver"])
        self.assertFalse(self.contexts["Driver"].connection.endpoint_enabled)

    def test_mlst_tckl_and_brdc_compatibility_has_no_unproven_side_effects(self):
        self.assertEqual(self.request("Driver", "MLST", USER="Driver", GROUP="g", LRSC="r")[0].fields,
                         {"ID": "42", "USER": "Driver", "GROUP": "g", "LRSC": "r", "SIZE": "0"})
        self.assertEqual(self.request("Driver", "MLST")[0].fields["ERR"], "INVALID_REQUEST")
        self.assertEqual(self.request("Driver", "TCKL")[0].fields, {"ID": "42"})
        self.assertEqual(self.request("Driver", "TCKL", USER="Guest")[0].fields, {"ID": "42", "USER": "Guest"})
        self.assertEqual(self.request("Driver", "BRDC", USER="Guest", TYPE="C", BODY="b", SUBJ="s", SECS="0")[0].fields["ERR"], "NOT_SUPPORTED")
        self.assertFalse(self.pushed("Guest", "RECV"))
        self.assertFalse(self.contexts["Driver"].connection.subscriptions)

    def test_concurrent_direct_messages_deliver_each_frame_once(self):
        def send(index):
            return self.request("Driver", "SEND", USER="Guest", BODY=str(index))[0].fields
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all("ERR" not in reply for reply in pool.map(send, range(40))))
        bodies = [frame.fields["BODY"] for frame in self.pushed("Guest", "RECV")]
        self.assertCountEqual(bodies, [str(index) for index in range(40)])

    def test_presence_subscription_belongs_to_connection_not_whole_persona(self):
        pushed = []
        second = self.adapter.open(("127.0.0.1", 9100), lambda wire: pushed.append(wire) or True, now=1)
        self.adapter.dispatch(CarbonSharedMessengerTests.auth("guest-key."), second, now=1)
        self.addCleanup(self.adapter.close, second)
        self.request("Guest", "PADD", USER="Driver")
        pushed.clear()
        self.clear_pushes()
        self.request("Driver", "PSET", SHOW="DND")
        self.assertEqual(len(self.pushed("Guest", "PGET")), 1)
        self.assertFalse(any(CarbonSharedMessengerTests._decode(wire).command == "PGET" for wire in pushed))

    def test_standalone_delivery_and_subscribe_before_peer_auth(self):
        adapter = CarbonMessengerAdapter(self.state)
        pushes = []
        guest = adapter.open(("127.0.0.1", 9200), lambda wire: pushes.append(wire) or True, now=1)
        adapter.dispatch(CarbonSharedMessengerTests.auth("guest-key."), guest, now=1)
        self.addCleanup(adapter.close, guest)
        frame = EAMessengerFrame.from_fields("PADD", {"USER": "Driver", "ID": "1"})
        replies = adapter.dispatch(frame, guest, now=1)
        self.assertEqual(CarbonSharedMessengerTests._decode(replies[1]).fields["SHOW"], "AWAY")
        driver = adapter.open(("127.0.0.1", 9201), lambda wire: True, now=1)
        adapter.dispatch(CarbonSharedMessengerTests.auth("driver-key."), driver, now=1)
        adapter.dispatch(
            EAMessengerFrame.from_fields("PSET", {"SHOW": "CHAT", "ID": "2"}, transaction=0),
            driver,
            now=1,
        )
        self.addCleanup(adapter.close, driver)
        self.assertEqual(CarbonSharedMessengerTests._decode(pushes[-1]).fields["SHOW"], "CHAT")
        guest.connection.sender = None
        adapter.dispatch(EAMessengerFrame.from_fields("SEND", {"USER": "Guest", "BODY": "queued", "SUBJ": "s", "SECS": "5"}), driver, now=2)
        received = [CarbonSharedMessengerTests._decode(wire) for wire in adapter.poll(guest, now=2)]
        self.assertEqual(received[0].command, "RECV")
        self.assertEqual(received[0].fields["USER"], "Driver")
        self.assertEqual(received[0].fields["BODY"], "queued")

    def test_subscription_tracks_shared_peer_outside_carbon_connections(self):
        self.adapter.close(self.contexts["Driver"])
        self.social.register_lobby("driver-mw", "driver", "Driver", "127.0.0.2", game_id="most_wanted")
        self.social.set_presence("Driver", show="CHAT", stat="In Most Wanted")
        self.assertEqual(self.request("Guest", "PADD", USER="Driver")[1].fields["SHOW"], "CHAT")
        self.clear_pushes()
        self.social.set_presence("Driver", show="DND", stat="Busy")
        self.adapter.poll(self.contexts["Guest"], now=3)
        self.assertEqual(self.pushed("Guest", "PGET")[0].fields["SHOW"], "DND")
        self.adapter.poll(self.contexts["Guest"], now=4)
        self.assertEqual(len(self.pushed("Guest", "PGET")), 1)
        self.request("Guest", "PDEL", USER="Driver")
        self.social.set_presence("Driver", show="CHAT")
        self.adapter.poll(self.contexts["Guest"], now=5)
        self.assertEqual(len(self.pushed("Guest", "PGET")), 1)


if __name__ == "__main__":
    unittest.main()
