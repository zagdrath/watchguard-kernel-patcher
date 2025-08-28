#!/usr/bin/env python3
"""
Extract components from the bzImage.

Creates: output/setup.bin, output/kernel.cmp.orig, output/vmlinux,
output/initramfs.cpio.gz.orig, output/initramfs.cpio.orig, and (if
present) /bin/codeverf from the initramfs as output/codeverf.

"""

from __future__ import annotations

import gzip
import pathlib
import shutil
import stat
import sys
import zlib
from typing import Tuple, Optional

def die(msg: str) -> None:
    """Print msg to stderr and exit with status 1."""
    print(msg, file=sys.stderr)
    sys.exit(1)

def gzip_is_gzip_magic(buf: bytes) -> bool:
    return len(buf) >= 3 and buf[:3] == b"\x1f\x8b\x08"

def gzip_decompress_slice(data: bytes) -> Tuple[bytes, int]:
    """Return (decompressed_bytes, bytes_consumed) for a member starting at data[0:]."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data)
    consumed = len(data) - len(d.unused_data)
    return out, consumed

CPIO_NEWC_MAGIC = b"070701"
CPIO_TRAILER = b"TRAILER!!!"

def _read_exact(b: bytes, off: int, n: int) -> bytes:
    if off + n > len(b):
        raise EOFError("cpio: truncated archive")
    return b[off:off + n]

def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4

def _canonical_cpio_name(n: str) -> str:
    """Normalise path variants such as ./bin/codeverf and /bin/codeverf."""
    if n.startswith("./"): n = n[2:]
    if n.startswith("/"): n = n[1:]
    return n

def cpio_newc_extract_single(archive_gz: bytes, want_path: str) -> Optional[bytes]:
    """
    Decompress a gzip 'newc' cpio and return the bytes of want_path.
    Follows a single symlink hop. Returns None if not found.
    """
    raw, _ = gzip_decompress_slice(archive_gz)
    want_norm = _canonical_cpio_name(want_path)

    found_data = None
    found_link_target = None
    off = 0
    entries = []

    while True:
        if off + 6 > len(raw):
            raise ValueError("cpio: missing magic")
        if raw[off:off+6] != CPIO_NEWC_MAGIC:
            raise ValueError("cpio: wrong magic")
        off += 6

        fields = [int(_read_exact(raw, off + i*8, 8), 16) for i in range(13)]
        off += 13 * 8
        (c_ino, c_mode, c_uid, c_gid, c_nlink, c_mtime, c_filesize,
        c_devmaj, c_devmin, c_rdevmaj, c_rdevmin, c_namesize, c_check) = fields

        name_b = _read_exact(raw, off, c_namesize)
        off += c_namesize
        off += _pad4(off)
        name_b = name_b[:-1]  # strip trailing NULL

        if name_b == CPIO_TRAILER:
            break

        data = _read_exact(raw, off, c_filesize)
        off += c_filesize
        off += _pad4(off)

        name = name_b.decode("utf-8", "surrogateescape")
        norm = _canonical_cpio_name(name)
        mode = c_mode & 0o777777
        ftype = stat.S_IFMT(mode)

        entries.append((norm, ftype, data))

        if norm == want_norm:
            if ftype == stat.S_IFREG:
                found_data = data
                break
            elif ftype == stat.S_IFLNK:
                # Strip any trailing NULLs in the target.
                found_link_target = data.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
                break
            else:
                break

    if found_data is not None:
        return found_data

    if found_link_target is not None:
        target_norm = _canonical_cpio_name(found_link_target)
        # First try the earlier entries.
        for norm, ftype, data in entries:
            if norm == target_norm and ftype == stat.S_IFREG:
                return data
        # Else one more pass through the archive.
        off2 = 0
        while True:
            if off2 + 6 > len(raw): break
            if raw[off2:off2+6] != CPIO_NEWC_MAGIC: break
            off2 += 6
            fields = [int(_read_exact(raw, off2 + i*8, 8), 16) for i in range(13)]
            off2 += 13 * 8
            c_namesize = fields[11]
            name_b = _read_exact(raw, off2, c_namesize)
            off2 += c_namesize
            off2 += _pad4(off2)
            name_b = name_b[:-1]
            if name_b == CPIO_TRAILER: break
            c_filesize = fields[6]
            data = _read_exact(raw, off2, c_filesize)
            off2 += c_filesize
            off2 += _pad4(off2)

            name = name_b.decode("utf-8", "surrogateescape")
            norm = _canonical_cpio_name(name)
            mode = fields[1] & 0o777777
            ftype = stat.S_IFMT(mode)
            if norm == target_norm and ftype == stat.S_IFREG:
                return data

    return None

def main() -> None:
    script_dir = pathlib.Path(__file__).resolve().parent
    bzimage = script_dir / "bzImage"
    if not bzimage.is_file():
        die("Error: bzImage not found in script folder")

    outdir = script_dir / "output"
    if outdir.exists():
        shutil.rmtree(outdir)
    outdir.mkdir()

    bz = bzimage.read_bytes()
    setup_sects = bz[0x1F1] or 4
    setup_bytes = (setup_sects + 1) * 512

    # Write splits
    (outdir / "setup.bin").write_bytes(bz[:setup_bytes])
    (outdir / "kernel.cmp.orig").write_bytes(bz[setup_bytes:])
    k = (outdir / "kernel.cmp.orig").read_bytes()

    # Locate gzip kernel stream in kernel.cmp.orig (scan first 4 KiB for start)
    kstart = kend = None
    for i in range(min(4096, len(k))):
        if gzip_is_gzip_magic(k[i:i+3]):
            try:
                _, consumed = gzip_decompress_slice(k[i:])
                kstart = i
                kend = i + consumed
                break
            except zlib.error:
                pass
    if kstart is None:
        die("Unsupported kernel compression (non‑gzip) or gzip stream not found")

    # Decompress kernel to vmlinux
    vmlinux = gzip.decompress(k[kstart:kend])
    (outdir / "vmlinux").write_bytes(vmlinux)

    # Find the LAST valid gzip member in vmlinux that looks like a cpio (newc)
    magic = b"\x1f\x8b\x08"
    last_cpio_start = None
    last_cpio_end = None

    i = 0
    while True:
        j = vmlinux.find(magic, i)
        if j == -1:
            break
        try:
            out, consumed = gzip_decompress_slice(vmlinux[j:])
            # Validate it's a cpio archive (newc starts with "070701")
            if out.startswith(CPIO_NEWC_MAGIC):
                last_cpio_start = j
                last_cpio_end = j + consumed
        except zlib.error:
            pass
        i = j + 1

    if last_cpio_start is None:
        die("No gzip‑>cpio (newc) stream found in vmlinux")

    gz_cpio = vmlinux[last_cpio_start:last_cpio_end]
    (outdir / "initramfs.cpio.gz.orig").write_bytes(gz_cpio)
    initramfs_cpio = zlib.decompress(gz_cpio, 16 + zlib.MAX_WBITS)
    (outdir / "initramfs.cpio.orig").write_bytes(initramfs_cpio)

    # Extract ONLY /bin/codeverf (follow one symlink hop)
    data = (cpio_newc_extract_single(gz_cpio, "/bin/codeverf")
            or cpio_newc_extract_single(gz_cpio, "bin/codeverf")
            or cpio_newc_extract_single(gz_cpio, "./bin/codeverf"))

    if data is None:
        print("[i] Warning: /bin/codeverf not found in initramfs", file=sys.stderr)
    else:
        (outdir / "codeverf").write_bytes(data)

    print("[+] Extraction complete into ./output")

if __name__ == "__main__":
    main()
