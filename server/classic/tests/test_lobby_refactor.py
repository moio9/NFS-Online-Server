"""Regression coverage for the incremental Classic lobby split."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import unittest

from classic.lobby.connection_registry import ClassicConnectionRegistryMixin
from classic.lobby.endpoints import ClassicEndpointMixin
from classic.lobby.game_commands import ClassicGameCommandMixin
from classic.lobby.game_search import ClassicGameSearchMixin
from classic.lobby.handshake import ClassicHandshakeMixin
from classic.lobby.lifecycle import ClassicLifecycleMixin
from classic.lobby.messages import ClassicMessageMixin
from classic.lobby.models import (
    ClassicPreloginContext as LobbyPreloginContext,
    ClassicPreloginProfile as LobbyPreloginProfile,
    ClassicPreloginReply as LobbyPreloginReply,
    ClassicUserset as LobbyUserset,
)
from classic.lobby.mw_sessions import ClassicMWSessionMixin
from classic.lobby.presence import ClassicPresenceMixin
from classic.lobby.selection import ClassicSelectionMixin
from classic.lobby.snapshots import ClassicSnapshotMixin
from classic.lobby.u2_rooms import ClassicU2RoomMixin
from classic.lobby.ranking import ClassicRankingMixin
from classic.lobby.router import ClassicRouterMixin
from classic.lobby.usersets import ClassicUsersetMixin
from classic.lobby.wire import ClassicWireMixin
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginReply,
    ClassicPreloginService,
    ClassicUserset,
)


class ClassicLobbyRefactorTests(unittest.TestCase):
    def test_prelogin_keeps_existing_model_exports(self) -> None:
        self.assertIs(ClassicPreloginContext, LobbyPreloginContext)
        self.assertIs(ClassicPreloginProfile, LobbyPreloginProfile)
        self.assertIs(ClassicPreloginReply, LobbyPreloginReply)
        self.assertIs(ClassicUserset, LobbyUserset)

    def test_ranking_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicRankingMixin))
        self.assertEqual(ClassicPreloginService._mw_snap_stats_board(4), 2)
        self.assertEqual(ClassicPreloginService._u2_snap_stats_board(1, 0), 1)

    def test_userset_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicUsersetMixin))
        self.assertEqual(
            ClassicPreloginService._dispatch_mw_userset_create.__module__,
            "classic.lobby.usersets",
        )
        self.assertEqual(
            ClassicPreloginService._dispatch_mw_userset_leave.__module__,
            "classic.lobby.usersets",
        )

    def test_mw_session_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicMWSessionMixin))
        for method_name in (
            "_mw_game_fields",
            "_mw_start_frames",
            "_mw_postrace_room_frames",
        ):
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, "classic.lobby.mw_sessions")

    def test_game_command_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicGameCommandMixin))
        for method_name in (
            "_dispatch_game_create",
            "_dispatch_game_join",
            "_dispatch_game_leave",
            "_dispatch_u2_kick",
            "_dispatch_game_settings",
            "_dispatch_game_start",
        ):
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, "classic.lobby.game_commands")

    def test_presence_and_message_api_remain_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicPresenceMixin))
        self.assertTrue(issubclass(ClassicPreloginService, ClassicMessageMixin))
        expected_modules = {
            "_dispatch_auxiliary": "classic.lobby.presence",
            "_dispatch_online": "classic.lobby.presence",
            "_dispatch_message": "classic.lobby.messages",
        }
        for method_name, module_name in expected_modules.items():
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, module_name)

    def test_lifecycle_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicLifecycleMixin))
        for method_name in (
            "_retire_game_transport",
            "_schedule_u2_transport_retirement",
            "release",
        ):
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, "classic.lobby.lifecycle")

    def test_u2_room_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicU2RoomMixin))
        for method_name in (
            "_u2_room",
            "_u2_game_sizes",
            "_u2_room_frames",
            "_dispatch_u2_move",
        ):
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, "classic.lobby.u2_rooms")

    def test_query_projection_api_remains_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicSelectionMixin))
        self.assertTrue(issubclass(ClassicPreloginService, ClassicSnapshotMixin))
        self.assertTrue(issubclass(ClassicPreloginService, ClassicGameSearchMixin))
        expected_modules = {
            "_selection_frame": "classic.lobby.selection",
            "_dispatch_selection": "classic.lobby.selection",
            "_snap_frames": "classic.lobby.snapshots",
            "_snap_personas": "classic.lobby.snapshots",
            "_game_matches_search": "classic.lobby.game_search",
            "_dispatch_game_search": "classic.lobby.game_search",
        }
        for method_name, module_name in expected_modules.items():
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, module_name)


    def test_transport_registry_and_wire_api_remain_on_prelogin_service(self) -> None:
        self.assertTrue(
            issubclass(ClassicPreloginService, ClassicEndpointMixin)
        )
        self.assertTrue(
            issubclass(ClassicPreloginService, ClassicConnectionRegistryMixin)
        )
        self.assertTrue(issubclass(ClassicPreloginService, ClassicWireMixin))
        expected_modules = {
            "set_endpoint_resolver": "classic.lobby.endpoints",
            "set_race_relay": "classic.lobby.endpoints",
            "_context_for_user": "classic.lobby.connection_registry",
            "_send_users": "classic.lobby.connection_registry",
            "_news_frame": "classic.lobby.wire",
            "_game_fields": "classic.lobby.wire",
            "_who_frame": "classic.lobby.wire",
        }
        for method_name, module_name in expected_modules.items():
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, module_name)

    def test_handshake_and_router_api_remain_on_prelogin_service(self) -> None:
        self.assertTrue(issubclass(ClassicPreloginService, ClassicHandshakeMixin))
        self.assertTrue(issubclass(ClassicPreloginService, ClassicRouterMixin))
        expected_modules = {
            "_dispatch_address": "classic.lobby.handshake",
            "_dispatch_authentication": "classic.lobby.handshake",
            "_dispatch_user": "classic.lobby.handshake",
            "dispatch": "classic.lobby.router",
        }
        for method_name, module_name in expected_modules.items():
            with self.subTest(method=method_name):
                method = getattr(ClassicPreloginService, method_name)
                self.assertEqual(method.__module__, module_name)

    def test_lobby_modules_import_before_protocol_reexports(self) -> None:
        server_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONPATH"] = str(server_root)
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import classic.lobby.connection_registry; "
                    "import classic.lobby.endpoints; "
                    "import classic.lobby.game_commands; "
                    "import classic.lobby.game_search; "
                    "import classic.lobby.handshake; "
                    "import classic.lobby.lifecycle; "
                    "import classic.lobby.messages; "
                    "import classic.lobby.models; "
                    "import classic.lobby.mw_sessions; "
                    "import classic.lobby.presence; "
                    "import classic.lobby.selection; "
                    "import classic.lobby.snapshots; "
                    "import classic.lobby.u2_rooms; "
                    "import classic.lobby.ranking; "
                    "import classic.lobby.router; "
                    "import classic.lobby.usersets; "
                    "import classic.lobby.wire; "
                    "import classic.protocols.prelogin"
                ),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
