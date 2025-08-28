#!/usr/bin/env bash
#
# This script performs these four steps:
#   1) Extracts bzImage and initramfs
#   2) Patch the codeverf binary
#   3) Repacks initramfs to original size
#   4) Rebuilds the bzImage with the patches
#

python extract_bzimage.py
python patch_codeverf.py
python repack_initramfs.py # --prefer-zopfli (try this if getting a size mismatch)
python repack_bzimage.py
