#!/bin/bash
# install.sh - Install the ACPI IVRS table override.
#
# Packs the patched table into a cpio archive, installs the initramfs-tools hook
# and regenerates the initramfs for the given kernel (the running one by default).
#
# Before regenerating, it saves a copy of the current initrd under ./backup/.
#
#   sudo ./install.sh IVRS.patched.aml [kernel-version]
#
# IMPORTANT: only one kernel is regenerated, on purpose. Using `-k all` would
# apply the override to your older kernels too, and those are exactly your
# rollback path if the system fails to boot.

set -euo pipefail

TABLE="${1:-}"
KVER="${2:-$(uname -r)}"

CPIO_DIR=/usr/local/lib/acpi-override
CPIO_PATH="${CPIO_DIR}/acpi_ivrs_override.cpio"
HOOK_PATH=/etc/initramfs-tools/hooks/acpi_ivrs_override
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${HERE}/backup"

if [ -z "${TABLE}" ]; then
    echo "usage: $0 <IVRS.patched.aml> [kernel-version]" >&2
    exit 1
fi
if [ ! -r "${TABLE}" ]; then
    echo "ERROR: cannot read ${TABLE}" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root" >&2
    exit 1
fi

# --- Pre-flight checks ------------------------------------------------------

echo "== Pre-flight checks =="

if ! grep -q '^CONFIG_ACPI_TABLE_UPGRADE=y' "/boot/config-${KVER}" 2>/dev/null; then
    echo "ERROR: kernel ${KVER} does not have CONFIG_ACPI_TABLE_UPGRADE=y." >&2
    echo "       This method cannot work on it." >&2
    exit 1
fi
echo "  [OK] CONFIG_ACPI_TABLE_UPGRADE=y"

LOCKDOWN=$(cat /sys/kernel/security/lockdown 2>/dev/null || echo "")
if echo "${LOCKDOWN}" | grep -q '\[integrity\]\|\[confidentiality\]'; then
    echo "ERROR: the kernel is in lockdown mode: ${LOCKDOWN}" >&2
    echo "       acpi_table_upgrade() is skipped under lockdown." >&2
    echo "       Disable Secure Boot in the BIOS and try again." >&2
    exit 1
fi
echo "  [OK] lockdown: ${LOCKDOWN:-not exposed}"

if ! grep -q 'prepend_earlyinitramfs' /usr/share/initramfs-tools/hook-functions 2>/dev/null; then
    echo "ERROR: your initramfs-tools does not support prepend_earlyinitramfs." >&2
    exit 1
fi
echo "  [OK] initramfs-tools supports prepend_earlyinitramfs"

if [ "$(head -c4 "${TABLE}")" != "IVRS" ]; then
    echo "ERROR: ${TABLE} does not look like an IVRS table" >&2
    exit 1
fi
echo "  [OK] ${TABLE} has an IVRS signature"

# --- Build the cpio ---------------------------------------------------------

echo
echo "== Building the cpio =="
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/kernel/firmware/acpi"
cp "${TABLE}" "${WORK}/kernel/firmware/acpi/IVRS.aml"
# Fixed timestamp so the cpio is reproducible
find "${WORK}" -print0 | xargs -0r touch --no-dereference --date="@1000000000"

mkdir -p "${CPIO_DIR}"
( cd "${WORK}" && find . -print0 | LC_ALL=C sort -z \
    | cpio --null --reproducible -R 0:0 -H newc -o --quiet ) > "${CPIO_PATH}"

echo "  ${CPIO_PATH}"
cpio -itv < "${CPIO_PATH}" 2>/dev/null | sed 's/^/    /'

# --- Install the hook -------------------------------------------------------

echo
echo "== Installing the hook =="
install -m 0755 "${HERE}/hooks/acpi_ivrs_override" "${HOOK_PATH}"
echo "  ${HOOK_PATH}"

# --- Back up the initrd -----------------------------------------------------

echo
echo "== Backing up the current initrd =="
mkdir -p "${BACKUP_DIR}"
if [ -f "/boot/initrd.img-${KVER}" ]; then
    cp -n "/boot/initrd.img-${KVER}" "${BACKUP_DIR}/initrd.img-${KVER}.bak" \
        && echo "  ${BACKUP_DIR}/initrd.img-${KVER}.bak" \
        || echo "  a backup already existed, not overwriting"
else
    echo "  WARNING: /boot/initrd.img-${KVER} does not exist"
fi

# --- Regenerate -------------------------------------------------------------

echo
echo "== Regenerating initramfs for ${KVER} =="
update-initramfs -u -k "${KVER}"

# --- Verify -----------------------------------------------------------------

echo
echo "== Verifying the table travels inside the initrd =="
python3 "${HERE}/verify_initrd.py" "/boot/initrd.img-${KVER}" "${TABLE}"

cat <<EOF

===========================================================================
Installed. Reboot and check:

    dmesg | grep -i 'Table Upgrade'
        -> should print: override [IVRS-...]

    stat -c%s /sys/firmware/acpi/tables/IVRS
        -> should match the size of your patched table

    cat /sys/bus/pci/devices/<YOUR_DEVICE>/iommu_group/reserved_regions
        -> the 'direct' lines should be gone

If the system does not boot: pick an older kernel from the GRUB menu
(Advanced options), whose initrd has no override, and run ./uninstall.sh
===========================================================================
EOF
