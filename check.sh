#!/bin/bash
# check.sh - Diagnostico de solo lectura para el error
# "Firmware has requested this device have a 1:1 IOMMU mapping".
#
#   sudo ./check.sh [direccion-pci]
#
# Ejemplo:
#   sudo ./check.sh 0000:01:00.0
#
# No modifica nada.

DEV="${1:-}"

echo "======================================================================"
echo " 1. Sistema"
echo "======================================================================"
uname -r
grep -E 'CONFIG_ACPI_TABLE_UPGRADE' "/boot/config-$(uname -r)" 2>/dev/null \
    || echo "CONFIG_ACPI_TABLE_UPGRADE: no encontrado (el metodo no funcionara)"
echo -n "lockdown: "; cat /sys/kernel/security/lockdown 2>/dev/null || echo "no expuesto"
command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null

echo
echo "======================================================================"
echo " 2. El error, si ya ocurrio"
echo "======================================================================"
dmesg | grep -i '1:1 IOMMU mapping' | tail -5 || echo "(no aparece en dmesg)"

echo
echo "======================================================================"
echo " 3. Se aplico algun override de tabla ACPI?"
echo "======================================================================"
dmesg | grep -iE 'Table Upgrade|table found in initrd' || echo "(ninguno)"

echo
echo "======================================================================"
echo " 4. Regiones reservadas por dispositivo"
echo "======================================================================"
echo "Las lineas 'direct' son las que provocan el rechazo de VFIO."
echo
for d in /sys/bus/pci/devices/*; do
    rr="$d/iommu_group/reserved_regions"
    [ -r "$rr" ] || continue
    n=$(grep -c direct "$rr" 2>/dev/null)
    [ "$n" -gt 0 ] || continue
    addr=$(basename "$d")
    desc=$(lspci -nns "${addr#0000:}" 2>/dev/null | cut -c1-70)
    printf "  direct=%s  %s\n" "$n" "${desc:-$addr}"
done
echo
echo "(los dispositivos sin lineas 'direct' no se listan)"

if [ -n "$DEV" ]; then
    echo
    echo "======================================================================"
    echo " 5. Detalle de $DEV"
    echo "======================================================================"
    rr="/sys/bus/pci/devices/$DEV/iommu_group/reserved_regions"
    if [ -r "$rr" ]; then
        cat "$rr"
        grp=$(basename "$(readlink -f "/sys/bus/pci/devices/$DEV/iommu_group")")
        echo
        echo "Grupo IOMMU $grp contiene:"
        for x in /sys/kernel/iommu_groups/"$grp"/devices/*; do
            a=$(basename "$x")
            echo "  $a  $(lspci -nns "${a#0000:}" 2>/dev/null | cut -d' ' -f2- | cut -c1-60)"
        done
        echo
        lspci -nnks "${DEV#0000:}" | grep -iE 'driver|modules'
    else
        echo "No existe $rr"
    fi
fi

echo
echo "======================================================================"
echo " Siguiente paso"
echo "======================================================================"
echo "Si tu dispositivo aparece con lineas 'direct', decodifica la IVRS:"
echo "    sudo ./dump_ivrs.py --device ${DEV:-0000:01:00.0}"
