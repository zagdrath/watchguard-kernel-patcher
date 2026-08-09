import subprocess, tempfile, os, gzip, io

def find_cpio_offset(kernel_bytes):
    """Return the file offset of the gzip-compressed cpio inside a bzImage."""
    # Look for gzip magic near the end (the initramfs is appended)
    idx = kernel_bytes.rfind(b'\x1f\x8b\x08')
    if idx == -1:
        # Try xz magic
        idx = kernel_bytes.rfind(b'\xFD\x37\x7A\x58\x5A\x00')
        if idx == -1:
            raise ValueError("Could not find compressed cpio in kernel")
    return idx

def patch_cpio(compressed_bytes):
    """Decompress cpio, patch codeverf, return new gzip-compressed cpio."""
    tmpdir = tempfile.mkdtemp()
    try:
        # Write compressed data to temp file
        gz_path = os.path.join(tmpdir, 'initramfs.gz')
        with open(gz_path, 'wb') as f:
            f.write(compressed_bytes)
        
        # Decompress
        subprocess.run(['gunzip', gz_path], check=True)
        cpio_path = gz_path[:-3]  # remove .gz
        
        # Extract cpio
        extract_dir = os.path.join(tmpdir, 'root')
        os.mkdir(extract_dir)
        subprocess.run(['cpio', '-idmv'], stdin=open(cpio_path, 'rb'), cwd=extract_dir, check=True)
        
        # Patch codeverf (adjust relative path if needed; check the actual structure)
        codeverf_path = os.path.join(extract_dir, 'codeverf')
        # In some cases it might be in a subdirectory like 'usr/bin/codeverf'
        if not os.path.exists(codeverf_path):
            # Try to locate it recursively
            for root, dirs, files in os.walk(extract_dir):
                if 'codeverf' in files:
                    codeverf_path = os.path.join(root, 'codeverf')
                    break
        if not os.path.exists(codeverf_path):
            raise FileNotFoundError("codeverf binary not found in initramfs")
        
        with open(codeverf_path, 'rb') as f:
            code = f.read()
        
        # Apply the three known patches
        patterns = [
            (b'\xff\x25\x82\x2f\x00\x00\x68\x0a\x00\x00\x00\xe9\x40\xff\xff\xff',
             b'\xB8\x01\x00\x00\x00\xC3\x90\x90\x90\x90\x90\x90\x90\x90\x90\x90'),
            (b'\xff\x25\x72\x2f\x00\x00\x68\x0c\x00\x00\x00\xe9\x20\xff\xff\xff',
             b'\xB8\x01\x00\x00\x00\xC3\x90\x90\x90\x90\x90\x90\x90\x90\x90\x90'),
            (b'\xff\x25\x02\x2f\x00\x00\x68\x1a\x00\x00\x00\xe9\x40\xfe\xff\xff',
             b'\xB8\x01\x00\x00\x00\xC3\x90\x90\x90\x90\x90\x90\x90\x90\x90\x90'),
        ]
        for old, new in patterns:
            if old in code:
                code = code.replace(old, new)
            else:
                print(f"Warning: pattern {old.hex()} not found in codeverf")
        
        with open(codeverf_path, 'wb') as f:
            f.write(code)
        
        # Repack cpio
        new_cpio_path = os.path.join(tmpdir, 'initramfs_new')
        with open(new_cpio_path, 'wb') as f:
            subprocess.run(['sh', '-c', 'find . | cpio -o -H newc'], cwd=extract_dir, stdout=f, check=True)
        
        # Recompress with gzip
        new_gz_path = new_cpio_path + '.gz'
        subprocess.run(['gzip', '-c', new_cpio_path], stdout=open(new_gz_path, 'wb'), check=True)
        
        with open(new_gz_path, 'rb') as f:
            patched_compressed = f.read()
        return patched_compressed
    finally:
        subprocess.run(['rm', '-rf', tmpdir], check=False)

def replace_cpio_in_kernel(original_kernel_path, new_compressed_cpio, output_path):
    with open(original_kernel_path, 'rb') as f:
        kernel = bytearray(f.read())
    offset = find_cpio_offset(kernel)
    # The old cpio likely extends to end of file; replace from offset to EOF
    new_kernel = kernel[:offset] + new_compressed_cpio
    with open(output_path, 'wb') as f:
        f.write(new_kernel)
    # Pad to 4-byte boundary (optional but safe)
    # Bootloader load whole file, kernel finds initramfs via scanning; size doesn't have to match
    print(f"Patched kernel written to {output_path}")
