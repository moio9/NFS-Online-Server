"""Verify shared Messenger framing and legacy compatibility."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

from common.messenger import EAMessengerFrame, EAMessengerHub, EAMessengerStreamDecoder
from classic.ea.messenger import EAMessengerFrame as LegacyFrame, EAMessengerHub as LegacyHub


class SharedMessengerTests(unittest.TestCase):
    def test_legacy_and_shared_codecs_handle_the_same_fragmented_stream(self):
        self.assertIs(LegacyFrame, EAMessengerFrame)
        self.assertIs(LegacyHub, EAMessengerHub)
        frame = LegacyFrame.from_fields('PING', {'ID': '42'}, transaction=0)
        encoded = frame.encode()
        decoder = EAMessengerStreamDecoder()
        self.assertEqual(decoder.feed(encoded[:7]), [])
        self.assertEqual(decoder.feed(encoded[7:]), [frame])

    def test_shared_transport_imports_without_classic_or_carbon(self):
        server = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env['PYTHONPATH'] = os.pathsep.join((str(server), env.get('PYTHONPATH', '')))
        subprocess.run([sys.executable, '-c',
                        'import sys; import common.messenger; '
                        'assert not any(n == "classic" or n.startswith("classic.") '
                        'or n == "carbon" or n.startswith("carbon.") for n in sys.modules)'],
                       env=env, check=True)
