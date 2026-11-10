"""Experimental conversion of Carbon's billboard DDS textures for Xbox 360.

The DDS input is linear DXT1. Xbox stores 4x4 blocks in Xenos tiled order,
with each 16-bit word byte-swapped and the smallest mips sharing one tile.
The wrapper fields here are the two fields read by Carbon's PAL callback.
"""
import struct


def tiled_offset(x: int, y: int, pitch: int) -> int:
    pitch = (pitch + 31) & ~31
    macro = ((x >> 5) + (y >> 5) * (pitch >> 5)) << 10
    micro = ((x & 7) + ((y & 14) << 2)) << 3
    offset = macro + ((micro & ~15) << 1) + (micro & 15) + ((y & 1) << 4)
    return ((offset & ~511) << 3) + ((y & 16) << 7) + ((offset & 448) << 2) + (((((y & 8) >> 2) + (x >> 3)) & 3) << 6) + (offset & 63)


def dds_to_xbox(data: bytes) -> bytes:
    if len(data) < 128 or data[:4] != b"DDS " or data[84:88] != b"DXT1":
        raise ValueError("expected DXT1 DDS")
    height, width = struct.unpack_from("<II", data, 12)
    levels = struct.unpack_from("<I", data, 28)[0]
    if (width, height) not in ((256, 128), (128, 256), (512, 128)) or levels != 5:
        raise ValueError("unsupported billboard dimensions or mip count")

    offsets = []
    total = 0
    for level in range(4):
        pitch = ((max(1, width >> level) + 3) // 4 + 31) & ~31
        rows = ((max(1, height >> level) + 3) // 4 + 31) & ~31
        offsets.append(total)
        total += pitch * rows * 8
    if total != (0x10000 if width == 512 else 0xa000):
        raise ValueError("unexpected Xbox texture allocation")
    native = bytearray(total)
    used = set()
    source = 128
    for level in range(5):
        w = max(1, width >> level)
        h = max(1, height >> level)
        bx, by = (w + 3) // 4, (h + 3) // 4
        stored_level = min(level, 3)
        pitch = ((max(1, width >> stored_level) + 3) // 4 + 31) & ~31
        x_offset = y_offset = 0
        if level >= 3:
            packed = 16 >> (level - 3)
            if width > height:
                y_offset = packed // 4
            else:
                x_offset = packed // 4
        for y in range(by):
            for x in range(bx):
                if source + 8 > len(data):
                    raise ValueError("truncated DDS mip")
                dst = offsets[stored_level] + tiled_offset(x + x_offset, y + y_offset, pitch)
                if dst + 8 > total or dst in used:
                    raise ValueError("invalid Xbox texture layout")
                used.add(dst)
                block = data[source:source + 8]
                native[dst:dst + 8] = bytes((block[1], block[0], block[3], block[2], block[5], block[4], block[7], block[6]))
                source += 8
    if source != len(data):
        raise ValueError("unexpected DDS trailing bytes")
    header = bytearray(0x1000)
    header[0xf4:0xf8] = bytes.fromhex("cf931a00")
    struct.pack_into("<I", header, 0x100, total)
    return bytes(header) + native
