from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.directory import SessionDirectory
from classic.ea.ranking import ClassicRankingStore
from classic.games.most_wanted.auth import create_auth_service
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)


class MostWantedProjectedAddressTests(unittest.TestCase):
    def test_presence_and_callback_user_match_virtual_game_slot(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credentials = CredentialStore(root / "auth.json")
            credentials.create_account("HostAccount", "password", persona="Host")
            identities = IdentityStore(token_factory=lambda: "host-token")
            auth = create_auth_service(credentials, identities, verify_passwords=False)
            sessions = SessionDirectory()
            service = ClassicPreloginService(
                auth,
                profile=ClassicPreloginProfile(game_id="most_wanted"),
                control_endpoint=Endpoint("198.51.100.10", 13505),
                sessions=sessions,
                ranking=ClassicRankingStore(root / "stats.json"),
            )
            account = credentials.resolve_account("HostAccount")
            identity, token = identities.login("HostAccount", "Host")
            context = ClassicPreloginContext(
                auth=ClassicAuthContext(
                    connection_id="host",
                    client_ip="192.168.1.150",
                    account=account,
                    identity=identity,
                    session_token=token,
                    lkey=token,
                    persona="Host",
                ),
                authenticated=True,
                persona_selected=True,
                client_address="192.168.1.150",
                client_port=42000,
            )
            game = sessions.create_game(
                0,
                identity.user_id,
                capacity=4,
                min_players=2,
                name="001.Host",
                host_persona="Host",
                host_address="192.168.1.150",
            )
            game.participant_race_addresses[identity.user_id] = "100.64.0.10"
            game.participant_aux[identity.user_id] = "SCF%3d0%0aCE%3d3,3,3%0aV%3d20%0a"

            pregame = dict(service._mw_presence_fields(context))
            self.assertEqual(pregame["A"], "192.168.1.150")
            self.assertEqual(pregame["LA"], "192.168.1.150")

            in_game = dict(
                service._mw_presence_fields(
                    context,
                    game=game,
                    display_game_id=game.game_id,
                )
            )
            self.assertEqual(in_game["A"], "100.64.0.10")
            self.assertEqual(in_game["LA"], "100.64.0.10")

            frame, remainder = ClassicEAFrame.decode_one(
                service._mw_usr_frame(context, game)
            )
            self.assertFalse(remainder)
            fields = frame.fields()
            self.assertEqual(fields["ADDR"], "100.64.0.10")
            self.assertEqual(fields["LADDR"], "100.64.0.10")
            self.assertIn("CE%3d3,3,3", fields["AUX"])


if __name__ == "__main__":
    unittest.main()
