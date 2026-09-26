"""Administrative complaint top, detail and deletion commands."""

from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from io import StringIO
import unittest
from unittest.mock import patch

import nfs_online
from common.accounts import SQLiteAccountDatabase
from common.social import SocialService


class ReportCommandTests(unittest.TestCase):
    def test_clear_option_reaches_report_command(self) -> None:
        output = StringIO()
        with patch.object(nfs_online, "report_lines", return_value=["Deleted 2 complaints."]) as reports:
            with redirect_stdout(output):
                self.assertEqual(nfs_online.main(["reports", "clear", "Bob", "--yes"]), 0)
        reports.assert_called_once_with(["clear", "Bob", "--yes"])
        self.assertIn("Deleted 2 complaints.", output.getvalue())

    def test_top_details_and_deletion_are_persistent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
            now = [1000.0]
            social = SocialService(database=database, clock=lambda: now[0])
            first = social.record_report("Alice", "Bob", "Cheating", source="website")
            now[0] += 1
            second = social.record_report("Charlie", "bOb", "Cheating", source="most_wanted_lobby")
            now[0] += 1
            social.record_report("Alice", "Bob", "Harassment", source="website")
            social.record_report("Dave", "Eve", "Spam", source="underground2_lobby")

            with patch.object(nfs_online, "configured_account_db", return_value=database.path):
                top = "\n".join(nfs_online.report_lines([]))
                self.assertLess(top.index("Bob"), top.index("Eve"))
                self.assertIn("3 / 2", top)
                self.assertIn("Cheating (2)", top)
                self.assertIn("Harassment (1)", top)

                details = "\n".join(nfs_online.report_lines(["show", "BOB"]))
                self.assertIn(f"#{first.report_id}", details)
                self.assertIn(f"#{second.report_id}", details)
                self.assertIn("most_wanted_lobby", details)

                self.assertEqual(
                    nfs_online.report_lines(["delete", str(first.report_id)]),
                    ["Complaint deleted."],
                )
                self.assertNotIn(f"#{first.report_id}", "\n".join(nfs_online.report_lines(["show", "Bob"])))
                with self.assertRaises(nfs_online.LauncherError):
                    nfs_online.report_lines(["clear", "Bob"])
                self.assertEqual(
                    nfs_online.report_lines(["clear", "bob", "--yes"]),
                    ["Deleted 2 complaints for bob."],
                )
                self.assertEqual(nfs_online.report_lines(["show", "Bob"]), ["No complaints for Bob."])
                self.assertIn("Eve", "\n".join(nfs_online.report_lines([])))


if __name__ == "__main__":
    unittest.main()
