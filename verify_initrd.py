#!/usr/bin/env python3
"""
verify_initrd.py - Comprueba que la tabla ACPI parcheada viaja dentro del initramfs.

POR QUE HACE FALTA UNA HERRAMIENTA PARA ESTO
---------------------------------------------
`cpio -it < initrd.img` NO sirve: el initramfs es una concatenacion de varios
archivos cpio (microcodigo, firmware, nuestro override) seguidos del cpio
principal comprimido. GNU cpio se detiene en el TRAILER!!! del primero, asi que
parece que el override no esta cuando en realidad si esta.

Este script recorre TODOS los cpio del prefijo no comprimido y extrae la tabla
para compararla con la que esperabas instalar.

USO
---
    ./verify_initrd.py /boot/initrd.img-$(uname -r) IVRS.patched.aml
"""

import hashlib
import struct
import sys

CPIO_MAGIC = b"070701"
# El prefijo no comprimido nunca es grande; mas alla de esto es el cpio principal
SCAN_LIMIT = 8 * 1024 * 1024


def iter_cpio_members(data):
    """Recorre los archivos cpio 'newc' concatenados al principio del initrd."""
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
        sys.exit(f"uso: {sys.argv[0]} <initrd.img> <tabla_esperada.aml>")

    initrd_path, expected_path = sys.argv[1], sys.argv[2]

    data = open(initrd_path, "rb").read()
    expected = open(expected_path, "rb").read()

    print(f"initrd: {initrd_path} ({len(data)} bytes)")
    print("\nMiembros del early-initramfs (todos los cpio concatenados):")

    found = None
    for name, content in iter_cpio_members(data):
        print(f"   {name}")
        if name.endswith(".aml"):
            found = (name, content)

    print()
    if found is None:
        print(">>> NO se encontro ninguna tabla .aml en el initrd.")
        print("    Revisa que el hook este instalado y que hayas corrido")
        print("    update-initramfs despues de instalarlo.")
        sys.exit(1)

    name, content = found
    got = hashlib.sha256(content).hexdigest()
    want = hashlib.sha256(expected).hexdigest()
    oem_rev = struct.unpack_from("<I", content, 24)[0]

    print(f">>> {name}: {len(content)} bytes")
    print(f"    sha256 en initrd : {got}")
    print(f"    sha256 esperado  : {want}")
    print(f"    COINCIDE         : {got == want}")
    print(f"    firma            : {content[:4].decode('ascii', 'replace')}")
    print(f"    checksum ok      : {(sum(content) & 0xff) == 0}")
    print(f"    oem_revision     : {oem_rev}")

    if got != want:
        sys.exit(1)

    print("\nTodo correcto. Reinicia y comprueba con:")
    print("    dmesg | grep -i 'Table Upgrade'")


if __name__ == "__main__":
    main()
