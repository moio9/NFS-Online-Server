import struct
import unittest
from pathlib import Path

from carbon.mad.xbox_texture import dds_to_xbox, tiled_offset


ASSETS = Path(__file__).parents[1] / "mad/assets"


class XboxTextureTests(unittest.TestCase):
    def test_known_xenos_addresses(self):
        self.assertEqual(tiled_offset(0, 0, 64), 0)
        self.assertEqual(tiled_offset(1, 0, 64), 8)
        self.assertEqual(tiled_offset(0, 1, 64), 16)
        self.assertEqual(tiled_offset(8, 0, 64), 64)
        self.assertEqual(tiled_offset(32, 0, 64), 8192)

    def test_all_blocks_and_mips_survive_conversion(self):
        for file in ASSETS.glob("*.dds"):
            with self.subTest(file=file.name):
                source = file.read_bytes()
                native = dds_to_xbox(source)
                h, w = struct.unpack_from("<II", source, 12)
                expected_size = 0x10000 if w == 512 else 0xa000
                self.assertEqual(native[0xf4:0xf8], bytes.fromhex("cf931a00"))
                self.assertEqual(struct.unpack_from("<I", native, 0x100)[0], expected_size)
                offsets, total = [], 0
                for level in range(4):
                    offsets.append(total)
                    blocks_w = ((w >> level) + 3) // 4
                    blocks_h = ((h >> level) + 3) // 4
                    total += ((blocks_w + 31) & ~31) * ((blocks_h + 31) & ~31) * 8
                self.assertEqual(total, expected_size)
                cursor = 128
                for level in range(5):
                    bw, bh = ((w >> level) + 3) // 4, ((h >> level) + 3) // 4
                    stored = min(level, 3)
                    pitch = (((w >> stored) + 3) // 4 + 31) & ~31
                    packed = (16 >> (level - 3)) // 4 if level >= 3 else 0
                    x0, y0 = (0, packed) if w > h else (packed, 0)
                    for y in range(bh):
                        for x in range(bw):
                            at = 0x1000 + offsets[stored] + tiled_offset(x + x0, y + y0, pitch)
                            actual = native[at:at + 8]
                            restored = bytes(actual[i ^ 1] for i in range(8))
                            self.assertEqual(restored, source[cursor:cursor + 8])
                            cursor += 8
                self.assertEqual(cursor, len(source))

    def test_truncated_and_unexpected_input_rejected(self):
        source = next(ASSETS.glob("*.dds")).read_bytes()
        for invalid in (b"DDS ", source[:-1], source + b"x"):
            with self.assertRaises(ValueError):
                dds_to_xbox(invalid)


if __name__ == "__main__":
    unittest.main()
