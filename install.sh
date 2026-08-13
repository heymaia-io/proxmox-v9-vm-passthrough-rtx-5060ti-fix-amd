#!/bin/bash
# install.sh - Instala el override de tabla ACPI IVRS.
#
# Empaqueta la tabla parcheada en un cpio, instala el hook de initramfs-tools y
# regenera el initramfs del kernel indicado (por defecto el que esta corriendo).
#
# Antes de regenerar, guarda una copia del initrd actual en ./backup/.
#
#   sudo ./install.sh IVRS.patched.aml [version-de-kernel]
#
# NOTA IMPORTANTE: se regenera SOLO un kernel a proposito. Usar `-k all` aplicaria
# el override tambien a los kernels antiguos, que son justamente tu ruta de
# rollback si el sistema no arranca.

set -euo pipefail

TABLE="${1:-}"
KVER="${2:-$(uname -r)}"

CPIO_DIR=/usr/local/lib/acpi-override
CPIO_PATH="${CPIO_DIR}/acpi_ivrs_override.cpio"
HOOK_PATH=/etc/initramfs-tools/hooks/acpi_ivrs_override
HERE="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${HERE}/backup"

if [ -z "${TABLE}" ]; then
    echo "uso: $0 <IVRS.patched.aml> [version-de-kernel]" >&2
    exit 1
fi
if [ ! -r "${TABLE}" ]; then
    echo "ERROR: no puedo leer ${TABLE}" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: hay que ejecutarlo como root" >&2
    exit 1
fi

# --- Comprobaciones previas -------------------------------------------------

echo "== Comprobaciones previas =="

if ! grep -q '^CONFIG_ACPI_TABLE_UPGRADE=y' "/boot/config-${KVER}" 2>/dev/null; then
    echo "ERROR: el kernel ${KVER} no tiene CONFIG_ACPI_TABLE_UPGRADE=y." >&2
    echo "       Este metodo no puede funcionar en el." >&2
    exit 1
fi
echo "  [OK] CONFIG_ACPI_TABLE_UPGRADE=y"

LOCKDOWN=$(cat /sys/kernel/security/lockdown 2>/dev/null || echo "")
if echo "${LOCKDOWN}" | grep -q '\[integrity\]\|\[confidentiality\]'; then
    echo "ERROR: el kernel esta en modo lockdown: ${LOCKDOWN}" >&2
    echo "       acpi_table_upgrade() se salta bajo lockdown." >&2
    echo "       Desactiva Secure Boot en el BIOS y vuelve a intentarlo." >&2
    exit 1
fi
echo "  [OK] lockdown: ${LOCKDOWN:-no expuesto}"

if ! grep -q 'prepend_earlyinitramfs' /usr/share/initramfs-tools/hook-functions 2>/dev/null; then
    echo "ERROR: tu initramfs-tools no soporta prepend_earlyinitramfs." >&2
    exit 1
fi
echo "  [OK] initramfs-tools soporta prepend_earlyinitramfs"

if [ "$(head -c4 "${TABLE}")" != "IVRS" ]; then
    echo "ERROR: ${TABLE} no parece una tabla IVRS" >&2
    exit 1
fi
echo "  [OK] ${TABLE} tiene firma IVRS"

# --- Construir el cpio ------------------------------------------------------

echo
echo "== Construyendo el cpio =="
WORK=$(mktemp -d)
trap 'rm -rf "${WORK}"' EXIT

mkdir -p "${WORK}/kernel/firmware/acpi"
cp "${TABLE}" "${WORK}/kernel/firmware/acpi/IVRS.aml"
# Timestamp fijo para que el cpio sea reproducible
find "${WORK}" -print0 | xargs -0r touch --no-dereference --date="@1000000000"

mkdir -p "${CPIO_DIR}"
( cd "${WORK}" && find . -print0 | LC_ALL=C sort -z \
    | cpio --null --reproducible -R 0:0 -H newc -o --quiet ) > "${CPIO_PATH}"

echo "  ${CPIO_PATH}"
cpio -itv < "${CPIO_PATH}" 2>/dev/null | sed 's/^/    /'

# --- Instalar el hook -------------------------------------------------------

echo
echo "== Instalando el hook =="
install -m 0755 "${HERE}/hooks/acpi_ivrs_override" "${HOOK_PATH}"
echo "  ${HOOK_PATH}"

# --- Backup del initrd ------------------------------------------------------

echo
echo "== Backup del initrd actual =="
mkdir -p "${BACKUP_DIR}"
if [ -f "/boot/initrd.img-${KVER}" ]; then
    cp -n "/boot/initrd.img-${KVER}" "${BACKUP_DIR}/initrd.img-${KVER}.bak" \
        && echo "  ${BACKUP_DIR}/initrd.img-${KVER}.bak" \
        || echo "  ya existia un backup, no se sobrescribe"
else
    echo "  AVISO: /boot/initrd.img-${KVER} no existe"
fi

# --- Regenerar --------------------------------------------------------------

echo
echo "== Regenerando initramfs de ${KVER} =="
update-initramfs -u -k "${KVER}"

# --- Verificar --------------------------------------------------------------

echo
echo "== Verificando que la tabla viaja dentro del initrd =="
python3 "${HERE}/verify_initrd.py" "/boot/initrd.img-${KVER}" "${TABLE}"

cat <<EOF

===========================================================================
Instalado. Reinicia y comprueba:

    dmesg | grep -i 'Table Upgrade'
        -> debe imprimir: override [IVRS-...]

    stat -c%s /sys/firmware/acpi/tables/IVRS
        -> debe coincidir con el tamano de tu tabla parcheada

    cat /sys/bus/pci/devices/<TU_DISPOSITIVO>/iommu_group/reserved_regions
        -> las lineas 'direct' deben haber desaparecido

Si el sistema no arranca: elige un kernel anterior en el menu de GRUB
(Advanced options), cuyo initrd no lleva el override, y ejecuta ./uninstall.sh
===========================================================================
EOF
