"""Keep local Carbon progression data out of packaged source trees."""
import unittest

from tools.check_public_tree import runtime_reason


class PublicTreeTests(unittest.TestCase):
    def test_carbon_sqlite_database_and_sidecars_are_rejected(self):
        for suffix in ('', '-wal', '-shm'):
            with self.subTest(suffix=suffix):
                self.assertIsNotNone(runtime_reason('data/carbon/progression.sqlite3' + suffix))
        self.assertIsNone(runtime_reason('data/carbon/dlc_catalog.json'))
