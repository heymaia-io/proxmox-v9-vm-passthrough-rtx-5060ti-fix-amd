#!/bin/bash
# check.sh - Read-only diagnostics for the error
# "Firmware has requested this device have a 1:1 IOMMU mapping".
#
#   sudo ./check.sh [pci-address]
#
# Example:
#   sudo ./check.sh 0000:01:00.0
#
# Modifies nothing.

DEV="${1:-}"

echo "======================================================================"
echo " 1. System"
echo "======================================================================"
uname -r
grep -E 'CONFIG_ACPI_TABLE_UPGRADE' "/boot/config-$(uname -r)" 2>/dev/null \
    || echo "CONFIG_ACPI_TABLE_UPGRADE: not found (this method will not work)"
echo -n "lockdown: "; cat /sys/kernel/security/lockdown 2>/dev/null || echo "not exposed"
command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null

echo
echo "======================================================================"
echo " 2. The error, if it already happened"
echo "======================================================================"
dmesg | grep -i '1:1 IOMMU mapping' | tail -5 || echo "(not present in dmesg)"

echo
echo "======================================================================"
echo " 3. Was any ACPI table override applied?"
echo "======================================================================"
dmesg | grep -iE 'Table Upgrade|table found in initrd' || echo "(none)"

echo
echo "======================================================================"
echo " 4. Reserved regions per device"
echo "======================================================================"
echo "The 'direct' lines are what makes VFIO refuse the passthrough."
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
echo "(devices without 'direct' lines are not listed)"

if [ -n "$DEV" ]; then
    echo
    echo "======================================================================"
    echo " 5. Details for $DEV"
    echo "======================================================================"
    rr="/sys/bus/pci/devices/$DEV/iommu_group/reserved_regions"
    if [ -r "$rr" ]; then
        cat "$rr"
        grp=$(basename "$(readlink -f "/sys/bus/pci/devices/$DEV/iommu_group")")
        echo
        echo "IOMMU group $grp contains:"
        for x in /sys/kernel/iommu_groups/"$grp"/devices/*; do
            a=$(basename "$x")
            echo "  $a  $(lspci -nns "${a#0000:}" 2>/dev/null | cut -d' ' -f2- | cut -c1-60)"
        done
        echo
        lspci -nnks "${DEV#0000:}" | grep -iE 'driver|modules'
    else
        echo "$rr does not exist"
    fi
fi

echo
echo "======================================================================"
echo " Next step"
echo "======================================================================"
echo "If your device shows 'direct' lines, decode the IVRS table:"
echo "    sudo ./dump_ivrs.py --device ${DEV:-0000:01:00.0}"
