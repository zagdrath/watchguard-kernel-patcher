#!/usr/bin/env python3
"""
Rebuild the bzImage from previously extracted parts, preserving sizes.

This script expects `output/setup.bin` and `output/kernel.cmp.orig` from a
prior extraction. It splices a patched initramfs into vmlinux, then
recompresses vmlinux so the embedded kernel gzip stream matches the
original length exactly, and finally creates a bzImage.patched file.

"""

from __future__ import annotations

import gzip
import io
import pathlib
import sys
import zlib
from typing import Optional, Tuple

def die(msg: str) -> None:
    """Print *msg* to stderr and exit with status 1."""
    print(msg, file=sys.stderr)
    sys.exit(1)

def gz_is_magic(buf: bytes) -> bool:
    return len(buf) >= 3 and buf[:3] == b"\x1f\x8b\x08"

def gz_decompress_slice(data: bytes) -> Tuple[bytes, int]:
    """Decompress a gzip member starting at data[0:]. Return (uncompressed, bytes_consumed)."""
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data)
    consumed = len(data) - len(d.unused_data)
    if consumed == 0:
        raise zlib.error("no gzip member")
    return out, consumed

def gz_compress(data: bytes, level: int = 9, fname_len: int = 0) -> bytes:
    """Deterministic gzip (mtime=0); optional fake FNAME length to tune the header size."""
    bio = io.BytesIO()
    fname = ("X" * fname_len) if fname_len > 0 else ""
    with gzip.GzipFile(filename=fname, mode="wb", fileobj=bio, mtime=0, compresslevel=level) as gf:
        gf.write(data)
    return bio.getvalue()

def gz_empty_member(fname_len: int = 0) -> bytes:
    """A gzip member that decompresses to empty bytes; size ≈ 20 + *fname_len*."""
    return gz_compress(b"", level=1, fname_len=fname_len)

def fit_gzip_to_exact_length(uncompressed: bytes, target_len: int) -> Optional[bytes]:
    """
    Compress *uncompressed* into a byte‑string of **exactly** *target_len* bytes.
    Strategy: compress main, spend remainder in header FNAME, pad with empty members.
    Returns ``None`` if the minimal gzip already exceeds the target.
    """
    base = len(gz_empty_member(0))

    # Smallest main among a few levels.
    best = None
    for lvl in (9, 8, 7):
        cand = gz_compress(uncompressed, level=lvl, fname_len=0)
        if best is None or len(cand) < len(best):
            best = cand
    main = best
    assert main is not None
    if len(main) > target_len:
        return None

    delta = target_len - len(main)
    if delta == 0:
        return main

    # Use header FNAME to consume delta % base so the remainder is a multiple.
    head_add = delta % base
    tuned = gz_compress(uncompressed, level=9, fname_len=(head_add - 1) if head_add > 0 else 0)
    if len(tuned) > target_len:
        tuned = main

    rem = target_len - len(tuned)
    if rem == 0:
        return tuned

    pieces = [tuned]
    if rem < base:
        fname_len = rem - base - 1
        if fname_len < 0:
            for nudge in range(1, base):
                tuned2 = gz_compress(uncompressed, level=9, fname_len=nudge)
                rem2 = target_len - len(tuned2)
                if rem2 == 0:
                    return tuned2
                if rem2 >= base:
                    tuned = tuned2
                    pieces = [tuned]
                    rem = rem2
                    break
            else:
                return None
        else:
            pieces.append(gz_empty_member(max(0, fname_len)))
            return b"".join(pieces)

    n_full = rem // base
    tail = rem - n_full * base
    pieces.extend(gz_empty_member(0) for _ in range(n_full))
    if tail:
        fname_len = tail - base - 1
        if fname_len < 0:
            return None
        pieces.append(gz_empty_member(max(0, fname_len)))
    return b"".join(pieces)

CPIO_NEWC_MAGIC = b"070701"

def find_last_cpio_gz_in_vmlinux(vmlinux: bytes) -> Tuple[int, int]:
    """Return (start,end) of the **last** gzip member that decompresses to a 'newc' cpio."""
    magic = b"\x1f\x8b\x08"
    last = None
    i = 0
    while True:
        j = vmlinux.find(magic, i)
        if j == -1:
            break
        try:
            out, consumed = gz_decompress_slice(vmlinux[j:])
            if out.startswith(CPIO_NEWC_MAGIC):
                last = (j, j + consumed)
        except zlib.error:
            pass
        i = j + 1
    if not last:
        die("No gzip‑>cpio (newc) stream found in vmlinux")
    return last

def locate_kernel_gzip_stream(kernel_cmp_orig: bytes) -> Tuple[int, int]:
    """Find the first gzip member in *kernel_cmp_orig* by scanning the first 4 KiB."""
    scan = min(4096, len(kernel_cmp_orig))
    for i in range(scan):
        if gz_is_magic(kernel_cmp_orig[i:i+3]):
            try:
                _, consumed = gz_decompress_slice(kernel_cmp_orig[i:])
                return i, i + consumed
            except zlib.error:
                pass
    die("Unsupported kernel compression (non‑gzip) or gzip stream not found in kernel.cmp.orig")

def main() -> None:
    script_dir = pathlib.Path(__file__).resolve().parent
    outdir = script_dir / "output"

    setup_path = outdir / "setup.bin"
    kcmp_orig_path = outdir / "kernel.cmp.orig"

    if not setup_path.is_file() or not kcmp_orig_path.is_file():
        die("Missing required inputs in ./output (need setup.bin and kernel.cmp.orig).")

    # Step 1: build vmlinux.patched
    vmlinux_patched_path = outdir / "vmlinux.patched"
    vmlinux_path = outdir / "vmlinux"
    new_gz_init_path = outdir / "initramfs.cpio.gz.new"
    new_cpio_init_path = outdir / "initramfs.cpio.new"

    if vmlinux_patched_path.is_file():
        print("[i] Using existing output/vmlinux.patched")
        vmlinux_patched = vmlinux_patched_path.read_bytes()
    else:
        if not vmlinux_path.is_file():
            die("Need output/vmlinux OR output/vmlinux.patched.")
        if not new_gz_init_path.is_file() and not new_cpio_init_path.is_file():
            die("Provide a patched initramfs at output/initramfs.cpio.gz.new or output/initramfs.cpio.new.")

        vmlinux = vmlinux_path.read_bytes()

        # Locate original initramfs gzip
        cpio_start, cpio_end = find_last_cpio_gz_in_vmlinux(vmlinux)
        orig_cpio_gz_len = cpio_end - cpio_start

        # Load new initramfs (uncompressed).
        if new_cpio_init_path.is_file():
            new_cpio_uncompressed = new_cpio_init_path.read_bytes()
        else:
            try:
                new_cpio_uncompressed = zlib.decompress(new_gz_init_path.read_bytes(), 16 + zlib.MAX_WBITS)
            except zlib.error as e:
                die(f"initramfs.cpio.gz.new is not a valid gzip: {e}")

        # Fit new cpio.gz to the exact original length
        fitted_cpio_gz = fit_gzip_to_exact_length(new_cpio_uncompressed, orig_cpio_gz_len)
        if fitted_cpio_gz is None:
            min_len = len(gz_compress(new_cpio_uncompressed))
            die(f"New initramfs cannot fit in‑place: minimal gzip size {min_len} > original {orig_cpio_gz_len}")

        # Splice into vmlinux (length preserved)
        vmlinux_patched = vmlinux[:cpio_start] + fitted_cpio_gz + vmlinux[cpio_end:]
        assert len(vmlinux_patched) == len(vmlinux), "vmlinux size changed (should not happen)"
        vmlinux_patched_path.write_bytes(vmlinux_patched)
        print("[+] Wrote output/vmlinux.patched")

    # Step 2: rebuild kernel.cmp.patched (with original kernel stream size)
    kernel_cmp_orig = kcmp_orig_path.read_bytes()
    kstart, kend = locate_kernel_gzip_stream(kernel_cmp_orig)
    kernel_prefix = kernel_cmp_orig[:kstart]
    kernel_stream_len = kend - kstart
    kernel_suffix = kernel_cmp_orig[kend:]

    fitted_kernel_gz = fit_gzip_to_exact_length(vmlinux_patched, kernel_stream_len)
    if fitted_kernel_gz is None:
        min_len = len(gz_compress(vmlinux_patched))
        die(f"Recompressed vmlinux cannot fit original kernel stream size: minimal {min_len} > {kernel_stream_len}")

    kernel_cmp_patched = kernel_prefix + fitted_kernel_gz + kernel_suffix
    assert len(kernel_cmp_patched) == len(kernel_cmp_orig), "kernel.cmp size changed"
    (outdir / "kernel.cmp.patched").write_bytes(kernel_cmp_patched)
    print("[+] Wrote output/kernel.cmp.patched")

    # Step 3: build bzImage.patched
    setup_bin = setup_path.read_bytes()
    bz_patched = setup_bin + kernel_cmp_patched
    (outdir / "../bzImage.patched").write_bytes(bz_patched)
    print("[+] Wrote bzImage.patched (size/offsets preserved)")

if __name__ == "__main__":
    main()
