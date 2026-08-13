#!/usr/bin/env python3
"""
patch_ivrs.py - Generate a patched ACPI IVRS table that excludes one PCI bus
from the IVMD reservations declared by the firmware.

WHAT THIS IS FOR
----------------
Some AMD boards declare IVMD entries in their ACPI IVRS table covering a wide
range of device IDs. The kernel turns those into IOMMU_RESV_DIRECT reservations
and sets require_direct=1 on every device in the range. When VFIO tries to claim
one of them for passthrough, it attaches a blocking domain and the IOMMU core
rejects it:

    "Firmware has requested this device have a 1:1 IOMMU mapping, rejecting
     configuring the device without a 1:1 mapping. Contact your platform vendor."

This script splits those entries into two ranges that skip the given bus, so the
device you want to pass through falls outside while the rest of the system keeps
the reservation the firmware asked for.

USAGE
-----
    # 1. dump the real table from your machine
    cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml

    # 2. patch it, excluding the bus your device lives on
    ./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01

The bus comes from the device's PCI address: in 0000:01:00.0 the bus is 01.

TWO THINGS THE KERNEL REQUIRES FOR THE TABLE TO BE ACCEPTED
-----------------------------------------------------------
1. The ACPI header checksum must be recomputed (all bytes must sum to 0 mod 256).

2. oem_revision MUST be greater than the firmware table's. Otherwise
   acpi_table_initrd_override() in drivers/acpi/tables.c discards it WITHOUT
   PRINTING ANY MESSAGE AT ALL:

       if (test_and_set_bit(table_index, acpi_initrd_installed) ||
           existing_table->oem_revision >= table->oem_revision) {
               acpi_os_unmap_memory(table, ACPI_HEADER_SIZE);
               goto next_table;
       }

   This script bumps it automatically.
"""

import argparse
import struct
import sys

IVMD_TYPES = (0x20, 0x21, 0x22)  # ALL devices / SPECIFIED device / DEVICE RANGE
IVMD_RANGE = 0x22

# Offsets within the standard 36-byte ACPI header
OFF_SIGNATURE = 0
OFF_LENGTH = 4
OFF_CHECKSUM = 9
OFF_OEM_ID = 10
OFF_OEM_TABLE_ID = 16
OFF_OEM_REVISION = 24

# The IVRS body starts after the ACPI header (36) + IVinfo (4) + reserved (8)
BODY_START = 48


def bdf(devid):
    """Format a 16-bit device ID as bus:device.function."""
    return f"{devid >> 8:02x}:{(devid >> 3) & 0x1f:02x}.{devid & 7}"


def walk(data):
    """Iterate over the IVRS body entries: (offset, type, flags, length)."""
    off = BODY_START
    while off + 4 <= len(data):
        typ = data[off]
        flags = data[off + 1]
        length = struct.unpack_from("<H", data, off + 2)[0]
        if length == 0:
            break
        yield off, typ, flags, length
        off += length


def describe(data, label):
    print(f"--- {label} ({len(data)} bytes) ---")
    for off, typ, flags, length in walk(data):
        if typ in IVMD_TYPES:
            lo = struct.unpack_from("<H", data, off + 4)[0]
            hi = struct.unpack_from("<H", data, off + 6)[0]
            start, mlen = struct.unpack_from("<QQ", data, off + 16)
            print(f"  [+0x{off:04x}] IVMD 0x{typ:02x} flags=0x{flags:02x} "
                  f"devid 0x{lo:04x}({bdf(lo)})..0x{hi:04x}({bdf(hi)}) "
                  f"mem 0x{start:012x}+{mlen}")
        else:
            print(f"  [+0x{off:04x}] IVHD 0x{typ:02x} len={length}")


def covering(data, devid):
    """Offsets of the IVMD entries covering a given device ID."""
    hits = []
    for off, typ, _flags, _length in walk(data):
        if typ in IVMD_TYPES:
            lo = struct.unpack_from("<H", data, off + 4)[0]
            hi = struct.unpack_from("<H", data, off + 6)[0]
            if lo <= devid <= hi:
                hits.append(off)
    return hits


def patch(data, bus):
    """Split the IVMD entries covering `bus` into two ranges that exclude it."""
    lo_end = (bus << 8) - 1        # last device ID before the bus
    hi_start = (bus + 1) << 8      # first device ID after the bus
    bus_lo = bus << 8
    bus_hi = (bus << 8) | 0xFF

    out = bytearray(data)
    new_entries = bytearray()
    patched = 0

    for off, typ, _flags, length in walk(data):
        if typ != IVMD_RANGE:
            continue
        lo = struct.unpack_from("<H", data, off + 4)[0]
        hi = struct.unpack_from("<H", data, off + 6)[0]

        # Only touch ranges that cover the whole bus
        if not (lo <= bus_lo and hi >= bus_hi):
            continue

        # 1) shorten the existing entry so it ends before the bus
        struct.pack_into("<H", out, off + 6, lo_end)

        # 2) clone it to cover from after the bus to the original end
        clone = bytearray(data[off:off + length])
        struct.pack_into("<H", clone, 4, hi_start)
        struct.pack_into("<H", clone, 6, hi)
        new_entries += clone
        patched += 1

    if patched == 0:
        sys.exit(f"ERROR: no IVMD range covers bus 0x{bus:02x}. "
                 "Nothing to patch: your problem is something else.")

    # Insert the new entries right after the last original IVMD, preserving the
    # structural order of the table.
    last_ivmd_end = max(off + length for off, typ, _f, length in walk(data)
                        if typ in IVMD_TYPES)
    out = out[:last_ivmd_end] + new_entries + out[last_ivmd_end:]

    # CRITICAL: bump oem_revision. Without this the kernel silently discards the
    # table (see module docstring).
    old_rev = struct.unpack_from("<I", out, OFF_OEM_REVISION)[0]
    struct.pack_into("<I", out, OFF_OEM_REVISION, old_rev + 1)

    # Update the header Length and recompute the checksum
    struct.pack_into("<I", out, OFF_LENGTH, len(out))
    out[OFF_CHECKSUM] = 0
    out[OFF_CHECKSUM] = (-sum(out)) & 0xFF

    return bytes(out), patched


def main():
    ap = argparse.ArgumentParser(
        description="Patch an ACPI IVRS table to exclude a PCI bus from the "
                    "firmware's IVMD reservations.")
    ap.add_argument("input", help="original IVRS table "
                                  "(cp /sys/firmware/acpi/tables/IVRS ...)")
    ap.add_argument("output", help="path to write the patched table to")
    ap.add_argument("--bus", required=True,
                    help="PCI bus to exclude, in hex. For 0000:01:00.0 use 01")
    args = ap.parse_args()

    try:
        bus = int(args.bus, 16)
    except ValueError:
        sys.exit(f"ERROR: --bus must be hexadecimal (e.g. 01), got {args.bus!r}")
    if not 0 <= bus <= 0xFF:
        sys.exit("ERROR: --bus out of range (00..ff)")

    original = open(args.input, "rb").read()

    if original[OFF_SIGNATURE:OFF_SIGNATURE + 4] != b"IVRS":
        sys.exit(f"ERROR: {args.input} is not an IVRS table "
                 f"(signature={original[:4]!r})")
    if (sum(original) & 0xFF) != 0:
        sys.exit("ERROR: the original table's checksum does not add up. "
                 "Re-dump it from /sys/firmware/acpi/tables/IVRS.")

    dev_lo = bus << 8
    dev_hi = (bus << 8) | 0xFF

    describe(original, "ORIGINAL")
    patched_data, n = patch(original, bus)
    print()
    describe(patched_data, "PATCHED")

    # --- Hard checks: if any fails, nothing is written ---
    checks = [
        ("checksum is valid",
         (sum(patched_data) & 0xFF) == 0),
        ("header Length matches actual size",
         struct.unpack_from("<I", patched_data, OFF_LENGTH)[0] == len(patched_data)),
        (f"bus 0x{bus:02x} (function .0) is NO LONGER covered",
         len(covering(patched_data, dev_lo)) == 0),
        (f"bus 0x{bus:02x} (function .7) is NO LONGER covered",
         len(covering(patched_data, dev_lo | 7)) == 0),
        (f"bus 0x{bus:02x} (last device ID) is NO LONGER covered",
         len(covering(patched_data, dev_hi)) == 0),
        ("every other device keeps its reservation",
         all(len(covering(patched_data, d)) == n
             for d in (0x0002, dev_lo - 1, dev_hi + 1)
             if 0 <= d <= 0xFFFF and not (dev_lo <= d <= dev_hi)
             and len(covering(original, d)) == n)),
        ("oem_revision EXCEEDS the firmware's (otherwise silently ignored)",
         struct.unpack_from("<I", patched_data, OFF_OEM_REVISION)[0] >
         struct.unpack_from("<I", original, OFF_OEM_REVISION)[0]),
        ("signature/oem_id/oem_table_id untouched (the kernel's match criteria)",
         patched_data[0:4] == original[0:4]
         and patched_data[OFF_OEM_ID:OFF_OEM_ID + 6] == original[OFF_OEM_ID:OFF_OEM_ID + 6]
         and patched_data[OFF_OEM_TABLE_ID:OFF_OEM_TABLE_ID + 8]
         == original[OFF_OEM_TABLE_ID:OFF_OEM_TABLE_ID + 8]),
        ("IVHD block count unchanged",
         sum(1 for _o, t, _f, _l in walk(original) if t not in IVMD_TYPES) ==
         sum(1 for _o, t, _f, _l in walk(patched_data) if t not in IVMD_TYPES)),
    ]

    print("\n--- VERIFICATION ---")
    ok = True
    for name, result in checks:
        print(f"  [{'OK  ' if result else 'FAIL'}] {name}")
        ok = ok and result

    if not ok:
        sys.exit("\nABORTED: a check failed, nothing was written.")

    with open(args.output, "wb") as f:
        f.write(patched_data)
    print(f"\nWrote: {args.output} ({len(patched_data)} bytes, {n} IVMD entries split)")
    print(f"oem_revision: {struct.unpack_from('<I', original, OFF_OEM_REVISION)[0]}"
          f" -> {struct.unpack_from('<I', patched_data, OFF_OEM_REVISION)[0]}")


if __name__ == "__main__":
    main()
