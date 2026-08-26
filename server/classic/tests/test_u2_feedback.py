from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from classic.accounts.credentials import CredentialStore
from classic.accounts.identity import IdentityStore
from classic.core.config import Endpoint
from classic.ea.social import SocialService
from classic.games.most_wanted.auth import create_auth_service as create_mw_auth
from classic.games.underground2.auth import create_auth_service as create_u2_auth
from classic.protocols.auth import ClassicAuthContext
from classic.protocols.frame import ClassicEAFrame
from classic.protocols.prelogin import (
    ClassicPreloginContext,
    ClassicPreloginProfile,
    ClassicPreloginService,
)


class Underground2FeedbackTests(unittest.TestCase):
    @staticmethod
    def _service(root: Path, *, game_id: str = "underground2"):
        credentials = CredentialStore(root / "auth.json")
        credentials.create_account("HostAccount", "password", persona="Host")
        identities = IdentityStore(token_factory=lambda: "token")
        auth_factory = create_mw_auth if game_id == "most_wanted" else create_u2_auth
        auth = auth_factory(credentials, identities, verify_passwords=False)
        social = SocialService(clock=lambda: 1234.5)
        service = ClassicPreloginService(
            auth,
            profile=ClassicPreloginProfile(game_id=game_id),
            control_endpoint=Endpoint("127.0.0.1", 13505),
            social=social,
        )
        identity, token = identities.login("HostAccount", "Host")
        context = ClassicPreloginContext(
            auth=ClassicAuthContext(
                connection_id="host",
                account=credentials.resolve_account("HostAccount"),
                identity=identity,
                session_token=token,
                lkey=token,
                persona="Host",
            ),
            authenticated=True,
            persona_selected=True,
        )
        return service, social, context

    def test_mw_feedback_gets_completion_ack_and_is_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            service, social, context = self._service(
                Path(temporary),
                game_id="most_wanted",
            )

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "rept",
                    (
                        ("PERS", "Guest"),
                        ("LANG", "NA"),
                        ("TYPE", "Cheating"),
                    ),
                ),
                context,
            )

            self.assertEqual(reply.reason, "feedback_reported")
            ack = ClassicEAFrame.decode_one(reply.frames[0])[0]
            self.assertEqual(ack.command, "rept")
            self.assertEqual(ack.fields(), {"TEXT": "Report complete"})
            report = social.report_snapshot()[-1]
            self.assertEqual(report.reporter, "Host")
            self.assertEqual(report.target, "Guest")
            self.assertEqual(report.reason, "Cheating")
            self.assertEqual(report.source, "most_wanted_lobby")

    def test_stock_feedback_probe_gets_completion_ack(self) -> None:
        with TemporaryDirectory() as temporary:
            service, social, context = self._service(Path(temporary))

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "rept",
                    (("PERS", "Guest"), ("LANG", "NA")),
                ),
                context,
            )

            self.assertEqual(reply.reason, "feedback_probe")
            self.assertEqual(len(reply.frames), 1)
            ack = ClassicEAFrame.decode_one(reply.frames[0])[0]
            self.assertEqual(ack.command, "rept")
            self.assertEqual(ack.fields(), {"TEXT": "Report complete"})
            report = social.report_snapshot()[-1]
            self.assertEqual(report.reporter, "Host")
            self.assertEqual(report.target, "Guest")
            self.assertEqual(report.reason, "")
            self.assertEqual(report.language, "NA")
            self.assertEqual(report.source, "underground2_lobby")

    def test_typed_feedback_is_unquoted_sanitized_and_recorded(self) -> None:
        with TemporaryDirectory() as temporary:
            service, social, context = self._service(Path(temporary))

            reply = service.dispatch(
                ClassicEAFrame.from_fields(
                    "rept",
                    (
                        ("PERS", "Guest"),
                        ("LANG", "NA"),
                        ("TYPE", '"Threats / Harassment"'),
                    ),
                ),
                context,
            )

            self.assertEqual(reply.reason, "feedback_reported")
            self.assertEqual(
                ClassicEAFrame.decode_one(reply.frames[0])[0].fields()["TEXT"],
                "Report complete",
            )
            report = social.report_snapshot()[-1]
            self.assertEqual(report.reason, "Threats / Harassment")
            self.assertEqual(report.created_at, 1234.5)

    def test_social_report_audit_is_bounded(self) -> None:
        social = SocialService(clock=lambda: 1.0)
        for index in range(140):
            social.record_report("Host", f"Guest{index}", "Cheating")

        reports = social.report_snapshot()
        self.assertEqual(len(reports), 128)
        self.assertEqual(reports[0].target, "Guest12")
        self.assertEqual(reports[-1].target, "Guest139")


if __name__ == "__main__":
    unittest.main()
