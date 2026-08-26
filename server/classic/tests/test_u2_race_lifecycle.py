from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory, SessionState
from classic.ea.ranking import ClassicRankingStore
from classic.games.underground2.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)


class Underground2RaceLifecycleTests(unittest.TestCase):
    def test_lobby_close_after_gsta_preserves_active_race_route(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account("Account", "password", persona="Host")
            identities = IdentityStore(token_factory=lambda: "token")
            auth = create_auth_service(
                credentials,
                identities,
                verify_passwords=False,
            )
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="underground2"),
                control_endpoint=Endpoint("127.0.0.1", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            account = credentials.resolve_account("Account")
            identity, token = identities.login("Account", "Host")
            context = ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id="u2-host",
                    account=account,
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona="Host",
                ),
                authenticated=True,
                persona_selected=True,
            )
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=2,
                min_players=2,
                host_persona="Host",
            )
            sessions.join_game(game.game_id, identity.user_id + 1, persona="Guest")
            sessions.set_state(game.game_id, SessionState.ACTIVE)
            game.participant_race_addresses = {
                identity.user_id: "127.0.0.1",
                identity.user_id + 1: "127.0.0.1",
            }
            context.lobby_game_id = game.game_id
            retired: list[int] = []
            service.set_race_relay(
                Endpoint("127.0.0.1", 20000),
                lambda current: dict(current.participant_race_addresses),
                unregistrar=lambda current: not retired.append(current.game_id),
            )

            service.release(context)

            preserved = sessions.get_game(game.game_id)
            self.assertIs(preserved, game)
            self.assertEqual(preserved.state, SessionState.ACTIVE)
            self.assertEqual(len(preserved.participants), 2)
            self.assertTrue(preserved.participant_race_addresses)
            self.assertEqual(retired, [])
            self.assertEqual(context.lobby_game_id, 0)


if __name__ == "__main__":
    unittest.main()
