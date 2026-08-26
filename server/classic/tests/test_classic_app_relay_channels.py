from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from classic.app import ClassicOnlineApplication
from classic.core.catalog import GameId
from classic.core.config import ClassicGameSettings, Endpoint, ServerSettings
from classic.core.tcp import TCPListener
from classic.core.udp import UDPListener


class ClassicApplicationRelayChannelTests(unittest.TestCase):
    @staticmethod
    def _game_settings(game: GameId) -> ClassicGameSettings:
        endpoint = Endpoint("127.0.0.1", 0)
        return ClassicGameSettings(
            game,
            endpoint,
            endpoint,
            endpoint,
            endpoint,
        )

    def test_mw_only_runtime_publishes_four_relay_channels(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            endpoint = Endpoint("127.0.0.1", 0)
            settings = ServerSettings(
                self._game_settings(GameId.UNDERGROUND2),
                self._game_settings(GameId.MOST_WANTED),
                endpoint,
                endpoint,
                endpoint,
                endpoint,
                enable_u2=False,
                enable_mw=True,
                auth_data_path=str(root / "auth.json"),
                social_data_path=str(root / "social.json"),
                stats_data_path=str(root / "stats.json"),
                race_listen=endpoint,
                race_public=Endpoint("127.0.0.1", 20000),
            )
            application = ClassicOnlineApplication(settings)

            with (
                mock.patch.object(
                    UDPListener,
                    "start",
                    autospec=True,
                    side_effect=lambda listener: listener.endpoint,
                ),
                mock.patch.object(UDPListener, "stop", autospec=True),
                mock.patch.object(
                    TCPListener,
                    "start",
                    autospec=True,
                    side_effect=lambda listener: listener.endpoint,
                ),
                mock.patch.object(TCPListener, "stop", autospec=True),
            ):
                application.start()
                try:
                    self.assertEqual(application.race_channel_count, 4)
                    self.assertEqual(
                        tuple(
                            endpoint.port
                            for endpoint in application.mw.prelogin.race_endpoints
                        ),
                        (20000, 20001, 20002, 20003),
                    )
                finally:
                    application.stop()


if __name__ == "__main__":
    unittest.main()
