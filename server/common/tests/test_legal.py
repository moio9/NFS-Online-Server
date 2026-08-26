from __future__ import annotations

import unittest

from common.legal import TERMS_OF_SERVICE_TEXT, TERMS_OF_SERVICE_VERSION


class LegalContentTests(unittest.TestCase):
    def test_terms_of_service_is_canonical_and_protocol_neutral(self) -> None:
        self.assertEqual(TERMS_OF_SERVICE_VERSION, "20426_17.20426_17")
        self.assertEqual(
            TERMS_OF_SERVICE_TEXT,
            "NFS Online community server terms of use:\n\n"
            "By using this server, you agree to follow its rules. "
            "This unofficial service is not affiliated with or endorsed by Electronic Arts.",
        )
        self.assertNotIn("\r", TERMS_OF_SERVICE_TEXT)


if __name__ == "__main__":
    unittest.main()
