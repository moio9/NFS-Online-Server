"""Wire-level regression checks for the first extracted FESL building blocks."""

import unittest

from carbon.fesl.primitives import fesl_string, sint8, sint16, sint32, sint64


class FESLPrimitivesTests(unittest.TestCase):
    def test_signed_integer_encodings_match_carbon_codec(self) -> None:
        self.assertEqual(sint8(0), b"\x80")
        self.assertEqual(sint16(0), b"\x80\x00")
        self.assertEqual(sint32(0), b"\x80\x00\x00\x00")
        self.assertEqual(sint64(0), b"\x80\x00\x00\x00\x00\x00\x00\x00")

    def test_fesl_string_is_ascii_and_bounded(self) -> None:
        self.assertEqual(fesl_string("AB"), b"\x80\x00\x00\x02AB")
        self.assertEqual(fesl_string("A" * 70)[-63:], b"A" * 63)
