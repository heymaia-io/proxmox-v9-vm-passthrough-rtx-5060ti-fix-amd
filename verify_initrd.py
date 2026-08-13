#!/usr/bin/env python3
"""
verify_initrd.py - Check that the patched ACPI table actually travels inside the
initramfs.

WHY A DEDICATED TOOL IS NEEDED
------------------------------
`cpio -it < initrd.img` does NOT work here: the initramfs is a concatenation of
several cpio archives (microcode, firmware, our override) followed by the main
compressed one. GNU cpio stops at the first TRAILER!!!, so it looks like the
override is missing when in fact it is there.

This script walks every cpio archive in the uncompressed prefix and extracts the
table to compare it with the one you meant to install.

USAGE
-----
    ./verify_initrd.py /boot/initrd.img-$(uname -r) IVRS.patched.aml
"""

import hashlib
import struct
import sys

CPIO_MAGIC = b"070701"
# The uncompressed prefix is never large; past this it is the main cpio
SCAN_LIMIT = 8 * 1024 * 1024


def iter_cpio_members(data):
    """Walk the concatenated 'newc' cpio archives at the start of the initrd."""
    off = 0
    while True:
        while off < len(data) and data[off:off + 6] != CPIO_MAGIC:
            off += 1
            if off > SCAN_LIMIT:
                return
        if off >= len(data) or data[off:off + 6] != CPIO_MAGIC:
            return

        header = data[off:off + 110]
        if len(header) < 110:
            return

        def field(i):
            return int(header[6 + i * 8:14 + i * 8], 16)

        filesize = field(6)
        namesize = field(11)
        name = data[off + 110:off + 110 + namesize - 1].decode("ascii", "replace")

        data_start = off + 110 + namesize
        data_start += (-data_start) % 4

        if name == "TRAILER!!!":
            off = data_start
            continue

        yield name, data[data_start:data_start + filesize]

        off = data_start + filesize
        off += (-off) % 4


def main():
    if len(sys.argv) != 3:
        sys.exit(f"usage: {sys.argv[0]} <initrd.img> <expected_table.aml>")

    initrd_path, expected_path = sys.argv[1], sys.argv[2]

    data = open(initrd_path, "rb").read()
    expected = open(expected_path, "rb").read()

    print(f"initrd: {initrd_path} ({len(data)} bytes)")
    print("\nEarly-initramfs members (all concatenated cpio archives):")

    found = None
    for name, content in iter_cpio_members(data):
        print(f"   {name}")
        if name.endswith(".aml"):
            found = (name, content)

    print()
    if found is None:
        print(">>> No .aml table found in the initrd.")
        print("    Check that the hook is installed and that you ran")
        print("    update-initramfs after installing it.")
        sys.exit(1)

    name, content = found
    got = hashlib.sha256(content).hexdigest()
    want = hashlib.sha256(expected).hexdigest()
    oem_rev = struct.unpack_from("<I", content, 24)[0]

    print(f">>> {name}: {len(content)} bytes")
    print(f"    sha256 in initrd : {got}")
    print(f"    sha256 expected  : {want}")
    print(f"    MATCH            : {got == want}")
    print(f"    signature        : {content[:4].decode('ascii', 'replace')}")
    print(f"    checksum ok      : {(sum(content) & 0xff) == 0}")
    print(f"    oem_revision     : {oem_rev}")

    if got != want:
        sys.exit(1)

    print("\nAll good. Reboot and check with:")
    print("    dmesg | grep -i 'Table Upgrade'")


if __name__ == "__main__":
    main()
