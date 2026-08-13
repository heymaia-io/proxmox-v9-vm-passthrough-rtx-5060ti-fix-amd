#!/usr/bin/env python3
"""
dump_ivrs.py - Dump and decode the ACPI IVRS table on an AMD machine.

Use it to diagnose the passthrough error:

    "Firmware has requested this device have a 1:1 IOMMU mapping..."

It prints the IVMD entries (the firmware's memory reservations) and, if you give
it a device's PCI address, tells you whether that device falls inside one.

USAGE
-----
    sudo ./dump_ivrs.py                          # reads /sys/firmware/acpi/tables/IVRS
    sudo ./dump_ivrs.py --device 0000:01:00.0    # check a specific device
    ./dump_ivrs.py --file IVRS.original.aml      # analyse a saved dump
"""

import argparse
import struct
import sys

IVMD_TYPES = {0x20: "ALL devices", 0x21: "SPECIFIED device", 0x22: "DEVICE RANGE"}
IVHD_TYPES = (0x10, 0x11, 0x40)
BODY_START = 48

DEFAULT_PATH = "/sys/firmware/acpi/tables/IVRS"


def bdf(devid):
    return f"{devid >> 8:02x}:{(devid >> 3) & 0x1f:02x}.{devid & 7}"


def parse_device(s):
    """Convert '0000:01:00.0' or '01:00.0' into a 16-bit device ID."""
    s = s.strip()
    if s.count(":") == 2:
        s = s.split(":", 1)[1]
    try:
        busdev, func = s.split(".")
        bus, dev = busdev.split(":")
        return (int(bus, 16) << 8) | (int(dev, 16) << 3) | int(func)
    except ValueError:
        sys.exit(f"ERROR: cannot parse PCI address {s!r}. "
                 "Expected format: 0000:01:00.0")


def walk(data):
    off = BODY_START
    while off + 4 <= len(data):
        typ = data[off]
        flags = data[off + 1]
        length = struct.unpack_from("<H", data, off + 2)[0]
        if length == 0:
            break
        yield off, typ, flags, length
        off += length


def main():
    ap = argparse.ArgumentParser(description="Decode the ACPI IVRS table.")
    ap.add_argument("--file", default=DEFAULT_PATH,
                    help=f"table to read (default {DEFAULT_PATH})")
    ap.add_argument("--device", help="PCI address to check, e.g. 0000:01:00.0")
    args = ap.parse_args()

    try:
        data = open(args.file, "rb").read()
    except PermissionError:
        sys.exit(f"ERROR: permission denied reading {args.file}. Try sudo.")
    except FileNotFoundError:
        sys.exit(f"ERROR: {args.file} does not exist. "
                 "This machine may not have an AMD IOMMU.")

    if data[:4] != b"IVRS":
        sys.exit(f"ERROR: unexpected signature {data[:4]!r}, not an IVRS table")

    oem_id = data[10:16].decode("ascii", "replace").strip("\x00 ")
    oem_table_id = data[16:24].decode("ascii", "replace").strip("\x00 ")
    oem_rev = struct.unpack_from("<I", data, 24)[0]

    print(f"IVRS: {len(data)} bytes  checksum_ok={(sum(data) & 0xff) == 0}")
    print(f"  oem_id={oem_id!r}  oem_table_id={oem_table_id!r}  oem_revision={oem_rev}")
    print()

    ivmds = []
    for off, typ, flags, length in walk(data):
        if typ in IVMD_TYPES:
            lo = struct.unpack_from("<H", data, off + 4)[0]
            hi = struct.unpack_from("<H", data, off + 6)[0]
            start, mlen = struct.unpack_from("<QQ", data, off + 16)
            ivmds.append((off, typ, flags, lo, hi, start, mlen))
            print(f"[+0x{off:04x}] IVMD type 0x{typ:02x} ({IVMD_TYPES[typ]}) "
                  f"flags=0x{flags:02x}")
            print(f"          devid 0x{lo:04x} ({bdf(lo)}) .. "
                  f"0x{hi:04x} ({bdf(hi)})")
            print(f"          mem   0x{start:012x} .. 0x{start + mlen - 1:012x}"
                  f"  ({mlen} bytes / {mlen // 1024} KB)")
            print(f"          unity={bool(flags & 1)} IR={bool(flags & 2)} "
                  f"IW={bool(flags & 4)} exclusion_range={bool(flags & 8)}")
        elif typ in IVHD_TYPES:
            print(f"[+0x{off:04x}] IVHD type 0x{typ:02x} len={length}")

    if not ivmds:
        print("No IVMD entries. This is not the problem you have.")
        return

    print(f"\nTotal: {len(ivmds)} IVMD entries")

    if args.device:
        devid = parse_device(args.device)
        print(f"\n--- {args.device} (devid 0x{devid:04x}) ---")
        hits = [e for e in ivmds if e[3] <= devid <= e[4]]
        if hits:
            print(f"  COVERED by {len(hits)} IVMD entry/entries.")
            print(f"  The kernel will set require_direct=1 on this device and")
            print(f"  VFIO will refuse the passthrough.")
            print(f"  Bus to exclude with patch_ivrs.py: --bus {devid >> 8:02x}")
        else:
            print("  NOT covered by any IVMD. Your problem is something else.")

    print("\nAlso check what the kernel sees:")
    print("  cat /sys/bus/pci/devices/<ADDR>/iommu_group/reserved_regions")
    print("  ('direct' lines are the ones causing the rejection)")


if __name__ == "__main__":
    main()
