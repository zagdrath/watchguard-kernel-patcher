#!/usr/bin/env python3
"""
Replace file in initramfs and match the original gzip size.

Loads output/initramfs.cpio.gz.orig, replaces bin/codeverf with
./output/codeverf.patched (size must match), rebuilds the cpio, then
compresses deterministically. If the result is smaller than the original,
it pads via the gzip FCOMMENT area so that the size matches exactly.
"""
from __future__ import annotations

import argparse
import gzip
import io
import shutil
import subprocess
from pathlib import Path

INPUT  = Path("./output/initramfs.cpio.gz.orig")
OUTPUT = Path("./output/initramfs.cpio.gz.new")
CODEVERF_ORIG    = "bin/codeverf"
CODEVERF_PATCHED = Path("./output/codeverf.patched")

def read_all(p: Path) -> bytes:
    return p.read_bytes()

def gunzip_bytes(blob: bytes) -> bytes:
    return gzip.GzipFile(fileobj=io.BytesIO(blob), mode='rb').read()

def pad4_len(pos: int) -> int:
    return (4 - (pos % 4)) % 4

def parse_newc(cpio_bytes: bytes):
    entries = []
    pos = 0
    while pos + 110 <= len(cpio_bytes):
        magic = cpio_bytes[pos:pos+6].decode('ascii', 'ignore')
        if magic not in ('070701', '070702'):
            break
        pos += 6
        def hx():
            nonlocal pos
            v = int(cpio_bytes[pos:pos+8].decode('ascii'), 16)
            pos += 8
            return v
        hdr = {}
        hdr['magic'] = magic
        hdr['ino'] = hx(); hdr['mode'] = hx(); hdr['uid'] = hx(); hdr['gid'] = hx()
        hdr['nlink'] = hx(); hdr['mtime'] = hx(); hdr['filesize'] = hx()
        hdr['devmajor'] = hx(); hdr['devminor'] = hx(); hdr['rdevmajor'] = hx(); hdr['rdevminor'] = hx()
        hdr['namesize'] = hx(); hdr['check'] = hx()
        name_bytes = cpio_bytes[pos:pos+hdr['namesize']]
        name = name_bytes[:-1].decode('utf-8', errors='replace')
        pos += hdr['namesize']; pos += pad4_len(pos)
        data = cpio_bytes[pos:pos+hdr['filesize']]
        pos += hdr['filesize']; pos += pad4_len(pos)
        entries.append({'name': name, 'hdr': hdr, 'data': data})
        if name == 'TRAILER!!!':
            break
    return entries

def write_newc(entries) -> bytes:
    out = io.BytesIO()
    for e in entries:
        hdr = e['hdr']; name = e['name']; data = e['data']
        assert hdr['namesize'] == len(name.encode('utf-8')) + 1, f"namesize mismatch for {name}"
        assert hdr['filesize'] == len(data), f"filesize mismatch for {name}"
        out.write(hdr['magic'].encode('ascii'))
        def hx(v): out.write(f"{v:08x}".encode('ascii'))
        hx(hdr['ino']); hx(hdr['mode']); hx(hdr['uid']); hx(hdr['gid'])
        hx(hdr['nlink']); hx(hdr['mtime']); hx(hdr['filesize'])
        hx(hdr['devmajor']); hx(hdr['devminor']); hx(hdr['rdevmajor']); hx(hdr['rdevminor'])
        hx(hdr['namesize']); hx(hdr['check'])
        out.write(name.encode('utf-8') + b'\x00')
        out.write(b'\x00' * pad4_len(out.tell()))
        out.write(data)
        out.write(b'\x00' * pad4_len(out.tell()))
    return out.getvalue()

def gzip_deterministic(payload: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode='wb', mtime=0) as gz:
        gz.write(payload)
    return buf.getvalue()

def gzip_with_zopfli(payload: bytes) -> bytes:
    # Requires `zopfli` CLI in PATH.
    p = subprocess.run(["zopfli", "--gzip", "--i50", "-c"], input=payload, stdout=subprocess.PIPE, check=True)
    return p.stdout

def add_gzip_comment_padding(gz_bytes: bytes, target_size: int) -> bytes:
    cur = len(gz_bytes)
    if cur == target_size:
        return gz_bytes
    if cur > target_size:
        raise RuntimeError(f"compressed stream ({cur}) is larger than target ({target_size}); cannot shrink safely")

    pad_needed = target_size - cur
    # Insert an FCOMMENT (0x10) uncompressed, NUL‑terminated field of length pad_needed‑1.
    # Layout: ID1 ID2 CM FLG MTIME(4) XFL OS [opt fields] ... COMPRESSED ... TRAILER(8)
    b = bytearray(gz_bytes)
    if len(b) < 10 or b[0] != 0x1f or b[1] != 0x8b:
        raise RuntimeError("not a gzip stream")
    # Set FLG bit 0x10 (FCOMMENT).
    b[3] |= 0x10
    # Insert comment right after the fixed 10‑byte header.
    insert_at = 10
    comment = b"A" * (pad_needed - 1) + b"\x00"
    b[insert_at:insert_at] = comment
    return bytes(b)

def main() -> None:
    ap = argparse.ArgumentParser(description="Replace one entry in initramfs and match original gzip size via header padding")
    ap.add_argument("--prefer-zopfli", action="store_true", help="favour zopfli to keep size ≤ original")
    args = ap.parse_args()

    orig_gz = read_all(INPUT)
    target_size = len(orig_gz)
    orig_cpio = gunzip_bytes(orig_gz)
    entries = parse_newc(orig_cpio)

    # Find and replace
    replaced = False
    repl_data = read_all(CODEVERF_PATCHED)
    for e in entries:
        if e['name'] == CODEVERF_ORIG:
            if len(repl_data) != e['hdr']['filesize']:
                raise RuntimeError(f"replacement size {len(repl_data)} != original entry size {e['hdr']['filesize']}")
            e['data'] = repl_data
            replaced = True
            break
    if not replaced:
        raise RuntimeError(f"path not found in archive: {CODEVERF_ORIG}")

    # Rebuild cpio
    new_cpio = write_newc(entries)

    # Compress (zopfli preferred to keep output <= original)
    if args.prefer_zopfli and shutil.which("zopfli"):
        gz = gzip_with_zopfli(new_cpio)
    else:
        gz = gzip_deterministic(new_cpio)

    # Pad gzip size up to match original if smaller
    if len(gz) <= target_size:
        gz = add_gzip_comment_padding(gz, target_size)
    else:
        # If larger than original and zopfli not used, try zopfli once
        if not (args.prefer_zopfli and shutil.which("zopfli")) and shutil.which("zopfli"):
            gz2 = gzip_with_zopfli(new_cpio)
            if len(gz2) <= target_size:
                gz = add_gzip_comment_padding(gz2, target_size)
            else:
                raise RuntimeError(f"even zopfli output ({len(gz2)}) > original size ({target_size}); cannot match size")
        else:
            raise RuntimeError(f"compressed size ({len(gz)}) > original size ({target_size}); rerun with --prefer-zopfli")

    OUTPUT.write_bytes(gz)
    print(f"[+] Wrote {OUTPUT} (target_size={target_size}, out_size={len(gz)}, match={len(gz)==target_size})")

if __name__ == "__main__":
    main()
