#!/usr/bin/env python3
"""
Patch known instruction sequences in `codeverf` to return success.

Searches for several byte patterns and replaces each with a small
stub that returns `0`. Offsets patched are reported.
"""
from __future__ import annotations

from pathlib import Path

INPUT  = Path("./output/codeverf")
OUTPUT = Path("./output/codeverf.patched")

# Original values (as discovered).
ORIGINALS = [
    bytes.fromhex("ff 25 82 2f 00 00 68 0a 00 00 00 e9 40 ff ff ff"),
    bytes.fromhex("ff 25 72 2f 00 00 68 0c 00 00 00 e9 20 ff ff ff"),
    bytes.fromhex("ff 25 02 2f 00 00 68 1a 00 00 00 e9 40 fe ff ff"),
]

# Replacement: mov eax,1 ; ret ; (padding NOPs to match length).
PATCHED = bytes.fromhex("B8 01 00 00 00 C3 90 90 90 90 90 90 90 90 90 90")

def find_and_patch(buf: bytearray, orig: bytes, patched: bytes) -> int:
    idx = buf.find(orig)
    if idx == -1:
        raise ValueError(f"pattern not found: {orig.hex()}")
    buf[idx:idx+len(orig)] = patched
    return idx

def main() -> None:
    data = bytearray(INPUT.read_bytes())

    for i, orig in enumerate(ORIGINALS, 1):
        off = find_and_patch(data, orig, PATCHED)
        print(f"[{i}] patched at offset {off:#x}")

    OUTPUT.write_bytes(data)
    print(f"[+] Wrote {OUTPUT}")

if __name__ == "__main__":
    main()
