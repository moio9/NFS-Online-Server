#!/usr/bin/env python3
"""Reject PE32 ASI binaries with direct relative CALLs into the IAT.

This catches a Windows x86 codegen/linkage failure where a Zig `extern fn`
without a DLL namespace is linked to the IAT slot itself. Executing that slot
causes an execute-access page fault inside the ASI's .rdata section.
"""
from __future__ import annotations
import pathlib, struct, sys


def u16(data: bytes, off: int) -> int:
    return struct.unpack_from('<H', data, off)[0]


def u32(data: bytes, off: int) -> int:
    return struct.unpack_from('<I', data, off)[0]


def rva_to_file(rva: int, sections: list[tuple[int,int,int,int]]) -> int | None:
    for va, vsize, raw, rawsize in sections:
        span=max(vsize, rawsize)
        if va <= rva < va + span:
            delta=rva-va
            if delta >= rawsize:
                return None
            return raw+delta
    return None


def audit(path: pathlib.Path) -> list[str]:
    data=path.read_bytes()
    errors=[]
    if data[:2] != b'MZ': return ['not an MZ image']
    pe=u32(data,0x3c)
    if data[pe:pe+4] != b'PE\0\0': return ['not a PE image']
    machine=u16(data,pe+4)
    nsec=u16(data,pe+6)
    opt_size=u16(data,pe+20)
    opt=pe+24
    if u16(data,opt) != 0x10b: return ['not PE32']
    if machine != 0x14c: return [f'not i386 (machine=0x{machine:x})']
    # PE32 data directory begins at optional-header + 96; IAT is directory 12.
    iat_rva=u32(data,opt+96+12*8)
    iat_size=u32(data,opt+96+12*8+4)
    sec_off=opt+opt_size
    sections=[]
    text=None
    for i in range(nsec):
        sh=sec_off+i*40
        name=data[sh:sh+8].split(b'\0',1)[0].decode('ascii','replace')
        vsize=u32(data,sh+8); va=u32(data,sh+12)
        rawsize=u32(data,sh+16); raw=u32(data,sh+20)
        sections.append((va,vsize,raw,rawsize))
        if name == '.text': text=(va,vsize,raw,rawsize)
    if not iat_rva or not iat_size: return ['missing IAT directory']
    if text is None: return ['missing .text']
    va,vsize,raw,rawsize=text
    code=data[raw:raw+rawsize]
    for i in range(0,max(0,len(code)-5)):
        if code[i] != 0xE8: continue
        rel=struct.unpack_from('<i',code,i+1)[0]
        src_rva=va+i
        target=(src_rva+5+rel)&0xffffffff
        if iat_rva <= target < iat_rva+iat_size:
            errors.append(f'direct CALL at RVA 0x{src_rva:x} targets IAT RVA 0x{target:x}')
    return errors


def main() -> int:
    failed=False
    for arg in sys.argv[1:]:
        path=pathlib.Path(arg)
        errors=audit(path)
        if errors:
            failed=True
            print(f'{path}: PE audit FAILED', file=sys.stderr)
            for e in errors: print(f'  - {e}', file=sys.stderr)
        else:
            print(f'{path}: PE audit OK')
    return 1 if failed else 0

if __name__ == '__main__':
    raise SystemExit(main())
