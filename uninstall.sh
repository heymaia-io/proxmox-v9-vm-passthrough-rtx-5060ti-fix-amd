#!/bin/bash
# uninstall.sh - Quita el override de tabla ACPI IVRS y deja el sistema como estaba.
#
#   sudo ./uninstall.sh [version-de-kernel]
#
# Si el sistema NO arranca, elige un kernel anterior en el menu de GRUB
# (Advanced options for ...) cuyo initrd no lleve el override, y ejecuta esto.

set -euo pipefail

KVER="${1:-$(uname -r)}"

CPIO_DIR=/usr/local/lib/acpi-override
HOOK_PATH=/etc/initramfs-tools/hooks/acpi_ivrs_override

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: hay que ejecutarlo como root" >&2
    exit 1
fi

echo "== Quitando el hook =="
if [ -e "${HOOK_PATH}" ]; then
    rm -f "${HOOK_PATH}"
    echo "  borrado ${HOOK_PATH}"
else
    echo "  ${HOOK_PATH} no existia"
fi

echo
echo "== Quitando el cpio =="
if [ -d "${CPIO_DIR}" ]; then
    rm -rf "${CPIO_DIR}"
    echo "  borrado ${CPIO_DIR}"
else
    echo "  ${CPIO_DIR} no existia"
fi

echo
echo "== Regenerando initramfs de ${KVER} =="
update-initramfs -u -k "${KVER}"

cat <<EOF

===========================================================================
Desinstalado. Reinicia para volver a la tabla IVRS del firmware.

Tras el reinicio, para confirmar que el override ya no se aplica:

    dmesg | grep -i 'Table Upgrade'      -> no debe imprimir nada

Si habias desactivado Secure Boot solo para esto, ya lo puedes reactivar
en el BIOS.
===========================================================================
EOF
