# WatchGuard Kernel Image Patcher

This toolkit is designed to **extract, patch, and rebuild WatchGuard
`bzImage` kernel images** while preserving all original sizes and offsets.
It allows safe in-place modification of the embedded initramfs — most
importantly the `/bin/codeverf` binary — so the image remains bootable.

---

## Components

- **extract_bzimage.py**
  Extracts parts of a WatchGuard `bzImage`:
  - `output/setup.bin`
  - `output/kernel.cmp.orig`
  - `output/vmlinux`
  - `output/initramfs.cpio.gz.orig`
  - `output/initramfs.cpio.orig`
  - `output/codeverf` (if present)

- **patch_codeverf.py**
  Locates known `codeverf` instruction sequences and replaces them with a stub
  that always returns success. Produces `output/codeverf.patched`.

- **repack_initramfs.py**
  Replaces `bin/codeverf` inside the extracted initramfs with the patched
  version and rebuilds it. The resulting gzip stream is padded back to the
  exact size of the original, ensuring offsets are unchanged.

- **repack_bzimage.py**
  Reassembles the `bzImage` from the patched parts. Ensures the kernel and
  initramfs streams remain the same length as the originals. Produces
  `bzImage.patched`.

- **run.sh**
  Convenience wrapper that performs the full workflow in order:
  1. Extract `bzImage`
  2. Patch `codeverf`
  3. Repack initramfs
  4. Rebuild `bzImage`

---

## Usage

1. Place your WatchGuard `bzImage` in this directory.
2. Run the automated workflow:

   ```bash
   ./run.sh

