#!/bin/bash
# uninstall.sh - Remove the ACPI IVRS override and put the system back as it was.
#
#   sudo ./uninstall.sh [kernel-version]
#
# If the system does NOT boot, pick an older kernel from the GRUB menu
# (Advanced options for ...) whose initrd has no override, and run this from there.

set -euo pipefail

KVER="${1:-$(uname -r)}"

CPIO_DIR=/usr/local/lib/acpi-override
HOOK_PATH=/etc/initramfs-tools/hooks/acpi_ivrs_override

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root" >&2
    exit 1
fi

echo "== Removing the hook =="
if [ -e "${HOOK_PATH}" ]; then
    rm -f "${HOOK_PATH}"
    echo "  removed ${HOOK_PATH}"
else
    echo "  ${HOOK_PATH} did not exist"
fi

echo
echo "== Removing the cpio =="
if [ -d "${CPIO_DIR}" ]; then
    rm -rf "${CPIO_DIR}"
    echo "  removed ${CPIO_DIR}"
else
    echo "  ${CPIO_DIR} did not exist"
fi

echo
echo "== Regenerating initramfs for ${KVER} =="
update-initramfs -u -k "${KVER}"

cat <<EOF

===========================================================================
Uninstalled. Reboot to go back to the firmware's IVRS table.

After rebooting, to confirm the override is no longer applied:

    dmesg | grep -i 'Table Upgrade'      -> should print nothing

If you disabled Secure Boot only for this, you can re-enable it in the BIOS.
===========================================================================
EOF
