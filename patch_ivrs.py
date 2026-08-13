#!/usr/bin/env python3
"""
patch_ivrs.py - Genera una tabla ACPI IVRS parcheada que excluye un bus PCI
concreto de las reservas IVMD declaradas por el firmware.

PARA QUE SIRVE
--------------
Algunas placas AMD declaran entradas IVMD en su tabla ACPI IVRS que cubren un
rango amplio de device IDs. El kernel las traduce a reservas IOMMU_RESV_DIRECT y
marca require_direct=1 en todos los dispositivos del rango. Cuando VFIO intenta
reclamar uno de ellos para passthrough, adjunta un blocking domain y el core del
IOMMU lo rechaza:

    "Firmware has requested this device have a 1:1 IOMMU mapping, rejecting
     configuring the device without a 1:1 mapping. Contact your platform vendor."

Este script parte esas entradas en dos rangos que saltan el bus indicado, de
forma que el dispositivo que quieres pasar a una VM queda fuera y todo el resto
del sistema conserva la reserva que pidio el firmware.

USO
---
    # 1. volcar la tabla real de tu maquina
    cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml

    # 2. parchearla excluyendo el bus donde vive tu dispositivo
    ./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01

El bus se saca de la direccion PCI del dispositivo: en 0000:01:00.0 el bus es 01.

DOS DETALLES QUE HACEN FALTA PARA QUE EL KERNEL ACEPTE LA TABLA
---------------------------------------------------------------
1. El checksum de la cabecera ACPI debe recalcularse (suma de todos los bytes
   igual a 0 modulo 256).

2. El campo oem_revision DEBE ser mayor que el de la tabla del firmware. Si no,
   acpi_table_initrd_override() en drivers/acpi/tables.c la descarta SIN IMPRIMIR
   NINGUN MENSAJE:

       if (test_and_set_bit(table_index, acpi_initrd_installed) ||
           existing_table->oem_revision >= table->oem_revision) {
               acpi_os_unmap_memory(table, ACPI_HEADER_SIZE);
               goto next_table;
       }

   Este script lo incrementa automaticamente.
"""

import argparse
import struct
import sys

IVMD_TYPES = (0x20, 0x21, 0x22)  # ALL devices / SPECIFIED device / DEVICE RANGE
IVMD_RANGE = 0x22

# Offsets dentro de la cabecera ACPI estandar (36 bytes)
OFF_SIGNATURE = 0
OFF_LENGTH = 4
OFF_CHECKSUM = 9
OFF_OEM_ID = 10
OFF_OEM_TABLE_ID = 16
OFF_OEM_REVISION = 24

# El cuerpo de IVRS empieza tras la cabecera ACPI (36) + IVinfo (4) + reservado (8)
BODY_START = 48


def bdf(devid):
    """Formatea un device ID de 16 bits como bus:dispositivo.funcion."""
    return f"{devid >> 8:02x}:{(devid >> 3) & 0x1f:02x}.{devid & 7}"


def walk(data):
    """Itera las entradas del cuerpo de IVRS: (offset, tipo, flags, longitud)."""
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
    """Offsets de las IVMD que cubren un devid dado."""
    hits = []
    for off, typ, _flags, _length in walk(data):
        if typ in IVMD_TYPES:
            lo = struct.unpack_from("<H", data, off + 4)[0]
            hi = struct.unpack_from("<H", data, off + 6)[0]
            if lo <= devid <= hi:
                hits.append(off)
    return hits


def patch(data, bus):
    """Parte las IVMD que cubren `bus` en dos rangos que lo excluyen."""
    lo_end = (bus << 8) - 1        # ultimo devid antes del bus
    hi_start = (bus + 1) << 8      # primer devid despues del bus
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

        # Solo tocamos rangos que cubren el bus entero
        if not (lo <= bus_lo and hi >= bus_hi):
            continue

        # 1) acortar la entrada existente para que termine antes del bus
        struct.pack_into("<H", out, off + 6, lo_end)

        # 2) clonarla para cubrir desde despues del bus hasta el fin original
        clone = bytearray(data[off:off + length])
        struct.pack_into("<H", clone, 4, hi_start)
        struct.pack_into("<H", clone, 6, hi)
        new_entries += clone
        patched += 1

    if patched == 0:
        sys.exit(f"ERROR: ninguna IVMD de rango cubre el bus 0x{bus:02x}. "
                 "Nada que parchear: tu problema es otro.")

    # Insertar las entradas nuevas justo despues de la ultima IVMD original,
    # conservando el orden estructural de la tabla.
    last_ivmd_end = max(off + length for off, typ, _f, length in walk(data)
                        if typ in IVMD_TYPES)
    out = out[:last_ivmd_end] + new_entries + out[last_ivmd_end:]

    # CRITICO: incrementar oem_revision. Sin esto el kernel descarta la tabla
    # en silencio (ver docstring).
    old_rev = struct.unpack_from("<I", out, OFF_OEM_REVISION)[0]
    struct.pack_into("<I", out, OFF_OEM_REVISION, old_rev + 1)

    # Actualizar Length de la cabecera y recalcular checksum
    struct.pack_into("<I", out, OFF_LENGTH, len(out))
    out[OFF_CHECKSUM] = 0
    out[OFF_CHECKSUM] = (-sum(out)) & 0xFF

    return bytes(out), patched


def main():
    ap = argparse.ArgumentParser(
        description="Parchea una tabla ACPI IVRS para excluir un bus PCI de las "
                    "reservas IVMD del firmware.")
    ap.add_argument("input", help="tabla IVRS original "
                                  "(cp /sys/firmware/acpi/tables/IVRS ...)")
    ap.add_argument("output", help="ruta donde escribir la tabla parcheada")
    ap.add_argument("--bus", required=True,
                    help="bus PCI a excluir, en hex. Para 0000:01:00.0 usa 01")
    args = ap.parse_args()

    try:
        bus = int(args.bus, 16)
    except ValueError:
        sys.exit(f"ERROR: --bus debe ser hexadecimal (ej. 01), recibido {args.bus!r}")
    if not 0 <= bus <= 0xFF:
        sys.exit("ERROR: --bus fuera de rango (00..ff)")

    original = open(args.input, "rb").read()

    if original[OFF_SIGNATURE:OFF_SIGNATURE + 4] != b"IVRS":
        sys.exit(f"ERROR: {args.input} no es una tabla IVRS "
                 f"(firma={original[:4]!r})")
    if (sum(original) & 0xFF) != 0:
        sys.exit("ERROR: el checksum de la tabla original no cuadra. "
                 "Vuelve a volcarla desde /sys/firmware/acpi/tables/IVRS.")

    gpu_lo = bus << 8
    gpu_hi = (bus << 8) | 0xFF

    describe(original, "ORIGINAL")
    patched_data, n = patch(original, bus)
    print()
    describe(patched_data, "PARCHEADA")

    # --- Verificaciones duras: si alguna falla, no se escribe nada ---
    checks = [
        ("checksum valido",
         (sum(patched_data) & 0xFF) == 0),
        ("Length de cabecera coincide",
         struct.unpack_from("<I", patched_data, OFF_LENGTH)[0] == len(patched_data)),
        (f"bus 0x{bus:02x} (funcion .0) YA NO esta cubierto",
         len(covering(patched_data, gpu_lo)) == 0),
        (f"bus 0x{bus:02x} (funcion .7) YA NO esta cubierto",
         len(covering(patched_data, gpu_lo | 7)) == 0),
        (f"bus 0x{bus:02x} (ultimo devid) YA NO esta cubierto",
         len(covering(patched_data, gpu_hi)) == 0),
        ("el resto de dispositivos conserva su reserva",
         all(len(covering(patched_data, d)) == n
             for d in (0x0002, gpu_lo - 1, gpu_hi + 1)
             if 0 <= d <= 0xFFFF and not (gpu_lo <= d <= gpu_hi)
             and len(covering(original, d)) == n)),
        ("oem_revision SUPERA la del firmware (si no, se ignora en silencio)",
         struct.unpack_from("<I", patched_data, OFF_OEM_REVISION)[0] >
         struct.unpack_from("<I", original, OFF_OEM_REVISION)[0]),
        ("signature/oem_id/oem_table_id intactos (criterio de match del kernel)",
         patched_data[0:4] == original[0:4]
         and patched_data[OFF_OEM_ID:OFF_OEM_ID + 6] == original[OFF_OEM_ID:OFF_OEM_ID + 6]
         and patched_data[OFF_OEM_TABLE_ID:OFF_OEM_TABLE_ID + 8]
         == original[OFF_OEM_TABLE_ID:OFF_OEM_TABLE_ID + 8]),
        ("numero de bloques IVHD sin cambios",
         sum(1 for _o, t, _f, _l in walk(original) if t not in IVMD_TYPES) ==
         sum(1 for _o, t, _f, _l in walk(patched_data) if t not in IVMD_TYPES)),
    ]

    print("\n--- VERIFICACION ---")
    ok = True
    for name, result in checks:
        print(f"  [{'OK ' if result else 'FALLO'}] {name}")
        ok = ok and result

    if not ok:
        sys.exit("\nABORTADO: alguna verificacion fallo, no se escribio el archivo.")

    with open(args.output, "wb") as f:
        f.write(patched_data)
    print(f"\nEscrito: {args.output} ({len(patched_data)} bytes, {n} IVMD divididas)")
    print(f"oem_revision: {struct.unpack_from('<I', original, OFF_OEM_REVISION)[0]}"
          f" -> {struct.unpack_from('<I', patched_data, OFF_OEM_REVISION)[0]}")


if __name__ == "__main__":
    main()
