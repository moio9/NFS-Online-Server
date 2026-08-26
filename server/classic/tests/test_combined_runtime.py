from __future__ import annotations

from dataclasses import replace
import socket
import struct
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import Mock

from classic.app import ClassicOnlineApplication
from classic.core.catalog import GameId
from classic.core.config import Endpoint, ClassicGameSettings, ServerSettings
from classic.ea.directory import SessionState
from classic.ea.messenger import EAMessengerStreamDecoder
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.password import make_password_token
from classic.protocols.auth import ERROR_IMST
from classic.protocols.prelogin import U2_READY_FLAG
from classic.protocols.stream import ClassicEAStreamDecoder


class CombinedRuntimeTests(unittest.TestCase):
    @staticmethod
    def _settings(root: Path) -> ServerSettings:
        return ServerSettings(
            underground2=ClassicGameSettings(
                GameId.UNDERGROUND2,
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                "517",
                "U2 Public Key",
            ),
            most_wanted=ClassicGameSettings(
                GameId.MOST_WANTED,
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                Endpoint("127.0.0.1", 0),
                "618",
                "MW Public Key",
            ),
            messenger_listen=Endpoint("127.0.0.1", 0),
            messenger_public=Endpoint("127.0.0.1", 0),
            web_listen=Endpoint("127.0.0.1", 0),
            web_public=Endpoint("127.0.0.1", 0),
            mw_lobby_extra_listen=Endpoint("127.0.0.1", 0),
            auth_data_path=str(root / "auth.json"),
            social_data_path=str(root / "social.json"),
            stats_data_path=str(root / "stats.json"),
            connection_timeout=3.0,
        )

    @staticmethod
    def _recv_classic(
        sock: socket.socket,
        decoder: ClassicEAStreamDecoder,
        count: int = 1,
    ):
        deadline = time.monotonic() + 3.0
        packets = list(getattr(decoder, "_test_packet_backlog", ()))
        while len(packets) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"timed out after {len(packets)} packets")
            sock.settimeout(remaining)
            data = sock.recv(8192)
            if not data:
                raise AssertionError("peer closed")
            packets.extend(decoder.feed(data))
        decoder._test_packet_backlog = tuple(packets[count:])
        return packets[:count]

    @staticmethod
    def _send(sock: socket.socket, command: str, fields=()) -> None:
        sock.sendall(ClassicEAFrame.from_fields(command, fields).encode())

    def _assert_snap(
        self,
        lobby: socket.socket,
        persona: str,
        *,
        most_wanted: bool = False,
    ) -> None:
        self._send(
            lobby,
            "snap",
            (
                ("INDEX", 4 if most_wanted else 99),
                ("CHAN", 6 if most_wanted else 12),
                ("START", 0),
                ("RANGE", 5),
                ("FIND", "$"),
            ),
        )
        first, second = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            2,
        )
        snap, row = (first, second)
        self.assertEqual(snap.command, "snap")
        self.assertEqual(snap.fields()["SEQN"], "0")
        if most_wanted:
            self.assertEqual(snap.fields()["RANGE"], "1")
        else:
            self.assertEqual(snap.fields()["RANGE"], "1")
            self.assertEqual(snap.fields()["COUNT"], "1")
            self.assertEqual(snap.fields()["TOTAL"], "1")
            self.assertEqual(snap.fields()["MORE"], "0")
        self.assertEqual(row.command, "+snp")
        self.assertEqual(row.fields()["N"], persona)
        if most_wanted:
            self.assertNotIn("R", row.fields())
            self.assertRegex(row.fields()["P"], r"^[0-9a-f]+$")
            self.assertEqual(len(row.fields()["S"].split(",")), 7)
        else:
            self.assertRegex(row.fields()["P"], r"^[0-9a-f]+$")
            self.assertEqual(len(row.fields()["S"].split(",")), 3)
            self.assertEqual(row.fields()["R"], "1")

    def _assert_empty_game_search(self, lobby: socket.socket) -> None:
        self._send(lobby, "gsea")
        search, status = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            2,
        )
        self.assertEqual(search.command, "gsea")
        self.assertEqual(search.fields()["COUNT"], "0")
        self.assertEqual(status.command, "+sst")
        self.assertEqual(status.fields()["GCR"], "0")
        self.assertEqual(status.fields()["GIP"], "0")

    def _assert_game_create(self, lobby: socket.socket, persona: str) -> None:
        self._send(
            lobby,
            "gcre",
            (
                ("NAME", f"007.{persona}"),
                ("MAXSIZE", 4),
                ("CUSTFLAGS", 0),
                ("SYSFLAGS", 0),
                ("PARAMS", "TRACK%3d4000%0aDIR%3d0%0aLAPS%3d3"),
            ),
        )
        created, who, managed = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            3,
        )
        self.assertEqual(created.command, "gcre")
        self.assertEqual(created.fields()["HOST"], persona)
        self.assertEqual(created.fields()["COUNT"], "1")
        self.assertEqual(who.command, "+who")
        self.assertEqual(who.fields()["N"], persona)
        self.assertEqual(managed.command, "+mgm")
        self.assertEqual(managed.fields()["IDENT"], created.fields()["IDENT"])

    def _assert_mw_userset_create(
        self,
        lobby: socket.socket,
        persona: str,
    ) -> None:
        userset_name = f"026.{persona}"
        params = "V%3d192359%0aP%3d425%0aE%3d534735613%0aL%3d3%0aM%3d2%0a"
        self._send(
            lobby,
            "ucre",
            (
                ("NAME", userset_name),
                ("SIZE", 4),
                ("TYPE", 0),
                ("UPDATES", 0),
                ("SYSFLAGS", "KV"),
                ("CUSTFLAGS", "JKM-"),
                ("PARAMS", params),
            ),
        )
        userset_frames = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            4,
        )
        created, who, updated_set, member = userset_frames
        created_fields = created.fields()
        self.assertEqual(created.command, "ucre")
        self.assertEqual(created_fields["I"], "1")
        self.assertEqual(created_fields["T"], "0")
        self.assertEqual(created_fields["SF"], "KV")
        self.assertEqual(created_fields["CF"], "JKM-")
        self.assertEqual(created_fields["O"], persona)
        self.assertEqual(created_fields["S"], "4")
        self.assertEqual(created_fields["N"], userset_name)
        self.assertEqual(created_fields["P"], params)
        self.assertEqual(created_fields["C"], "1")
        self.assertEqual(created_fields["IDENT"], "1")
        self.assertEqual(created_fields["NAME"], userset_name)
        self.assertEqual(who.command, "+who")
        self.assertEqual(who.fields()["I"], "1")
        self.assertEqual(who.fields()["US"], userset_name)
        self.assertEqual(updated_set.command, "+ust")
        self.assertNotIn("IDENT", updated_set.fields())
        self.assertNotIn("NAME", updated_set.fields())
        self.assertEqual(member.command, "+usm")

        self._send(
            lobby,
            "gcre",
            (
                ("NAME", persona),
                ("MAXSIZE", 4),
                ("MINSIZE", 2),
                ("SYSFLAGS", 0),
            ),
        )
        room_who, room_member, game_created = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            3,
        )
        self.assertEqual(room_who.command, "+who")
        self.assertEqual(room_who.fields()["US"], userset_name)
        self.assertEqual(room_who.fields()["G"], "0")
        self.assertEqual(room_member.command, "+usm")
        self.assertEqual(room_member.fields()["G"], "0")
        self.assertEqual(game_created.command, "gcre")
        self.assertEqual(game_created.fields()["IDENT"], "1")
        self.assertEqual(game_created.fields()["NAME"], persona)
        self.assertEqual(game_created.fields()["OPID0"], "1")
        self.assertEqual(game_created.fields()["PRES0"], "0")

        self._send(
            lobby,
            "uadm",
            (
                ("NAME", userset_name),
                ("DESC", "$0100007f"),
                ("PARAMS", params),
                ("CUSTFLAGS", "JKM-"),
            ),
        )
        updated_frames = self._recv_classic(
            lobby,
            ClassicEAStreamDecoder(),
            5,
        )
        updated = updated_frames[0]
        self.assertEqual(updated.command, "uadm")
        self.assertEqual(updated.fields()["D"], "$0100007f")
        self.assertEqual(
            [frame.command for frame in updated_frames[1:]],
            ["+who", "+ust", "+usm", "+mgm"],
        )
        self.assertEqual(updated_frames[1].fields()["G"], "1")
        self.assertEqual(updated_frames[3].fields()["G"], "1")

    def _login(
        self,
        bootstrap_endpoint: Endpoint,
        account: str,
        persona: str,
        password: str,
    ) -> tuple[socket.socket, dict[str, str]]:
        with socket.create_connection(
            (bootstrap_endpoint.host, bootstrap_endpoint.port), timeout=2
        ) as bootstrap:
            decoder = ClassicEAStreamDecoder()
            self._send(bootstrap, "@dir")
            directory = self._recv_classic(bootstrap, decoder)[0]
        fields = directory.fields()
        lobby = socket.create_connection((fields["ADDR"], int(fields["PORT"])), timeout=2)
        decoder = ClassicEAStreamDecoder()
        self._send(lobby, "addr", (("ADDR", "127.0.0.1"), ("PORT", 65000)))
        self._recv_classic(lobby, decoder)
        self._send(lobby, "skey")
        self._recv_classic(lobby, decoder)
        self._send(lobby, "news")
        news = self._recv_classic(lobby, decoder)[0]
        self._send(
            lobby,
            "auth",
            (
                ("NAME", account),
                ("PASS", make_password_token(password, fields["MASK"])),
                ("PSES", fields["MASK"]),
            ),
        )
        auth = self._recv_classic(lobby, decoder)[0]
        self.assertEqual(auth.command, "auth")
        self._send(lobby, "pers", (("PERS", persona),))
        pers = self._recv_classic(lobby, decoder)[0]
        self.assertEqual(pers.fields()["PERS"], persona)
        self._send(lobby, "user")
        self._recv_classic(lobby, decoder, 2)
        return lobby, news.fields()

    def test_both_games_share_one_messenger_listener(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = self._settings(root)
            app = ClassicOnlineApplication(settings)
            app.credentials.create_account("U2Account", "secret", persona="U2Driver")
            app.credentials.create_account("MWAccount", "secret", persona="MWDriver")
            app.credentials.create_account(
                "MWGuestAccount",
                "secret",
                persona="MWGuest",
            )
            app.start()
            u2_lobby = mw_lobby = mw_guest = None
            try:
                u2_lobby, u2_news = self._login(
                    app.u2.bootstrap_listener.bound_endpoint,
                    "U2Account",
                    "U2Driver",
                    "secret",
                )
                self._assert_snap(u2_lobby, "U2Driver")
                self._assert_empty_game_search(u2_lobby)
                self._assert_game_create(u2_lobby, "U2Driver")
                mw_lobby, mw_news = self._login(
                    app.mw.bootstrap_listener.bound_endpoint,
                    "MWAccount",
                    "MWDriver",
                    "secret",
                )
                self._assert_snap(mw_lobby, "MWDriver", most_wanted=True)
                self._assert_empty_game_search(mw_lobby)
                self._assert_mw_userset_create(mw_lobby, "MWDriver")
                mw_guest, _guest_news = self._login(
                    app.mw.bootstrap_listener.bound_endpoint,
                    "MWGuestAccount",
                    "MWGuest",
                    "secret",
                )
                guest_decoder = ClassicEAStreamDecoder()
                host_decoder = ClassicEAStreamDecoder()
                self._send(mw_guest, "usea")
                usersets = self._recv_classic(mw_guest, guest_decoder, 2)
                self.assertEqual(usersets[1].fields()["N"], "026.MWDriver")
                self._send(
                    mw_guest,
                    "ujoi",
                    (("NAME", "026.MWDriver"),),
                )
                userset_joined = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    1,
                )
                self.assertEqual(
                    [frame.command for frame in userset_joined],
                    ["ujoi"],
                )
                self.assertEqual(userset_joined[0].fields()["C"], "2")
                self._send(mw_guest, "gjoi", (("NAME", "MWDriver"),))
                # Retail publishes the new member to existing peers when
                # gjoi succeeds, not during the earlier ujoi transaction.
                host_userset_join = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in host_userset_join],
                    ["+ust", "+usm"],
                )
                self.assertEqual(host_userset_join[0].fields()["O"], "MWDriver")
                self.assertEqual(host_userset_join[0].fields()["C"], "2")
                self.assertEqual(host_userset_join[1].fields()["N"], "MWGuest")
                self.assertEqual(host_userset_join[1].fields()["G"], "0")
                guest_wire_id = int(host_userset_join[1].fields()["I"])
                staged_join = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in staged_join],
                    ["+usm", "gjoi"],
                )
                self.assertEqual(staged_join[0].fields()["N"], "MWDriver")
                self.assertEqual(staged_join[0].fields()["G"], "1")
                self.assertEqual(staged_join[1].fields()["IDENT"], "1")
                self.assertEqual(staged_join[1].fields()["OPID0"], "1")
                self.assertEqual(staged_join[1].fields()["OPID1"], "2")

                # The live client can send AUXI immediately after GJOI,
                # before the host gets around to `onln PERS=<joiner>`.  Retail
                # gives the joiner its G=0 roster snapshot but does not promote
                # the CommUDP edge from AUXI.
                guest_context = next(
                    candidate
                    for candidate in app.mw.prelogin._connections.values()
                    if candidate.auth.persona == "MWGuest"
                )
                self.assertEqual(guest_context.mw_join_pending_game_id, 1)
                self._send(
                    mw_guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aV%3d20%0a"),),
                )
                staged_snapshot = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    4,
                )
                self.assertEqual(
                    [frame.command for frame in staged_snapshot],
                    ["+who", "+ust", "+usm", "auxi"],
                )
                self.assertEqual(staged_snapshot[0].fields()["G"], "0")
                self.assertEqual(guest_context.mw_join_pending_game_id, 1)

                # Regression for the real timing: a second AUXI used to clear
                # the pending flag and publish +usm/+mgm before ONLN.  Keep it
                # staged instead.
                self._send(
                    mw_guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aV%3d20%0a"),),
                )
                waiting_aux = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    1,
                )
                self.assertEqual([frame.command for frame in waiting_aux], ["auxi"])
                self.assertEqual(guest_context.mw_join_pending_game_id, 1)

                # Only the existing peer's ONLN lookup promotes G=0 -> G=game.
                self._send(
                    mw_lobby,
                    "onln",
                    (("PERS", "MWGuest"),),
                )
                host_promotion = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in host_promotion],
                    ["onln", "+usm", "+mgm"],
                )
                # ``after_send`` runs after the ONLN response write.
                promotion_deadline = time.monotonic() + 1.0
                while (
                    guest_context.mw_join_pending_game_id
                    and time.monotonic() < promotion_deadline
                ):
                    time.sleep(0.005)
                self.assertEqual(guest_context.mw_join_pending_game_id, 0)
                guest_promotion = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in guest_promotion],
                    ["+who", "+usm", "+mgm"],
                )
                self.assertEqual(guest_promotion[0].fields()["G"], "1")
                self.assertEqual(guest_promotion[1].fields()["G"], "1")
                self.assertEqual(guest_promotion[2].fields()["COUNT"], "2")
                self._send(
                    mw_guest,
                    "onln",
                    (("PERS", "MWDriver"),),
                )
                host_online = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                )[0]
                self.assertEqual(host_online.command, "onln")
                self.assertEqual(host_online.fields()["I"], "1")
                self.assertEqual(host_online.fields()["F"], "")
                self.assertEqual(host_online.fields()["P"], "425")
                self.assertEqual(host_online.fields()["S"], "")
                self.assertEqual(host_online.fields()["G"], "1")
                self.assertEqual(host_online.fields()["CL"], "511")
                self.assertEqual(host_online.fields()["MA"], "")
                self.assertEqual(
                    host_online.fields()["US"],
                    "026.MWDriver",
                )
                updated_params = (
                    "V%3d192359%0aP%3d425%0aE%3d123456%0a"
                    "L%3d3%0aM%3d2%0a"
                )
                self._send(
                    mw_lobby,
                    "uadm",
                    (
                        ("NAME", "026.MWDriver"),
                        ("PARAMS", updated_params),
                        ("CUSTFLAGS", "JKM2"),
                    ),
                )
                host_update = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    5,
                )
                self.assertEqual(host_update[0].command, "uadm")
                guest_update = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                )[0]
                self.assertEqual(guest_update.command, "+ust")
                self.assertEqual(guest_update.fields()["P"], updated_params)
                self.assertEqual(guest_update.fields()["CF"], "JKM2")
                self._send(
                    mw_guest,
                    "glea",
                    (("NAME", "MWDriver"), ("FORCE", 1)),
                )
                game_left = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                )[0]
                self.assertEqual(game_left.command, "glea")
                self.assertEqual(game_left.fields()["COUNT"], "1")
                host_leave_notice = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    2,
                )
                self.assertEqual(host_leave_notice[0].command, "+usm")
                self.assertEqual(host_leave_notice[0].fields()["G"], "0")
                self.assertEqual(host_leave_notice[1].fields()["COUNT"], "1")
                self._send(
                    mw_guest,
                    "ulea",
                    (("NAME", "026.MWDriver"),),
                )
                userset_left = self._recv_classic(
                    mw_guest,
                    guest_decoder,
                    4,
                )
                self.assertEqual(
                    [frame.command for frame in userset_left],
                    ["+who", "+mgm", "ulea", "+who"],
                )
                self.assertEqual(userset_left[0].fields()["G"], "0")
                self.assertEqual(userset_left[-1].fields()["US"], "")
                host_userset_notice = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in host_userset_notice],
                    ["+ust", "+usm", "+mgm"],
                )
                removed_fields = host_userset_notice[1].fields()
                self.assertEqual(removed_fields["I"], str(guest_wire_id))
                self.assertEqual(removed_fields["S"], "0")
                self.assertNotIn("N", removed_fields)
                self._send(
                    mw_lobby,
                    "gdel",
                    (("NAME", "MWDriver"), ("FORCE", 1)),
                )
                self.assertEqual(
                    self._recv_classic(mw_lobby, host_decoder)[0].command,
                    "gdel",
                )
                self._send(
                    mw_lobby,
                    "udel",
                    (("NAME", "026.MWDriver"),),
                )
                userset_deleted = self._recv_classic(
                    mw_lobby,
                    host_decoder,
                    4,
                )
                self.assertEqual(
                    [frame.command for frame in userset_deleted],
                    ["udel", "+who", "+ust", "+mgm"],
                )
                self.assertEqual(userset_deleted[1].fields()["US"], "")
                self.assertEqual(userset_deleted[2].fields()["I"], "1")
                self.assertEqual(userset_deleted[3].fields()["IDENT"], "1")
                messenger = app.messenger_listener.bound_endpoint
                self.assertEqual(int(u2_news["BUDDY_PORT"]), messenger.port)
                self.assertEqual(int(mw_news["BUDDY_PORT"]), messenger.port)
                self.assertEqual(u2_news["TOSURL"], mw_news["TOSA_URL"])
                self.assertEqual(mw_news["TOSA_URL"], mw_news["TOSAC_URL"])
                self.assertTrue(u2_news["TOSURL"].endswith("/tos"))
                self.assertTrue(u2_news["NEWSURL"].endswith("/u2"))
                self.assertTrue(mw_news["NEWS_URL"].endswith("/mw"))
                self.assertNotEqual(u2_news["NEWSURL"], mw_news["NEWS_URL"])

                web = app.web_listener.bound_endpoint
                for endpoint in (messenger, web):
                    with socket.create_connection(
                        (endpoint.host, endpoint.port), timeout=2
                    ) as client:
                        client.sendall(
                            b"GET /news HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
                        )
                        client.settimeout(2)
                        response = bytearray()
                        while True:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            response.extend(chunk)
                        self.assertIn(b"HTTP/1.1 200 OK", response)
                        self.assertIn(b'CMD=news TITLE="News"', response)

                for endpoint in (messenger, web):
                    for persona in ("U2Driver", "MWDriver"):
                        with socket.create_connection(
                            (endpoint.host, endpoint.port), timeout=2
                        ) as client:
                            client.sendall(
                                ClassicEAFrame.from_fields(
                                    "AUTH",
                                    {
                                        "PERS": persona,
                                        "PROD": "NFS-CONSOLE-2005",
                                    },
                                ).encode()
                            )
                            client.settimeout(2)
                            response = client.recv(4096)
                            frame = EAMessengerStreamDecoder().feed(response)[0]
                            self.assertEqual(frame.command, "AUTH")
                            self.assertEqual(frame.fields["TITL"], "EA MESSENGER")

                # Stock MW also opens a one-shot Messenger socket with DISC
                # as its first frame. It expects the generic AUTH banner
                # before the server closes that otherwise unselectable socket.
                with socket.create_connection(
                    (messenger.host, messenger.port), timeout=2
                ) as client:
                    client.sendall(
                        ClassicEAFrame.from_fields("DISC", ()).encode()
                    )
                    client.settimeout(2)
                    response = client.recv(4096)
                    frame = EAMessengerStreamDecoder().feed(response)[0]
                    self.assertEqual(frame.command, "AUTH")
                    self.assertEqual(frame.fields["TITL"], "EA MESSENGER")
            finally:
                if u2_lobby is not None:
                    u2_lobby.close()
                if mw_lobby is not None:
                    mw_lobby.close()
                if mw_guest is not None:
                    mw_guest.close()
                app.stop()
    def test_mw_userset_roster_promotes_onln_without_unconfirmed_kick_extension(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.credentials.create_account(
                "RosterHostAccount",
                "secret",
                persona="RosterHost",
            )
            app.credentials.create_account(
                "RosterGuestAccount",
                "secret",
                persona="RosterGuest",
            )
            app.credentials.create_account(
                "RosterGuestTwoAccount",
                "secret",
                persona="RosterGuestTwo",
            )
            app.start()
            host = guest = guest_two = None
            try:
                # Deliberately keep every identifier in a different domain.
                app.mw.prelogin._next_userset_id = 41
                app.mw.prelogin.sessions._next_game_id = 73
                endpoint = app.mw.bootstrap_listener.bound_endpoint
                host, _ = self._login(
                    endpoint,
                    "RosterHostAccount",
                    "RosterHost",
                    "secret",
                )
                guest, _ = self._login(
                    endpoint,
                    "RosterGuestAccount",
                    "RosterGuest",
                    "secret",
                )
                guest_two, _ = self._login(
                    endpoint,
                    "RosterGuestTwoAccount",
                    "RosterGuestTwo",
                    "secret",
                )
                host_decoder = ClassicEAStreamDecoder()
                guest_decoder = ClassicEAStreamDecoder()
                guest_two_decoder = ClassicEAStreamDecoder()

                self._send(
                    host,
                    "ucre",
                    (("NAME", "026.RosterHost"), ("SIZE", 4)),
                )
                created_userset = self._recv_classic(host, host_decoder, 4)[0]
                userset_id = int(created_userset.fields()["I"])
                self.assertEqual(userset_id, 41)
                self.assertEqual(created_userset.fields()["O"], "RosterHost")

                self._send(
                    host,
                    "gcre",
                    (("NAME", "RosterHost"), ("MAXSIZE", 4)),
                )
                created_game = self._recv_classic(host, host_decoder, 3)[2]
                game_id = int(created_game.fields()["IDENT"])
                self.assertEqual(game_id, 73)

                self._send(guest, "usea")
                userset_search = self._recv_classic(guest, guest_decoder, 2)
                self.assertEqual(
                    userset_search[1].fields()["I"],
                    str(userset_id),
                )
                self._send(
                    guest,
                    "ujoi",
                    (("NAME", "026.RosterHost"),),
                )
                guest_join = self._recv_classic(guest, guest_decoder, 1)
                self.assertEqual(guest_join[0].fields()["C"], "2")
                self._send(guest, "gjoi", (("NAME", "RosterHost"),))
                host_roster = self._recv_classic(host, host_decoder, 2)
                self.assertEqual(
                    [frame.command for frame in host_roster],
                    ["+ust", "+usm"],
                )
                self.assertEqual(host_roster[0].fields()["I"], str(userset_id))
                self.assertEqual(host_roster[0].fields()["O"], "RosterHost")
                self.assertEqual(host_roster[0].fields()["C"], "2")
                self.assertNotIn("IDENT", host_roster[0].fields())
                self.assertNotIn("NAME", host_roster[0].fields())
                self.assertEqual(host_roster[1].fields()["N"], "RosterGuest")
                self.assertEqual(host_roster[1].fields()["G"], "0")
                guest_wire_id = int(host_roster[1].fields()["I"])
                guest_user_id = next(
                    user_id
                    for user_id, wire_id in app.mw.prelogin._wire_user_ids.items()
                    if wire_id == guest_wire_id
                )
                guest_context = app.mw.prelogin._context_for_user(guest_user_id)
                self.assertIsNotNone(guest_context)
                self.assertEqual(guest_context.userset_id, userset_id)
                self.assertNotEqual(guest_wire_id, userset_id)
                self.assertNotEqual(guest_wire_id, game_id)
                self.assertNotEqual(userset_id, game_id)
                guest_gjoi = self._recv_classic(guest, guest_decoder, 5)
                self.assertEqual(
                    [frame.command for frame in guest_gjoi],
                    ["+usm", "gjoi", "+who", "+ust", "+usm"],
                )
                self.assertEqual(guest_gjoi[0].fields()["N"], "RosterHost")
                self.assertEqual(guest_gjoi[0].fields()["G"], str(game_id))
                self.assertEqual(guest_gjoi[2].fields()["G"], "0")
                self.assertEqual(guest_gjoi[3].fields()["I"], str(userset_id))
                self.assertEqual(guest_gjoi[4].fields()["N"], "RosterGuest")
                self.assertEqual(guest_gjoi[4].fields()["G"], "0")

                # The host's online lookup is the final promotion point:
                # +usm first changes G from zero to the real game id, then
                # +mgm publishes the game membership.
                self._send(host, "onln", (("PERS", "RosterGuest"),))
                host_promotion = self._recv_classic(host, host_decoder, 3)
                self.assertEqual(
                    [frame.command for frame in host_promotion],
                    ["onln", "+usm", "+mgm"],
                )
                self.assertEqual(
                    host_promotion[1].fields()["G"],
                    str(game_id),
                )
                self.assertEqual(
                    host_promotion[2].fields()["IDENT"],
                    str(game_id),
                )
                guest_local_promotion = self._recv_classic(
                    guest,
                    guest_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in guest_local_promotion],
                    ["+who", "+usm", "+mgm"],
                )
                self.assertEqual(
                    guest_local_promotion[0].fields()["G"],
                    str(game_id),
                )
                self.assertEqual(
                    guest_local_promotion[1].fields()["G"],
                    str(game_id),
                )
                promotion_deadline = time.monotonic() + 0.5
                while (
                    game_id in app.mw.prelogin._mw_join_serial_unstable
                    and time.monotonic() < promotion_deadline
                ):
                    time.sleep(0.005)
                self.assertNotIn(
                    game_id,
                    app.mw.prelogin._mw_join_serial_unstable,
                )
                self._send(
                    guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aLT%3d0%0aV%3d20%0a"),),
                )
                guest_auxi = self._recv_classic(guest, guest_decoder, 1)
                self.assertEqual(guest_auxi[0].command, "auxi")
                host_aux_update = self._recv_classic(
                    host,
                    host_decoder,
                    1,
                )
                self.assertEqual(host_aux_update[0].command, "+usm")
                self.assertEqual(host_aux_update[0].fields()["N"], "RosterGuest")

                # Retail stages a third join exactly like the first one.  At
                # the gjoi transaction both existing peers first receive the
                # userset's new count and a G=0 member.  Their onln lookups
                # then promote that member once both views exist.
                self._send(guest_two, "usea")
                self._recv_classic(guest_two, guest_two_decoder, 2)
                self._send(
                    guest_two,
                    "ujoi",
                    (("NAME", "026.RosterHost"),),
                )
                third_join = self._recv_classic(
                    guest_two,
                    guest_two_decoder,
                    1,
                )
                self.assertEqual(third_join[0].fields()["C"], "3")
                self._send(guest_two, "gjoi", (("NAME", "RosterHost"),))
                for peer, decoder in (
                    (host, host_decoder),
                    (guest, guest_decoder),
                ):
                    peer_roster = self._recv_classic(peer, decoder, 2)
                    self.assertEqual(
                        [frame.command for frame in peer_roster],
                        ["+ust", "+usm"],
                    )
                    self.assertEqual(
                        peer_roster[0].fields()["C"],
                        "3",
                    )
                    self.assertEqual(
                        peer_roster[0].fields()["I"],
                        str(userset_id),
                    )
                    self.assertNotIn("IDENT", peer_roster[0].fields())
                    self.assertNotIn("NAME", peer_roster[0].fields())
                    self.assertEqual(
                        peer_roster[1].fields()["N"],
                        "RosterGuestTwo",
                    )
                    self.assertEqual(
                        peer_roster[1].fields()["G"],
                        "0",
                    )

                # Retail first replays every established identity in reverse
                # join order, then installs the third player's own G=0 view.
                # These rows prevent stable wire IDs from resolving through a
                # stale client-side name object after leave/rejoin.
                third_gjoi = self._recv_classic(
                    guest_two,
                    guest_two_decoder,
                    6,
                )
                self.assertEqual(
                    [frame.command for frame in third_gjoi],
                    ["+usm", "+usm", "gjoi", "+who", "+ust", "+usm"],
                )
                self.assertEqual(
                    [third_gjoi[index].fields()["N"] for index in range(2)],
                    ["RosterGuest", "RosterHost"],
                )
                self.assertEqual(
                    [third_gjoi[index].fields()["G"] for index in range(2)],
                    [str(game_id), str(game_id)],
                )
                self.assertEqual(third_gjoi[3].fields()["G"], "0")
                self.assertEqual(third_gjoi[4].fields()["C"], "3")
                self.assertEqual(third_gjoi[5].fields()["N"], "RosterGuestTwo")
                self.assertEqual(third_gjoi[5].fields()["G"], "0")
                self.assertEqual(
                    [
                        third_gjoi[2].fields()[f"OPPO{index}"]
                        for index in range(3)
                    ],
                    ["RosterHost", "RosterGuest", "RosterGuestTwo"],
                )

                # Live stock clients issue the acknowledgement ONLN after the
                # staged +usm, but its PERS field can still name a participant
                # already cached by that viewer.  The transaction belongs to
                # the sole G=0 member just announced to it; do not return the
                # cached persona and leave the third transport unfinalized.
                self._send(
                    guest,
                    "onln",
                    (("PERS", "RosterHost"),),
                )
                guest_lookup = self._recv_classic(
                    guest,
                    guest_decoder,
                    1,
                )
                self.assertEqual(
                    guest_lookup[0].fields()["G"],
                    "0",
                )
                self.assertEqual(
                    guest_lookup[0].fields()["N"],
                    "RosterGuestTwo",
                )
                self._send(
                    host,
                    "onln",
                    (("PERS", "RosterGuest"),),
                )
                host_promotion = self._recv_classic(host, host_decoder, 3)
                self.assertEqual(
                    [frame.command for frame in host_promotion],
                    ["onln", "+usm", "+mgm"],
                )
                self.assertEqual(host_promotion[0].fields()["G"], str(game_id))
                self.assertEqual(
                    host_promotion[0].fields()["N"],
                    "RosterGuestTwo",
                )
                self.assertEqual(host_promotion[1].fields()["G"], str(game_id))
                self.assertEqual(
                    [
                        host_promotion[2].fields()[f"OPPO{index}"]
                        for index in range(3)
                    ],
                    ["RosterHost", "RosterGuest", "RosterGuestTwo"],
                )
                guest_promotion = self._recv_classic(
                    guest,
                    guest_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in guest_promotion],
                    ["+usm", "+mgm"],
                )
                self.assertEqual(guest_promotion[0].fields()["G"], str(game_id))
                self.assertEqual(
                    [
                        guest_promotion[1].fields()[f"OPPO{index}"]
                        for index in range(3)
                    ],
                    ["RosterHost", "RosterGuest", "RosterGuestTwo"],
                )
                third_local_promotion = self._recv_classic(
                    guest_two,
                    guest_two_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in third_local_promotion],
                    ["+who", "+usm", "+mgm"],
                )
                self.assertEqual(
                    third_local_promotion[0].fields()["G"],
                    str(game_id),
                )
                self.assertEqual(
                    third_local_promotion[1].fields()["G"],
                    str(game_id),
                )
                self.assertEqual(
                    third_local_promotion[2].fields()["COUNT"],
                    "3",
                )
                self.assertEqual(
                    [
                        third_local_promotion[2].fields()[f"OPPO{index}"]
                        for index in range(3)
                    ],
                    ["RosterHost", "RosterGuest", "RosterGuestTwo"],
                )
                guest_two_context = next(
                    context
                    for context in app.mw.prelogin._connections.values()
                    if context.auth.persona == "RosterGuestTwo"
                )
                self.assertEqual(guest_two_context.mw_join_pending_game_id, 0)
                for persona in ("RosterHost", "RosterGuest"):
                    viewer = next(
                        context
                        for context in app.mw.prelogin._connections.values()
                        if context.auth.persona == persona
                    )
                    self.assertNotIn(
                        game_id,
                        viewer.mw_staged_onln_target_ids,
                    )
                self._send(
                    guest_two,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aV%3d20%0a"),),
                )
                third_auxi = self._recv_classic(
                    guest_two,
                    guest_two_decoder,
                    1,
                )
                self.assertEqual(third_auxi[0].command, "auxi")
                for peer, decoder in (
                    (host, host_decoder),
                    (guest, guest_decoder),
                ):
                    third_aux_update = self._recv_classic(peer, decoder, 1)
                    self.assertEqual(third_aux_update[0].command, "+usm")
                    self.assertEqual(
                        third_aux_update[0].fields()["N"],
                        "RosterGuestTwo",
                    )

                # A KICK field is not a confirmed stock MW command.  It must
                # behave as an ordinary gset update and must not remove the
                # guest from the userset or game.
                self._send(
                    host,
                    "gset",
                    (("NAME", "RosterHost"), ("KICK", "RosterGuest")),
                )
                host_gset = self._recv_classic(host, host_decoder, 1)
                self.assertEqual(host_gset[0].command, "gset")
                userset = app.mw.prelogin._usersets[userset_id]
                remaining = app.mw.prelogin.sessions.get_game(game_id)
                self.assertIn(guest_user_id, userset.members or set())
                self.assertIsNotNone(remaining)
                self.assertIn(guest_user_id, remaining.participants)
                self.assertEqual(guest_context.userset_id, userset_id)
                self.assertEqual(guest_context.lobby_game_id, game_id)
            finally:
                if host is not None:
                    host.close()
                if guest is not None:
                    guest.close()
                if guest_two is not None:
                    guest_two.close()
                app.stop()

    def test_mw_stale_previous_persona_promotes_only_pending_replacement_guest(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            for account, persona in (
                ("PreviousHostAccount", "PreviousHost"),
                ("NextHostAccount", "NextHost"),
                ("ReplacementGuestAccount", "ReplacementGuest"),
            ):
                app.credentials.create_account(
                    account,
                    "secret",
                    persona=persona,
                )
            app.start()
            previous_host = next_host = replacement_guest = None
            try:
                endpoint = app.mw.bootstrap_listener.bound_endpoint
                previous_host, _ = self._login(
                    endpoint,
                    "PreviousHostAccount",
                    "PreviousHost",
                    "secret",
                )
                next_host, _ = self._login(
                    endpoint,
                    "NextHostAccount",
                    "NextHost",
                    "secret",
                )
                replacement_guest, _ = self._login(
                    endpoint,
                    "ReplacementGuestAccount",
                    "ReplacementGuest",
                    "secret",
                )
                previous_decoder = ClassicEAStreamDecoder()
                next_decoder = ClassicEAStreamDecoder()
                replacement_decoder = ClassicEAStreamDecoder()

                def create_room(
                    lobby: socket.socket,
                    decoder: ClassicEAStreamDecoder,
                    persona: str,
                    expected_id: int,
                ) -> None:
                    self._send(
                        lobby,
                        "ucre",
                        (("NAME", f"026.{persona}"), ("SIZE", 4)),
                    )
                    created_userset = self._recv_classic(lobby, decoder, 4)
                    self.assertEqual(
                        created_userset[0].fields()["I"],
                        str(expected_id),
                    )
                    self._send(
                        lobby,
                        "gcre",
                        (("NAME", persona), ("MAXSIZE", 4)),
                    )
                    created_game = self._recv_classic(lobby, decoder, 3)
                    self.assertEqual(
                        created_game[2].fields()["IDENT"],
                        str(expected_id),
                    )

                def join_and_promote(
                    host: socket.socket,
                    host_decoder: ClassicEAStreamDecoder,
                    guest: socket.socket,
                    guest_decoder: ClassicEAStreamDecoder,
                    host_persona: str,
                    guest_persona: str,
                    game_id: int,
                ) -> None:
                    self._send(guest, "usea")
                    self._recv_classic(guest, guest_decoder, 2)
                    self._send(
                        guest,
                        "ujoi",
                        (("NAME", f"026.{host_persona}"),),
                    )
                    self._recv_classic(guest, guest_decoder, 1)
                    self._send(
                        guest,
                        "gjoi",
                        (("NAME", host_persona),),
                    )
                    staged_for_host = self._recv_classic(
                        host,
                        host_decoder,
                        2,
                    )
                    self.assertEqual(
                        [frame.command for frame in staged_for_host],
                        ["+ust", "+usm"],
                    )
                    self.assertEqual(
                        staged_for_host[1].fields()["N"],
                        guest_persona,
                    )
                    self.assertEqual(staged_for_host[1].fields()["G"], "0")
                    local_join = self._recv_classic(guest, guest_decoder, 5)
                    self.assertEqual(
                        [frame.command for frame in local_join],
                        ["+usm", "gjoi", "+who", "+ust", "+usm"],
                    )
                    self.assertEqual(local_join[0].fields()["N"], host_persona)
                    self.assertEqual(local_join[0].fields()["G"], str(game_id))
                    self._send(host, "onln", (("PERS", guest_persona),))
                    promoted_for_host = self._recv_classic(
                        host,
                        host_decoder,
                        3,
                    )
                    self.assertEqual(
                        [frame.command for frame in promoted_for_host],
                        ["onln", "+usm", "+mgm"],
                    )
                    self.assertEqual(
                        promoted_for_host[1].fields()["G"],
                        str(game_id),
                    )
                    self._recv_classic(guest, guest_decoder, 3)

                # The future host first participates in another owner's room.
                create_room(previous_host, previous_decoder, "PreviousHost", 1)
                join_and_promote(
                    previous_host,
                    previous_decoder,
                    next_host,
                    next_decoder,
                    "PreviousHost",
                    "NextHost",
                    1,
                )

                # The first owner deletes both objects. The client can still
                # retain its persona record even after accepting this teardown.
                self._send(
                    previous_host,
                    "gdel",
                    (("NAME", "PreviousHost"),),
                )
                self._recv_classic(previous_host, previous_decoder, 1)
                game_reset = self._recv_classic(next_host, next_decoder, 4)
                self.assertEqual(game_reset[-1].command, "+mgm")
                self._send(
                    previous_host,
                    "udel",
                    (("NAME", "026.PreviousHost"),),
                )
                self._recv_classic(previous_host, previous_decoder, 4)
                userset_reset = self._recv_classic(next_host, next_decoder, 3)
                self.assertEqual(
                    [frame.command for frame in userset_reset],
                    ["+who", "+ust", "+sst"],
                )

                # The former guest owns a fresh room and receives a new guest.
                create_room(next_host, next_decoder, "NextHost", 2)
                self._send(replacement_guest, "usea")
                self._recv_classic(replacement_guest, replacement_decoder, 2)
                self._send(
                    replacement_guest,
                    "ujoi",
                    (("NAME", "026.NextHost"),),
                )
                self._recv_classic(replacement_guest, replacement_decoder, 1)
                self._send(
                    replacement_guest,
                    "gjoi",
                    (("NAME", "NextHost"),),
                )
                staged_replacement = self._recv_classic(
                    next_host,
                    next_decoder,
                    2,
                )
                self.assertEqual(
                    staged_replacement[1].fields()["N"],
                    "ReplacementGuest",
                )
                self.assertEqual(staged_replacement[1].fields()["G"], "0")
                self._recv_classic(
                    replacement_guest,
                    replacement_decoder,
                    5,
                )
                replacement_context = next(
                    context
                    for context in app.mw.prelogin._connections.values()
                    if context.auth.persona == "ReplacementGuest"
                )
                self.assertEqual(replacement_context.mw_join_pending_game_id, 2)

                # Reproduce the live failure: the host resolves the stale
                # previous persona instead of the sole G=0 replacement member.
                self._send(
                    next_host,
                    "onln",
                    (("PERS", "PreviousHost"),),
                )
                replacement_promotion = self._recv_classic(
                    next_host,
                    next_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in replacement_promotion],
                    ["onln", "+usm", "+mgm"],
                )
                self.assertEqual(
                    replacement_promotion[0].fields()["N"],
                    "ReplacementGuest",
                )
                self.assertEqual(replacement_promotion[0].fields()["G"], "2")
                self.assertEqual(replacement_promotion[1].fields()["G"], "2")
                self.assertEqual(
                    replacement_promotion[2].fields()["OPPO1"],
                    "ReplacementGuest",
                )
                local_promotion = self._recv_classic(
                    replacement_guest,
                    replacement_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in local_promotion],
                    ["+who", "+usm", "+mgm"],
                )
                self.assertEqual(replacement_context.mw_join_pending_game_id, 0)

                # Cover the more common variant too: the host stays in the
                # same room, the promoted guest leaves, and a different guest
                # takes that slot while the host still asks for the old name.
                self._send(
                    replacement_guest,
                    "glea",
                    (("NAME", "NextHost"),),
                )
                self._recv_classic(replacement_guest, replacement_decoder, 1)
                departure_notice = self._recv_classic(
                    next_host,
                    next_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in departure_notice],
                    ["+usm", "+mgm"],
                )
                self._send(
                    replacement_guest,
                    "ulea",
                    (("NAME", "026.NextHost"),),
                )
                self._recv_classic(replacement_guest, replacement_decoder, 4)
                self._recv_classic(next_host, next_decoder, 3)

                self._send(previous_host, "usea")
                self._recv_classic(previous_host, previous_decoder, 2)
                self._send(
                    previous_host,
                    "ujoi",
                    (("NAME", "026.NextHost"),),
                )
                self._recv_classic(previous_host, previous_decoder, 1)
                self._send(
                    previous_host,
                    "gjoi",
                    (("NAME", "NextHost"),),
                )
                second_staged_guest = self._recv_classic(
                    next_host,
                    next_decoder,
                    2,
                )
                self.assertEqual(
                    second_staged_guest[1].fields()["N"],
                    "PreviousHost",
                )
                self._recv_classic(previous_host, previous_decoder, 5)
                self._send(
                    next_host,
                    "onln",
                    (("PERS", "ReplacementGuest"),),
                )
                second_promotion = self._recv_classic(
                    next_host,
                    next_decoder,
                    3,
                )
                self.assertEqual(
                    [frame.command for frame in second_promotion],
                    ["onln", "+usm", "+mgm"],
                )
                self.assertEqual(
                    second_promotion[0].fields()["N"],
                    "PreviousHost",
                )
                self._recv_classic(previous_host, previous_decoder, 3)
            finally:
                if previous_host is not None:
                    previous_host.close()
                if next_host is not None:
                    next_host.close()
                if replacement_guest is not None:
                    replacement_guest.close()
                app.stop()

    def test_authenticated_lobby_survives_connection_poll_timeout(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                self._settings(root),
                connection_timeout=0.05,
            )
            app = ClassicOnlineApplication(settings)
            app.credentials.create_account(
                "IdleAccount",
                "secret",
                persona="IdleDriver",
            )
            app.start()
            lobby = None
            try:
                lobby, _news = self._login(
                    app.u2.bootstrap_listener.bound_endpoint,
                    "IdleAccount",
                    "IdleDriver",
                    "secret",
                )
                time.sleep(0.2)
                self._assert_empty_game_search(lobby)
            finally:
                if lobby is not None:
                    lobby.close()
                app.stop()

    def test_authenticated_classic_lobbies_receive_periodic_heartbeat(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = replace(
                self._settings(Path(temporary)),
                connection_timeout=0.02,
                classic_lobby_heartbeat_interval=0.2,
            )
            app = ClassicOnlineApplication(settings)
            for suffix in ("U2", "MW"):
                app.credentials.create_account(
                    f"Heartbeat{suffix}Account",
                    "secret",
                    persona=f"Heartbeat{suffix}Driver",
                )
            app.start()
            lobbies = []
            try:
                cases = (
                    (
                        "U2",
                        app.u2.bootstrap_listener.bound_endpoint,
                        "HeartbeatU2Account",
                        "HeartbeatU2Driver",
                    ),
                    (
                        "MW",
                        app.mw.bootstrap_listener.bound_endpoint,
                        "HeartbeatMWAccount",
                        "HeartbeatMWDriver",
                    ),
                )
                for label, endpoint, account, persona in cases:
                    with self.subTest(game=label):
                        lobby, _news = self._login(
                            endpoint,
                            account,
                            persona,
                            "secret",
                        )
                        lobbies.append(lobby)
                        decoder = ClassicEAStreamDecoder()

                        # Client traffic restarts the shared heartbeat deadline.
                        time.sleep(0.08)
                        self._send(lobby, "skey")
                        self.assertEqual(
                            self._recv_classic(lobby, decoder)[0].command,
                            "skey",
                        )
                        lobby.settimeout(0.1)
                        with self.assertRaises(socket.timeout):
                            lobby.recv(8192)

                        lobby.settimeout(0.3)
                        heartbeat = decoder.feed(lobby.recv(8192))[0]
                        if label == "MW":
                            self.assertEqual(heartbeat.command, "~png")
                            self.assertRegex(
                                heartbeat.fields()["REF"],
                                r"^\d{4}\.\d{1,2}\.\d{1,2}-\d{2}:\d{2}:\d{2}$",
                            )
                            self.assertEqual(heartbeat.reserved, 0)
                            self.assertTrue(heartbeat.payload.endswith(b"\x00"))
                            self.assertNotIn(b"\n", heartbeat.payload[:-1])
                        else:
                            expected = ClassicEAFrame.signed("@cnt", b"\x00", 9)
                            self.assertEqual(heartbeat.command, "@cnt")
                            self.assertEqual(heartbeat.payload, expected.payload)
                            self.assertEqual(heartbeat.total_length, 21)
            finally:
                for lobby in lobbies:
                    lobby.close()
                app.stop()

    def test_pre_auth_classic_lobbies_receive_periodic_heartbeat(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = replace(
                self._settings(Path(temporary)),
                connection_timeout=0.02,
                classic_lobby_heartbeat_interval=0.2,
            )
            app = ClassicOnlineApplication(settings)
            app.start()
            lobbies = []
            try:
                cases = (
                    ("U2", app.u2.lobby_listener.bound_endpoint),
                    ("MW", app.mw.lobby_listener.bound_endpoint),
                )
                for label, endpoint in cases:
                    with self.subTest(game=label):
                        lobby = socket.create_connection(
                            (endpoint.host, endpoint.port),
                            timeout=2,
                        )
                        lobbies.append(lobby)
                        decoder = ClassicEAStreamDecoder()

                        # Pre-auth traffic restarts the same shared heartbeat
                        # deadline used after a successful login.
                        self._send(
                            lobby,
                            "addr",
                            (("ADDR", "127.0.0.1"), ("PORT", 65000)),
                        )
                        self.assertEqual(
                            self._recv_classic(lobby, decoder)[0].command,
                            "addr",
                        )
                        lobby.settimeout(0.1)
                        with self.assertRaises(socket.timeout):
                            lobby.recv(8192)

                        lobby.settimeout(0.3)
                        heartbeat = decoder.feed(lobby.recv(8192))[0]
                        if label == "MW":
                            self.assertEqual(heartbeat.command, "~png")
                            self.assertRegex(
                                heartbeat.fields()["REF"],
                                r"^\d{4}\.\d{1,2}\.\d{1,2}-\d{2}:\d{2}:\d{2}$",
                            )
                            self.assertEqual(heartbeat.reserved, 0)
                            self.assertTrue(heartbeat.payload.endswith(b"\x00"))
                            self.assertNotIn(b"\n", heartbeat.payload[:-1])
                        else:
                            expected = ClassicEAFrame.signed("@cnt", b"\x00", 9)
                            self.assertEqual(heartbeat.command, "@cnt")
                            self.assertEqual(heartbeat.payload, expected.payload)
                            self.assertEqual(heartbeat.total_length, 21)
            finally:
                for lobby in lobbies:
                    lobby.close()
                app.stop()

    def test_mw_stock_callback_starts_race_with_exactly_two_players(self) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.credentials.create_account(
                "MWHostAccount", "secret", persona="MWHost"
            )
            app.credentials.create_account(
                "MWGuestAccount", "secret", persona="MWGuest"
            )
            app.start()
            host = guest = callback = None
            try:
                endpoint = app.mw.bootstrap_listener.bound_endpoint
                host, _ = self._login(
                    endpoint, "MWHostAccount", "MWHost", "secret"
                )
                guest, _ = self._login(
                    endpoint, "MWGuestAccount", "MWGuest", "secret"
                )
                host_decoder = ClassicEAStreamDecoder()
                guest_decoder = ClassicEAStreamDecoder()
                callback_decoder = ClassicEAStreamDecoder()

                self._assert_mw_userset_create(host, "MWHost")
                self._send(guest, "usea")
                self._recv_classic(guest, guest_decoder, 2)
                self._send(guest, "ujoi", (("NAME", "026.MWHost"),))
                self._recv_classic(guest, guest_decoder)
                self._send(guest, "gjoi", (("NAME", "MWHost"),))
                self._recv_classic(host, host_decoder, 2)
                local_join = self._recv_classic(guest, guest_decoder, 5)
                self.assertEqual(
                    [frame.command for frame in local_join],
                    ["+usm", "gjoi", "+who", "+ust", "+usm"],
                )
                self._send(host, "onln", (("PERS", "MWGuest"),))
                self._recv_classic(host, host_decoder, 3)
                self.assertEqual(
                    [
                        frame.command
                        for frame in self._recv_classic(
                            guest,
                            guest_decoder,
                            3,
                        )
                    ],
                    ["+who", "+usm", "+mgm"],
                )
                self._send(
                    guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aV%3d20%0a"),),
                )
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder, 1)[0].command,
                    "auxi",
                )
                host_aux_update = self._recv_classic(
                    host,
                    host_decoder,
                    1,
                )[0]
                self.assertEqual(host_aux_update.command, "+usm")
                self.assertEqual(host_aux_update.fields()["N"], "MWGuest")

                self._send(
                    guest,
                    "mesg",
                    (("TEXT", "42"), ("ATTR", "EGS")),
                )
                guest_egs = self._recv_classic(guest, guest_decoder, 2)
                host_egs = self._recv_classic(host, host_decoder, 1)
                self.assertEqual(guest_egs[0].command, "mesg")
                self.assertEqual(guest_egs[1].fields()["F"], "EGSU")
                self.assertEqual(guest_egs[1].fields()["U"], "")
                self.assertEqual(host_egs[0].fields()["F"], "EGS")
                self.assertNotIn("A", host_egs[0].fields())

                callback = socket.create_connection(
                    (
                        app.mw.extra_lobby_listeners[0].bound_endpoint.host,
                        app.mw.extra_lobby_listeners[0].bound_endpoint.port,
                    ),
                    timeout=2,
                )
                callback.sendall(
                    ClassicEAFrame.from_fields(
                        "MESG",
                        (
                            ("GAME", 1),
                            ("FLAGS", "EGS"),
                            ("NAME", "MWGuest"),
                            ("TEXT", "42"),
                        ),
                    ).encode()
                )
                duplicate_egs_ack = self._recv_classic(
                    callback, callback_decoder
                )[0]
                self.assertEqual(duplicate_egs_ack.command, "MESG")
                callback.sendall(
                    ClassicEAFrame.from_fields(
                        "MESG",
                        (
                            ("GAME", 1),
                            ("FLAGS", "EGT"),
                            ("NAME", "MWHost"),
                            ("TEXT", "TIME%3d1%0aPAUSE%3d0%0aHURRY%3d1"),
                        ),
                    ).encode()
                )
                callback_ack = self._recv_classic(
                    callback, callback_decoder
                )[0]
                self.assertEqual(callback_ack.command, "MESG")
                self.assertEqual(
                    self._recv_classic(host, host_decoder)[0].fields()["F"],
                    "EGTU",
                )
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder)[0].fields()["F"],
                    "EGT",
                )

                self._send(
                    guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aLT%3d1%0aV%3d20%0a"),),
                )
                guest_ready_refresh = self._recv_classic(
                    guest, guest_decoder, 3
                )
                host_ready_refresh = self._recv_classic(
                    host, host_decoder, 1
                )
                self.assertEqual(
                    [frame.command for frame in guest_ready_refresh],
                    ["auxi", "+who", "+usm"],
                )
                self.assertEqual(host_ready_refresh[0].command, "+usm")
                self.assertEqual(
                    guest_ready_refresh[1].fields()["N"], "MWGuest"
                )
                self.assertEqual(
                    host_ready_refresh[0].fields()["N"], "MWGuest"
                )

                self._send(
                    host,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aCE%3d3,2%0aV%3d20%0a"),),
                )
                completed_host = self._recv_classic(
                    host, host_decoder, 1
                )[0]
                completed_guest = self._recv_classic(
                    guest, guest_decoder, 1
                )[0]
                self.assertEqual(completed_host.command, "auxi")
                self.assertIn("CE%3d3,2", completed_host.fields()["TEXT"])
                self.assertEqual(completed_guest.command, "+usm")
                self.assertIn("CE%3d3,2", completed_guest.fields()["X"])
                host_post_complete = self._recv_classic(
                    host, host_decoder, 2
                )
                self.assertEqual(
                    [frame.command for frame in host_post_complete],
                    ["+who", "+usm"],
                )

                callback_token = 0xFFFFC832
                callback.sendall(
                    ClassicEAFrame.from_fields(
                        "GSTA",
                        (
                            ("CALLUSER", 1),
                            ("CALLPING", 1),
                            ("CALLADDR", "127.0.0.1"),
                            ("NAME", "MWHost"),
                        ),
                        reserved=callback_token,
                    ).encode()
                )
                callback.settimeout(3)
                token_ack = b""
                while len(token_ack) < 13:
                    token_ack += callback.recv(13 - len(token_ack))
                self.assertEqual(
                    token_ack[:4], callback_token.to_bytes(4, "big")
                )
                self.assertEqual(token_ack[8:12], (13).to_bytes(4, "big"))
                callback_rows = self._recv_classic(
                    callback, callback_decoder, 3
                )
                self.assertEqual(
                    [frame.command for frame in callback_rows],
                    ["+usr", "+usr", "+gam"],
                )

                host_start = self._recv_classic(host, host_decoder, 5)
                guest_start = self._recv_classic(guest, guest_decoder, 5)
                for frames, persona in (
                    (host_start, "MWHost"),
                    (guest_start, "MWGuest"),
                ):
                    self.assertEqual(
                        [frame.command for frame in frames],
                        ["+who", "+usm", "+usm", "+mgm", "+ses"],
                    )
                    self.assertEqual(frames[0].fields()["F"], "GU")
                    self.assertEqual(frames[3].fields()["COUNT"], "2")
                    self.assertEqual(frames[3].fields()["MINSIZE"], "2")
                    self.assertEqual(frames[3].fields()["SYSFLAGS"], "524288")
                    self.assertEqual(frames[4].fields()["SELF"], persona)
                    self.assertNotEqual(
                        frames[4].fields()["ADDR0"],
                        frames[4].fields()["ADDR1"],
                    )
                self.assertEqual(host_start[4].fields()["OPID0"], "1")
                self.assertEqual(host_start[4].fields()["OPID1"], "2")
                self.assertEqual(guest_start[4].fields()["OPID0"], "1")
                self.assertEqual(guest_start[4].fields()["OPID1"], "2")
                self.assertNotIn("RLYHOST", host_start[4].fields())
                self.assertNotIn("RLYPORT", host_start[4].fields())
                self.assertNotIn("RLYHOST", guest_start[4].fields())
                self.assertNotIn("RLYPORT", guest_start[4].fields())
            finally:
                if host is not None:
                    host.close()
                if guest is not None:
                    guest.close()
                if callback is not None:
                    callback.close()
                app.stop()

    def test_mw_recreate_hands_previous_transport_to_replacement(self) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.credentials.create_account(
                "RecreateHostAccount", "secret", persona="RecreateHost"
            )
            app.credentials.create_account(
                "RecreateGuestAccount", "secret", persona="RecreateGuest"
            )
            app.start()
            host = guest = None
            try:
                endpoint = app.mw.bootstrap_listener.bound_endpoint
                host, _ = self._login(
                    endpoint,
                    "RecreateHostAccount",
                    "RecreateHost",
                    "secret",
                )
                guest, _ = self._login(
                    endpoint,
                    "RecreateGuestAccount",
                    "RecreateGuest",
                    "secret",
                )
                host_decoder = ClassicEAStreamDecoder()
                guest_decoder = ClassicEAStreamDecoder()

                self._assert_mw_userset_create(host, "RecreateHost")
                self._send(guest, "usea")
                self._recv_classic(guest, guest_decoder, 2)
                self._send(guest, "ujoi", (("NAME", "026.RecreateHost"),))
                self._recv_classic(guest, guest_decoder)
                self._send(guest, "gjoi", (("NAME", "RecreateHost"),))
                self._recv_classic(host, host_decoder, 2)
                local_join = self._recv_classic(guest, guest_decoder, 5)
                self.assertEqual(
                    [frame.command for frame in local_join],
                    ["+usm", "gjoi", "+who", "+ust", "+usm"],
                )
                self._send(host, "onln", (("PERS", "RecreateGuest"),))
                self._recv_classic(host, host_decoder, 3)
                self.assertEqual(
                    [
                        frame.command
                        for frame in self._recv_classic(
                            guest,
                            guest_decoder,
                            3,
                        )
                    ],
                    ["+who", "+usm", "+mgm"],
                )
                self._send(
                    guest,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aV%3d20%0a"),),
                )
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder, 1)[0].command,
                    "auxi",
                )
                guest_ready_update = self._recv_classic(
                    host,
                    host_decoder,
                )[0]
                self.assertEqual(guest_ready_update.command, "+usm")

                previous = app.mw.prelogin.sessions.get_game(1)
                self.assertIsNotNone(previous)
                previous_addresses = dict(previous.participant_race_addresses)
                previous_order = previous.ordered_participants()
                previous_token = app.race_relay._game_tokens[id(previous)]

                # A normal pre-race MYGAME subscription stays a plain
                # selection acknowledgement. The full active snapshot is
                # gated by the explicit post-race INGAME=0 transition.
                self._send(
                    guest,
                    "sele",
                    (("MYGAME", 1), ("USERSETS", 1)),
                )
                normal_select = self._recv_classic(
                    guest,
                    guest_decoder,
                )
                self.assertEqual(len(normal_select), 1)
                self.assertEqual(normal_select[0].command, "sele")

                # When the guest exits before the owner, stock MW keeps both
                # users on the active game and replays that room snapshot.
                app.mw.prelogin.sessions.set_state(1, SessionState.ACTIVE)
                self._send(guest, "sele", (("INGAME", 0),))
                self._recv_classic(guest, guest_decoder)
                self._send(
                    guest,
                    "sele",
                    (("MYGAME", 1), ("USERSETS", 1)),
                )
                phase1 = self._recv_classic(guest, guest_decoder, 6)
                self.assertEqual(phase1[0].command, "sele")
                self.assertEqual(phase1[1].command, "+ust")
                self.assertEqual(phase1[1].fields()["C"], "2")
                self.assertEqual(phase1[2].command, "+usm")
                self.assertEqual(phase1[2].fields()["N"], "RecreateHost")
                self.assertEqual(phase1[2].fields()["F"], "G")
                self.assertEqual(phase1[2].fields()["G"], "1")
                self.assertEqual(phase1[3].command, "+usm")
                self.assertEqual(phase1[3].fields()["N"], "RecreateGuest")
                self.assertEqual(phase1[3].fields()["F"], "G")
                self.assertEqual(phase1[3].fields()["G"], "1")
                self.assertEqual(phase1[4].command, "+mgm")
                self.assertEqual(phase1[4].fields()["IDENT"], "1")
                self.assertEqual(phase1[4].fields()["SYSFLAGS"], "524288")
                self.assertEqual(phase1[5].command, "+sst")
                self.assertEqual(phase1[5].fields()["UIG"], "2")
                self.assertEqual(phase1[5].fields()["GIP"], "1")

                # The official guest-first flow acknowledges the report with
                # the still-active session before publishing the G=0 room
                # view.  That two-phase boundary prevents the client from
                # searching for and joining the completed game.
                self._send(guest, "rank", (("TIME", 90),))
                report = self._recv_classic(guest, guest_decoder, 2)
                self.assertEqual(
                    [frame.command for frame in report],
                    ["rank", "+ses"],
                )
                self.assertEqual(report[1].fields()["IDENT"], "1")
                self.assertEqual(report[1].fields()["SYSFLAGS"], "524288")
                self.assertEqual(report[1].fields()["SELF"], "RecreateGuest")

                room_view = self._recv_classic(guest, guest_decoder, 4)
                self.assertEqual(
                    [frame.command for frame in room_view],
                    ["+who", "+usm", "+usm", "+mgm"],
                )
                self.assertEqual(room_view[0].fields()["G"], "0")
                self.assertEqual(room_view[1].fields()["N"], "RecreateHost")
                self.assertEqual(room_view[1].fields()["G"], "0")
                self.assertEqual(room_view[2].fields()["N"], "RecreateGuest")
                self.assertEqual(room_view[2].fields()["G"], "0")
                self.assertEqual(room_view[3].fields(), {"IDENT": "1"})
                host_room_view = self._recv_classic(
                    host,
                    host_decoder,
                )[0]
                self.assertEqual(host_room_view.command, "+who")
                self.assertEqual(host_room_view.fields()["G"], "0")

                # MW crosses INGAME=1 once more before the guest's final
                # lobby subscription.  That transition clears its local
                # member list, so the guest-first path must replay the G=0
                # room view while the owner is still finishing the race.
                self._send(guest, "sele", (("INGAME", 1),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder)[0].command,
                    "sele",
                )
                self._send(guest, "sele", (("INGAME", 0),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder)[0].command,
                    "sele",
                )
                self._send(
                    guest,
                    "sele",
                    (("MYGAME", 1), ("USERSETS", 1)),
                )
                waiting_room = self._recv_classic(guest, guest_decoder, 7)
                self.assertEqual(
                    [frame.command for frame in waiting_room],
                    ["sele", "+who", "+ust", "+usm", "+usm", "+mgm", "+sst"],
                )
                self.assertEqual(waiting_room[1].fields()["N"], "RecreateGuest")
                self.assertEqual(waiting_room[1].fields()["G"], "0")
                self.assertEqual(waiting_room[2].fields()["C"], "2")
                self.assertEqual(waiting_room[3].fields()["N"], "RecreateHost")
                self.assertEqual(waiting_room[3].fields()["G"], "0")
                self.assertEqual(waiting_room[4].fields()["N"], "RecreateGuest")
                self.assertEqual(waiting_room[4].fields()["G"], "0")
                self.assertEqual(waiting_room[5].fields(), {"IDENT": "1"})
                self.assertEqual(waiting_room[6].fields()["UIL"], "2")
                self.assertEqual(waiting_room[6].fields()["UIG"], "0")

                guest_context = next(
                    candidate
                    for candidate in app.mw.prelogin._connections.values()
                    if candidate.auth.persona == "RecreateGuest"
                )
                self.assertEqual(guest_context.mw_join_pending_game_id, 0)
                self.assertEqual(guest_context.mw_postrace_snapshot_game_id, 0)
                self.assertEqual(guest_context.mw_postrace_room_view_game_id, 1)
                self.assertEqual(guest_context.mw_deferred_gjoi_game_id, 0)
                self.assertEqual(previous.state, SessionState.ACTIVE)

                # The following onln lookup is what made the stock client
                # restart GRWM after the otherwise idempotent gjoi.  During
                # post-race room re-entry every participant is advertised in
                # room view (G=0), not as an active-race endpoint.
                self._send(
                    guest,
                    "onln",
                    (("PERS", "RecreateHost"),),
                )
                same_room_online = self._recv_classic(
                    guest,
                    guest_decoder,
                )[0]
                self.assertEqual(same_room_online.command, "onln")
                self.assertEqual(same_room_online.fields()["N"], "RecreateHost")
                self.assertEqual(same_room_online.fields()["G"], "0")
                self.assertEqual(same_room_online.fields()["US"], "026.RecreateHost")
                self.assertEqual(guest_context.mw_join_pending_game_id, 0)

                # 3playerroom&race has the guest search at 358.162s while the
                # owner does not recreate until 382.440s.  Stock answers the
                # active userset immediately instead of leaving usea pending.
                self._send(
                    guest,
                    "usea",
                    (
                        ("NAME", "026.RecreateHost"),
                        ("START", 0),
                        ("COUNT", 1),
                        ("CUSTFLAGS", 0),
                        ("CUSTMASK", 0),
                    ),
                )
                active_search = self._recv_classic(
                    guest,
                    guest_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in active_search],
                    ["usea", "+uss"],
                )
                self.assertEqual(active_search[0].fields()["COUNT"], "1")
                self.assertEqual(active_search[1].fields()["C"], "2")
                self.assertEqual(guest_context.mw_deferred_usea_game_id, 0)

                # The early gjoi itself is answered as well.  Retail sends a
                # 13-byte gjoi frame with the reserved marker "ugam", then
                # retries after +usm/+ust announces the replacement game.
                self._send(
                    guest,
                    "gjoi",
                    (("NAME", "RecreateHost"),),
                )
                unavailable = self._recv_classic(
                    guest,
                    guest_decoder,
                )[0]
                self.assertEqual(unavailable.command, "gjoi")
                self.assertEqual(unavailable.reserved, 0x7567616D)
                self.assertEqual(unavailable.payload, b"\x00")
                self.assertEqual(guest_context.mw_deferred_gjoi_game_id, 1)

                # The first phase-2 retirement is global to the old game.
                # When the owner reaches the lobby later, retail MW receives
                # only the normal selection acknowledgements and proceeds to
                # gcre; replaying phase 1 for the owner pushes the guest back
                # through INGAME=1 and causes a second stale gjoi cycle.
                self._send(host, "sele", (("INGAME", 0),))
                owner_ingame = self._recv_classic(host, host_decoder)
                self.assertEqual(
                    [frame.command for frame in owner_ingame],
                    ["sele"],
                )
                self._send(
                    host,
                    "sele",
                    (("MYGAME", 1), ("USERSETS", 1)),
                )
                owner_room_select = self._recv_classic(host, host_decoder)
                self.assertEqual(
                    [frame.command for frame in owner_room_select],
                    ["sele"],
                )
                self._send(host, "rank", (("TIME", 95),))
                owner_room_report = self._recv_classic(host, host_decoder)
                self.assertEqual(
                    [frame.command for frame in owner_room_report],
                    ["rank"],
                )
                self._send(
                    host,
                    "auxi",
                    (("TEXT", "SCF%3d0%0aLT%3d0%0aV%3d20%0a"),),
                )
                owner_room_aux = self._recv_classic(host, host_decoder)
                self.assertEqual(
                    [frame.command for frame in owner_room_aux],
                    ["auxi"],
                )

                self._send(
                    host,
                    "gcre",
                    (
                        ("NAME", "RecreateHost-2"),
                        ("MAXSIZE", 4),
                        ("MINSIZE", 2),
                        ("SYSFLAGS", 0),
                    ),
                )
                recreated = self._recv_classic(host, host_decoder, 3)[2]
                self.assertEqual(recreated.command, "gcre")
                self.assertEqual(recreated.fields()["IDENT"], "2")
                self.assertIsNone(app.mw.prelogin.sessions.get_game(1))
                replacement_games = app.mw.prelogin.sessions.list_games()
                self.assertEqual(len(replacement_games), 1)
                self.assertEqual(replacement_games[0].game_id, 2)
                self.assertEqual(replacement_games[0].name, "RecreateHost-2")

                # The owner has recreated the game while the guest is still
                # only a member of the host-owned userset. Its immediate
                # uadm snapshot must show that waiting guest at G=0 without
                # adding it to the replacement game before gjoi.
                self._send(
                    host,
                    "uadm",
                    (("NAME", "026.RecreateHost"),),
                )
                owner_waiting_view = self._recv_classic(
                    host,
                    host_decoder,
                    7,
                )
                self.assertEqual(
                    [frame.command for frame in owner_waiting_view],
                    ["uadm", "+who", "+ust", "+usm", "+usm", "+mgm", "+sst"],
                )
                self.assertEqual(owner_waiting_view[3].fields()["N"], "RecreateGuest")
                self.assertEqual(owner_waiting_view[3].fields()["G"], "0")
                self.assertEqual(owner_waiting_view[4].fields()["N"], "RecreateHost")
                self.assertEqual(owner_waiting_view[4].fields()["G"], "2")
                self.assertEqual(owner_waiting_view[6].fields()["UIL"], "1")
                self.assertEqual(owner_waiting_view[6].fields()["UIG"], "1")
                guest_user_id = next(
                    user_id
                    for user_id in previous.participants
                    if user_id != previous.owner_id
                )
                self.assertNotIn(
                    guest_user_id,
                    replacement_games[0].participants,
                )

                replacement_bridge = self._recv_classic(
                    guest,
                    guest_decoder,
                    2,
                )
                self.assertEqual(
                    [frame.command for frame in replacement_bridge],
                    ["+usm", "+ust"],
                )
                self.assertEqual(replacement_bridge[0].fields()["N"], "RecreateHost")
                self.assertEqual(replacement_bridge[0].fields()["G"], "2")
                self.assertEqual(replacement_bridge[1].fields()["C"], "2")
                self.assertEqual(guest_context.mw_deferred_usea_game_id, 0)
                self.assertEqual(guest_context.mw_deferred_gjoi_game_id, 0)
                self.assertEqual(guest_context.lobby_game_id, 0)
                self._send(
                    guest,
                    "gjoi",
                    (("NAME", "RecreateHost-2"),),
                )
                completed = self._recv_classic(guest, guest_decoder, 5)
                self.assertEqual(
                    [frame.command for frame in completed],
                    ["+usm", "gjoi", "+who", "+ust", "+usm"],
                )
                self.assertEqual(completed[0].fields()["N"], "RecreateHost")
                self.assertEqual(completed[0].fields()["G"], "2")
                self.assertEqual(completed[1].fields()["IDENT"], "2")
                self.assertEqual(completed[1].fields()["COUNT"], "2")
                self.assertEqual(guest_context.lobby_game_id, 2)
                self.assertEqual(guest_context.mw_join_pending_game_id, 2)
                self.assertEqual(guest_context.mw_deferred_gjoi_game_id, 0)

                replacement = app.mw.prelogin.sessions.get_game(2)
                self.assertIsNotNone(replacement)
                self.assertEqual(len(replacement.participants), 2)
                self.assertEqual(
                    replacement.participant_race_addresses,
                    previous_addresses,
                )
                self.assertEqual(
                    replacement.ordered_participants(),
                    previous_order,
                )
                self.assertEqual(
                    app.race_relay._game_tokens[id(replacement)],
                    previous_token,
                )
                self.assertNotIn(id(previous), app.race_relay._game_tokens)

                owner_update = self._recv_classic(host, host_decoder, 2)
                self.assertEqual(owner_update[0].command, "+ust")
                self.assertEqual(owner_update[0].fields()["C"], "2")
                self.assertEqual(owner_update[1].command, "+usm")
                self.assertEqual(owner_update[1].fields()["N"], "RecreateGuest")
                self.assertEqual(owner_update[1].fields()["G"], "0")

                # Stable virtual identities now belong to the replacement
                # token, while all client-owned physical UDP endpoints wait
                # to be learned again from the new post-race sockets.
                for user_id, address in previous_addresses.items():
                    identity = (previous_token, user_id)
                    self.assertEqual(
                        app.race_relay._virtual_to_identity[address],
                        identity,
                    )
                    self.assertNotIn(
                        identity,
                        app.race_relay._identity_to_endpoint,
                    )

                # The userset's current game is authoritative when a guest's
                # lobby context retained the previous race id. The post-race
                # snapshot must use game 2, not the stale game 1.
                app.mw.prelogin.sessions.set_state(2, SessionState.ACTIVE)
                guest_context.lobby_game_id = 1
                self._send(guest, "sele", (("INGAME", 0),))
                for _ in range(8):
                    stale_context_reply = self._recv_classic(
                        guest,
                        guest_decoder,
                    )[0]
                    if stale_context_reply.command == "sele":
                        break
                else:
                    self.fail("stale game correction did not acknowledge sele")
                self.assertEqual(guest_context.lobby_game_id, 2)
                self._send(
                    guest,
                    "sele",
                    (("MYGAME", 1), ("USERSETS", 1)),
                )
                corrected_replay = self._recv_classic(
                    guest,
                    guest_decoder,
                    6,
                )
                self.assertEqual(corrected_replay[4].command, "+mgm")
                self.assertEqual(corrected_replay[4].fields()["IDENT"], "2")

                # Owner gdel + udel must cascade both deletions to the guest.
                self._send(host, "gdel", (("NAME", "RecreateHost-2"),))
                self._recv_classic(host, host_decoder)
                game_reset = self._recv_classic(guest, guest_decoder, 4)
                self.assertEqual(game_reset[0].command, "+who")
                self.assertEqual(game_reset[0].fields()["G"], "0")
                self.assertEqual(game_reset[1].command, "+usm")
                self.assertEqual(game_reset[1].fields()["G"], "0")
                self.assertEqual(game_reset[2].command, "+usm")
                self.assertEqual(game_reset[2].fields()["G"], "0")
                self.assertEqual(game_reset[3].command, "+mgm")
                self.assertEqual(game_reset[3].fields()["IDENT"], "2")

                self._send(host, "udel", (("NAME", "026.RecreateHost"),))
                owner_delete = self._recv_classic(host, host_decoder, 4)
                self.assertEqual(owner_delete[2].command, "+ust")
                self.assertEqual(owner_delete[2].fields()["I"], "1")
                self.assertEqual(owner_delete[3].fields()["IDENT"], "2")
                userset_reset = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(userset_reset[0].command, "+who")
                self.assertEqual(userset_reset[0].fields()["US"], "")
                self.assertEqual(userset_reset[1].command, "+ust")
                self.assertEqual(userset_reset[1].fields()["I"], "1")
                self.assertEqual(userset_reset[2].command, "+sst")
                self.assertEqual(userset_reset[2].fields()["UIG"], "0")

                self.assertEqual(guest_context.lobby_game_id, 0)
                self.assertEqual(guest_context.userset_id, 0)
            finally:
                if host is not None:
                    host.close()
                if guest is not None:
                    guest.close()
                app.stop()

    def test_pre_auth_lobby_survives_connection_poll_timeout(self) -> None:
        with TemporaryDirectory() as temporary:
            settings = replace(
                self._settings(Path(temporary)),
                connection_timeout=0.05,
            )
            app = ClassicOnlineApplication(settings)
            app.start()
            try:
                endpoint = app.u2.lobby_listener.bound_endpoint
                with socket.create_connection(
                    (endpoint.host, endpoint.port),
                    timeout=2,
                ) as lobby:
                    decoder = ClassicEAStreamDecoder()
                    self._send(
                        lobby,
                        "addr",
                        (("ADDR", "127.0.0.1"), ("PORT", 65000)),
                    )
                    self.assertEqual(
                        self._recv_classic(lobby, decoder)[0].command,
                        "addr",
                    )
                    time.sleep(0.2)
                    self._send(lobby, "skey")
                    self.assertEqual(
                        self._recv_classic(lobby, decoder)[0].command,
                        "skey",
                    )
            finally:
                app.stop()

    def test_room_join_chat_ready_and_kick_are_broadcast(self) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.credentials.create_account("HostAccount", "secret", persona="HostDriver")
            app.credentials.create_account("GuestAccount", "secret", persona="GuestDriver")
            app.start()
            host = guest = None
            try:
                endpoint = app.u2.bootstrap_listener.bound_endpoint
                host, _ = self._login(endpoint, "HostAccount", "HostDriver", "secret")
                guest, _ = self._login(endpoint, "GuestAccount", "GuestDriver", "secret")
                host_decoder = ClassicEAStreamDecoder()
                guest_decoder = ClassicEAStreamDecoder()

                self._send(host, "auxi", (("TEXT", "host-car"),))
                self.assertEqual(
                    self._recv_classic(host, host_decoder)[0].command,
                    "auxi",
                )
                self._send(guest, "auxi", (("TEXT", "guest-car"),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder)[0].command,
                    "auxi",
                )

                self._send(
                    host,
                    "gcre",
                    (("NAME", "007.HostDriver"), ("MAXSIZE", 4)),
                )
                created = self._recv_classic(host, host_decoder, 3)[0]
                game_id = int(created.fields()["IDENT"])

                self._send(
                    host,
                    "mesg",
                    (
                        ("PRIV", "GuestDriver"),
                        ("ATTR", "EPQ"),
                        ("TEXT", "Join my game"),
                    ),
                )
                host_invite = self._recv_classic(host, host_decoder, 2)
                guest_invite = self._recv_classic(guest, guest_decoder, 1)[0]
                self.assertEqual(host_invite[0].command, "mesg")
                self.assertEqual(host_invite[0].fields()["ATTR"], "EPQ")
                self.assertEqual(host_invite[1].command, "+msg")
                self.assertEqual(host_invite[1].fields()["F"], "EPQ")
                self.assertNotIn("A", host_invite[1].fields())
                self.assertEqual(guest_invite.command, "+msg")
                self.assertEqual(guest_invite.fields()["F"], "EPQ")
                self.assertEqual(guest_invite.fields()["N"], "HostDriver")
                self.assertEqual(guest_invite.fields()["T"], "Join my game")
                self.assertNotIn("A", guest_invite.fields())

                self._send(guest, "gsea")
                search = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(search[2].fields()["IDENT"], str(game_id))

                self._send(guest, "gjoi", (("IDENT", game_id),))
                joined = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(joined[0].command, "gjoi")
                self.assertEqual(joined[0].fields()["COUNT"], "2")
                self.assertEqual(joined[1].fields()["X"], "guest-car")
                host_join_notice = self._recv_classic(host, host_decoder, 2)
                self.assertEqual(host_join_notice[0].command, "+who")
                self.assertEqual(host_join_notice[0].fields()["N"], "GuestDriver")
                self.assertEqual(host_join_notice[0].fields()["X"], "guest-car")
                self.assertEqual(host_join_notice[1].fields()["COUNT"], "2")

                self._send(guest, "onln", (("PERS", "HostDriver"),))
                host_presence = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(host_presence[0].command, "onln")
                self.assertEqual(host_presence[0].fields()["N"], "HostDriver")
                self.assertEqual(host_presence[0].fields()["X"], "host-car")
                self.assertEqual(host_presence[1].command, "+who")
                self.assertEqual(host_presence[1].fields()["X"], "host-car")
                self.assertEqual(host_presence[2].command, "+mgm")

                self._send(guest, "auxi", (("TEXT", "guest-car-updated"),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder)[0].command,
                    "auxi",
                )
                updated_car = self._recv_classic(host, host_decoder)[0]
                self.assertEqual(updated_car.command, "+who")
                self.assertEqual(updated_car.fields()["X"], "guest-car-updated")

                self._send(guest, "mesg", (("TEXT", "Hello from the room"),))
                guest_chat = self._recv_classic(guest, guest_decoder, 2)
                host_chat = self._recv_classic(host, host_decoder, 1)[0]
                self.assertEqual(guest_chat[0].command, "mesg")
                self.assertEqual(host_chat.command, "+msg")
                self.assertEqual(host_chat.fields()["T"], "Hello from the room")
                self.assertEqual(host_chat.fields()["N"], "GuestDriver")

                self._send(guest, "gset", (("USERFLAGS", U2_READY_FLAG),))
                guest_ready = self._recv_classic(guest, guest_decoder, 2)
                host_ready = self._recv_classic(host, host_decoder, 1)[0]
                self.assertEqual(guest_ready[0].command, "gset")
                self.assertEqual(host_ready.command, "+mgm")
                self.assertEqual(
                    host_ready.fields()["OPFLAG1"],
                    str(U2_READY_FLAG),
                )

                self._send(host, "gsta")
                host_start = self._recv_classic(host, host_decoder, 3)
                guest_start = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(host_start[0].command, "gsta")
                self.assertEqual(host_start[0].payload, b"\x00" * 9)
                self.assertEqual(host_start[1].command, "+mgm")
                self.assertEqual(host_start[2].command, "+ses")
                self.assertEqual(host_start[2].fields()["SELF"], "HostDriver")
                self.assertEqual(host_start[2].fields()["COUNT"], "2")
                self.assertEqual(host_start[2].fields()["SYSFLAGS"], "524288")
                self.assertEqual(host_start[2].fields()["NUMPART"], "1")
                self.assertEqual(host_start[2].fields()["OPPART0"], "0")
                self.assertEqual(host_start[2].fields()["OPPART1"], "0")
                start_order = [
                    item.split("=", 1)[0]
                    for item in host_start[1]
                    .payload.rstrip(b"\x00")
                    .decode("latin-1")
                    .split("\t")
                    if item
                ]
                self.assertEqual(
                    start_order,
                    [
                        "IDENT", "WHEN", "NAME", "HOST", "ROOM", "MAXSIZE",
                        "MINSIZE", "COUNT", "CUSTFLAGS", "SYSFLAGS", "EVID",
                        "EVGID", "NUMPART", "LIMIT", "FLAGS", "PARAMS",
                        "RLYHOST", "RLYPORT", "OPID0", "OPPO0", "ADDR0",
                        "LADDR0", "MADDR0", "OPPART0", "OPFLAG0", "OPPARAM0",
                        "OPID1", "OPPO1", "ADDR1", "LADDR1", "MADDR1",
                        "OPPART1", "OPFLAG1", "OPPARAM1", "PARTSIZE0",
                        "PARTPARAMS0",
                    ],
                )
                self.assertEqual(guest_start[0].command, "gsta")
                self.assertEqual(guest_start[0].payload, b"\x00" * 9)
                self.assertEqual(guest_start[2].fields()["SELF"], "GuestDriver")
                self.assertEqual(guest_start[2].fields()["OPPO0"], "GuestDriver")
                self.assertEqual(guest_start[2].fields()["OPPO1"], "HostDriver")
                self.assertEqual(guest_start[2].fields()["NUMPART"], "1")
                self.assertEqual(guest_start[2].fields()["OPPART0"], "0")
                self.assertEqual(guest_start[2].fields()["OPPART1"], "0")

                self._send(
                    host,
                    "gset",
                    (("NAME", "007.HostDriver"), ("KICK", "GuestDriver")),
                )
                host_kick = self._recv_classic(host, host_decoder, 2)
                guest_kick = self._recv_classic(guest, guest_decoder, 5)
                self.assertEqual(host_kick[0].command, "gset")
                self.assertEqual(host_kick[0].fields()["COUNT"], "1")
                self.assertEqual(host_kick[1].fields()["COUNT"], "1")
                self.assertEqual(guest_kick[0].command, "gset")
                self.assertEqual(guest_kick[0].fields()["KICK"], "GuestDriver")
                self.assertEqual(guest_kick[1].command, "+msg")
                self.assertEqual(
                    guest_kick[1].fields()["T"],
                    '"You have been kicked out of the room by HostDriver"',
                )
                self.assertEqual(guest_kick[1].fields()["N"], "Server")
                self.assertEqual(guest_kick[1].fields()["F"], "PU")
                self.assertEqual(guest_kick[2].command, "+who")
                self.assertEqual(guest_kick[2].fields()["G"], "0")
                self.assertEqual(guest_kick[3].command, "+mgm")
                self.assertEqual(guest_kick[3].fields()["IDENT"], "1")
                self.assertEqual(guest_kick[3].fields()["NAME"], "007.HostDriver")
                self.assertEqual(guest_kick[3].fields()["COUNT"], "1")
                self.assertEqual(guest_kick[4].command, "+sst")
                self.assertEqual(guest_kick[4].fields()["GIP"], "0")

                self._send(
                    guest,
                    "gset",
                    (("NAME", "007.HostDriver"), ("USERFLAGS", 0)),
                )
                guest_reset = self._recv_classic(guest, guest_decoder, 1)
                self.assertEqual(guest_reset[0].command, "gset")
                self.assertEqual(guest_reset[0].fields(), {})

                self._send(guest, "gsea")
                guest_search = self._recv_classic(guest, guest_decoder, 2)
                self.assertEqual(guest_search[0].fields()["COUNT"], "0")
                kicked_game = app.u2.prelogin.sessions.get_game(game_id)
                self.assertIsNotNone(kicked_game)
                self.assertEqual(len(kicked_game.kicked_participants), 1)

                self._send(
                    host,
                    "mesg",
                    (
                        ("PRIV", "GuestDriver"),
                        ("ATTR", "EPQ"),
                        ("TEXT", "Rejoin my game"),
                    ),
                )
                self._recv_classic(host, host_decoder, 2)
                self._recv_classic(guest, guest_decoder, 1)
                reinvited_game = app.u2.prelogin.sessions.get_game(game_id)
                self.assertIsNotNone(reinvited_game)
                self.assertEqual(reinvited_game.kicked_participants, set())

                self._send(host, "gdel", (("NAME", "007.HostDriver"),))
                self.assertEqual(
                    self._recv_classic(host, host_decoder, 1)[0].command,
                    "gdel",
                )

                self._send(
                    host,
                    "gcre",
                    (("NAME", "008.HostDriver"), ("MAXSIZE", 4)),
                )
                second_created = self._recv_classic(host, host_decoder, 3)[0]
                second_game_id = int(second_created.fields()["IDENT"])

                self._send(guest, "gsea")
                second_search = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(
                    second_search[2].fields()["IDENT"],
                    str(second_game_id),
                )
                self._send(guest, "gjoi", (("IDENT", second_game_id),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder, 3)[0].command,
                    "gjoi",
                )
                self._recv_classic(host, host_decoder, 2)

                self._send(host, "gdel", (("NAME", "008.HostDriver"),))
                host_delete = self._recv_classic(host, host_decoder, 1)[0]
                guest_closed = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(host_delete.command, "gdel")
                self.assertEqual(guest_closed[0].command, "+who")
                self.assertEqual(guest_closed[0].fields()["G"], "0")
                self.assertEqual(guest_closed[1].command, "+mgm")
                self.assertEqual(
                    guest_closed[1].fields()["IDENT"],
                    str(second_game_id),
                )
                self.assertNotIn("NAME", guest_closed[1].fields())
                self.assertEqual(guest_closed[2].command, "+sst")
                self.assertEqual(guest_closed[2].fields()["GIP"], "0")

                self._send(guest, "gsea")
                after_delete = self._recv_classic(guest, guest_decoder, 2)
                self.assertEqual(after_delete[0].fields()["COUNT"], "0")
            finally:
                if host is not None:
                    host.close()
                if guest is not None:
                    guest.close()
                app.stop()

    def test_private_and_password_games_require_authorization(self) -> None:
        with TemporaryDirectory() as temporary:
            app = ClassicOnlineApplication(self._settings(Path(temporary)))
            app.credentials.create_account(
                "PrivateHost",
                "secret",
                persona="PrivateHost",
            )
            app.credentials.create_account(
                "PrivateGuest",
                "secret",
                persona="PrivateGuest",
            )
            app.start()
            host = guest = None
            try:
                endpoint = app.u2.bootstrap_listener.bound_endpoint
                host, _ = self._login(
                    endpoint,
                    "PrivateHost",
                    "PrivateHost",
                    "secret",
                )
                guest, _ = self._login(
                    endpoint,
                    "PrivateGuest",
                    "PrivateGuest",
                    "secret",
                )
                host_decoder = ClassicEAStreamDecoder()
                guest_decoder = ClassicEAStreamDecoder()

                self._send(
                    host,
                    "gcre",
                    (
                        ("NAME", "Private.InviteOnly"),
                        ("MAXSIZE", 4),
                        ("CUSTFLAGS", -2147483264),
                    ),
                )
                private_game = self._recv_classic(host, host_decoder, 3)[0]
                private_game_id = int(private_game.fields()["IDENT"])

                self._send(guest, "gsea")
                hidden = self._recv_classic(guest, guest_decoder, 2)
                self.assertEqual(hidden[0].fields()["COUNT"], "0")

                self._send(
                    host,
                    "mesg",
                    (
                        ("PRIV", "PrivateGuest"),
                        ("ATTR", "EPQ"),
                        ("TEXT", "Private invite"),
                    ),
                )
                self._recv_classic(host, host_decoder, 2)
                self._recv_classic(guest, guest_decoder, 1)
                self._send(guest, "gsea")
                visible = self._recv_classic(guest, guest_decoder, 3)
                self.assertEqual(visible[0].fields()["COUNT"], "1")
                self.assertEqual(
                    visible[2].fields()["IDENT"],
                    str(private_game_id),
                )
                self._send(guest, "gjoi", (("IDENT", private_game_id),))
                self.assertEqual(
                    self._recv_classic(guest, guest_decoder, 3)[0].command,
                    "gjoi",
                )
                self._recv_classic(host, host_decoder, 2)

                self._send(host, "gdel")
                self._recv_classic(host, host_decoder, 1)
                self._recv_classic(guest, guest_decoder, 3)

                self._send(
                    host,
                    "gcre",
                    (
                        ("NAME", "Private.Password"),
                        ("MAXSIZE", 4),
                        ("CUSTFLAGS", -2147483520),
                        ("PASS", "open-sesame"),
                    ),
                )
                password_game = self._recv_classic(host, host_decoder, 3)[0]
                password_game_id = int(password_game.fields()["IDENT"])

                self._send(guest, "gsea")
                password_listing = self._recv_classic(
                    guest,
                    guest_decoder,
                    3,
                )
                self.assertEqual(password_listing[0].fields()["COUNT"], "1")
                self.assertNotIn("PASS", password_listing[2].fields())
                self.assertEqual(
                    password_listing[2].fields()["SYSFLAGS"],
                    "65536",
                )

                self._send(
                    guest,
                    "gjoi",
                    (("IDENT", password_game_id), ("PASS", "wrong")),
                )
                rejected = self._recv_classic(guest, guest_decoder, 1)[0]
                self.assertEqual(rejected.tag, "gjoipass")

                self._send(
                    guest,
                    "gjoi",
                    (
                        ("IDENT", password_game_id),
                        ("PASS", "open-sesame"),
                    ),
                )
                accepted = self._recv_classic(guest, guest_decoder, 3)[0]
                self.assertEqual(accepted.command, "gjoi")
                self.assertEqual(
                    accepted.fields()["IDENT"],
                    str(password_game_id),
                )
            finally:
                if host is not None:
                    host.close()
                if guest is not None:
                    guest.close()
                app.stop()

    def test_live_ban_drains_stock_rejection_then_closes_mw_lobby(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = replace(
                self._settings(root),
                enable_u2=False,
                account_db_path=str(root / "accounts.sqlite3"),
                account_files_path=str(root / "users"),
            )
            app = ClassicOnlineApplication(settings)
            self.assertIsNotNone(app.account_database)
            app.credentials.create_account(
                "BannedAccount",
                "secret",
                persona="BannedDriver",
            )
            app.start()
            lobby: socket.socket | None = None
            try:
                lobby, _news = self._login(
                    app.mw.bootstrap_listener.bound_endpoint,
                    "BannedAccount",
                    "BannedDriver",
                    "secret",
                )
                assert app.account_database is not None
                app.account_database.set_banned("BannedAccount", True)

                policy = self._recv_classic(
                    lobby,
                    ClassicEAStreamDecoder(),
                    1,
                )[0]
                self.assertEqual(policy.command, "auth")
                self.assertEqual(policy.reserved, ERROR_IMST)

                lobby.settimeout(0.25)
                with self.assertRaises(socket.timeout):
                    lobby.recv(8192)
                lobby.settimeout(3.0)
                try:
                    closed = lobby.recv(8192)
                except ConnectionResetError:
                    closed = b""
                self.assertEqual(closed, b"")
                with app.account_database.connect() as connection:
                    active = int(
                        connection.execute(
                            "SELECT COUNT(*) FROM active_sessions"
                        ).fetchone()[0]
                    )
                self.assertEqual(active, 0)
            finally:
                if lobby is not None:
                    lobby.close()
                app.stop()


class ApplicationLifecycleTests(unittest.TestCase):
    def test_policy_monitor_stops_when_messenger_ipc_start_fails(self) -> None:
        app = object.__new__(ClassicOnlineApplication)
        app.account_policy_monitor = Mock()
        app.carbon_messenger_ipc_receiver = Mock()
        app.carbon_messenger_ipc_receiver.start.side_effect = RuntimeError(
            "IPC start failed"
        )

        with self.assertRaisesRegex(RuntimeError, "IPC start failed"):
            app.start()

        app.account_policy_monitor.start.assert_called_once_with()
        app.account_policy_monitor.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
