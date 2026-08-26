"""Carbon dialect coverage for the shared U2/MW/Carbon Messenger hub."""

from __future__ import annotations

from pathlib import Path
import socket
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
import unittest

from common.accounts import SQLiteAccountDatabase
from common.enforcement import (
    AccountPolicyEvent,
    LiveAccountConnectionRegistry,
)
from classic.core.catalog import GameId
from classic.ea.messenger import (
    EAMessengerFrame,
    EAMessengerHub,
    EAMessengerStreamDecoder,
)
from classic.ea.multiplex import ClassicEndpointMultiplexer
from classic.ea.social import SocialService
from classic.protocols.carbon_messenger import CarbonMessengerAdapter
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
            "duplicate-key.": {**duplicate, "reason": "DUPL"},
        }
        self.state.apply(replacement)

        wires = self.adapter.poll(context, now=10.1)

        self.assertEqual(wires, [])
        self.assertFalse(context.connection.close_requested)
        self.assertTrue(context.connection.authenticated)

    def test_duplicate_messenger_auth_gets_admn_dupl(self) -> None:
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

        wires = self.adapter.dispatch(self.auth("duplicate-key."), context, now=20.0)

        frames = EAMessengerStreamDecoder().feed(wires[0])
        self.assertEqual(frames[0].command, "ADMN")
        self.assertEqual(frames[0].word, 0x80000000)
        self.assertEqual(frames[0].fields, {"TYPE": "DUPL", "SECS": "0"})
        self.assertEqual(frames[0].fields["TYPE"], "DUPL")
        self.assertEqual(self.adapter.poll(context, now=20.1), [])
        self.assertTrue(context.connection.close_requested)
        self.assertIsNotNone(self.state.resolve_session("driver-key."))

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
                        "USER": "Guest",
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
            self.assertEqual([frame.command for frame in decoded], ["USCH", "USER"])
            self.assertEqual(decoded[0].fields, {"ID": "7", "SIZE": "1"})
            self.assertEqual(
                decoded[1].fields,
                {"ID": "7", "RSRC": "eagames/NFS-2007", "USER": "Guest"},
            )

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


if __name__ == "__main__":
    unittest.main()
