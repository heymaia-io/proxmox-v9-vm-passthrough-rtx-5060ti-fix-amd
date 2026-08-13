# Arreglar el error "Firmware has requested this device have a 1:1 IOMMU mapping"

Herramientas para diagnosticar y resolver este fallo de passthrough PCIe en placas AMD, **sin recompilar el kernel**:

```
vfio-pci 0000:01:00.0: Firmware has requested this device have a 1:1 IOMMU mapping,
rejecting configuring the device without a 1:1 mapping. Contact your platform vendor.

TASK ERROR: start failed: QEMU exited with code 1
```

Probado en Proxmox VE 9.1.6, kernel 6.17, MSI PRO B850M-A WIFI (BIOS 2.A30, AGESA), pasando una NVIDIA RTX 5060 Ti a una VM. El método sirve para cualquier combinación de placa AMD y dispositivo PCIe que sufra lo mismo.

---

## El problema en corto

Algunas placas AMD declaran entradas **IVMD** en su tabla ACPI **IVRS** que cubren un **rango entero de device IDs**, no un dispositivo concreto. El kernel las traduce a reservas `IOMMU_RESV_DIRECT` y marca `require_direct=1` en todos los dispositivos del rango.

Cuando VFIO reclama uno de ellos para pasárselo a una VM, primero adjunta un *blocking domain*. El core del IOMMU ve `require_direct` y lo rechaza con `-EINVAL`, que es el mensaje de arriba.

**Consecuencia importante:** el dispositivo no tiene la culpa. En la máquina donde se desarrolló esto, el rango cubría los buses PCI `00` a `0f`, así que el NVMe, la tarjeta de red y los puentes PCIe estaban igual de bloqueados. Cambiar de GPU no habría servido de nada.

## La solución

Sustituir la tabla IVRS por una copia parcheada, inyectada vía **early-initramfs**, en la que los rangos IVMD se parten en dos para saltar el bus de tu dispositivo:

```
firmware:   IVMD 0x0000 .. 0x0fff
parcheada:  IVMD 0x0000 .. 0x00ff   +   IVMD 0x0200 .. 0x0fff
                              ^^^ el bus 01 queda en el hueco
```

No se borra ninguna reserva: el resto del sistema conserva exactamente la que pidió el firmware. Solo tu dispositivo queda fuera.

Frente a compilar un kernel parcheado, esto tarda segundos en vez de horas y **sobrevive a las actualizaciones de kernel**, porque vive como hook de `initramfs-tools` y se re-aplica en cada `update-initramfs`.

---

## Requisitos

| Requisito | Cómo comprobarlo |
|---|---|
| IOMMU de AMD (la tabla se llama IVRS) | `ls /sys/firmware/acpi/tables/IVRS` |
| `CONFIG_ACPI_TABLE_UPGRADE=y` | `grep ACPI_TABLE_UPGRADE /boot/config-$(uname -r)` |
| `initramfs-tools` >= 0.140 | `dpkg -l initramfs-tools` |
| **Secure Boot desactivado** | `cat /sys/kernel/security/lockdown` debe decir `[none]` |
| Python 3 | ya viene en Proxmox |

**Secure Boot es un bloqueo real, no una recomendación.** Bajo lockdown `integrity`, `acpi_table_upgrade()` se salta por completo y el override no se aplica. Hay que desactivarlo en el BIOS. Es el mismo requisito que tendría un kernel parcheado sin firmar.

---

## Uso

### 1. Diagnosticar

```bash
sudo ./check.sh 0000:01:00.0
```

Busca tu dispositivo en la salida. Si aparece con líneas `direct`, sigue:

```bash
sudo ./dump_ivrs.py --device 0000:01:00.0
```

Te dirá si cae dentro de alguna IVMD y qué bus hay que excluir. Si dice que no está cubierto, **tu problema es otro** y este repo no te sirve.

### 2. Parchear la tabla

```bash
sudo cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml
./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01
```

El bus sale de la dirección PCI: en `0000:01:00.0` es `01`. El script corre nueve verificaciones y **no escribe nada si alguna falla**.

### 3. Instalar

```bash
sudo ./install.sh IVRS.patched.aml
```

Comprueba los requisitos, empaqueta la tabla en un cpio, instala el hook, **guarda un backup del initrd actual en `./backup/`** y regenera el initramfs del kernel en uso.

> **Regenera un solo kernel a propósito.** Usar `update-initramfs -u -k all` aplicaría el override también a los kernels antiguos, que son justamente tu ruta de rollback si el sistema no arranca.

### 4. Reiniciar y verificar

```bash
dmesg | grep -i 'Table Upgrade'
#   -> ACPI: Table Upgrade: override [IVRS-...]

stat -c%s /sys/firmware/acpi/tables/IVRS
#   -> el tamaño de tu tabla parcheada, no el original

cat /sys/bus/pci/devices/0000:01:00.0/iommu_group/reserved_regions
#   -> las líneas 'direct' deben haber desaparecido
```

Si las tres salen bien, arranca la VM.

### Rollback

```bash
sudo ./uninstall.sh
sudo reboot
```

Si el sistema **no arranca**, elige un kernel anterior en el menú de GRUB (`Advanced options for ...`) — su initrd no lleva el override — y ejecuta el desinstalador desde ahí.

---

## Las dos trampas que te van a costar tiempo

### 1. `oem_revision` — el fallo silencioso

Esta es la parte que hace perder horas. El kernel **exige que la tabla nueva tenga un `oem_revision` estrictamente mayor** que la del firmware. De `drivers/acpi/tables.c`:

```c
if (test_and_set_bit(table_index, acpi_initrd_installed) ||
    existing_table->oem_revision >= table->oem_revision) {
        acpi_os_unmap_memory(table, ACPI_HEADER_SIZE);
        goto next_table;        /* descarte SILENCIOSO */
}
```

Si copias la cabecera tal cual, ese `>=` descarta tu tabla y **no se imprime ningún error ni warning**. El kernel incluso te dice que la encontró:

```
ACPI: IVRS ACPI table found in initrd [kernel/firmware/acpi/IVRS.aml][0x25e]
```

...y luego enumera tranquilamente la del firmware. La única forma de notarlo es comparar los tamaños: `0x25e` (606) es la tuya, `0x1fe` (510) la del firmware.

`patch_ivrs.py` incrementa `oem_revision` automáticamente.

Ojo también con los otros tres campos: `signature`, `oem_id` y `oem_table_id` **deben quedar idénticos**, porque son el criterio con el que el kernel empareja tu tabla con la que va a sustituir.

### 2. `cpio -it` te miente

El initramfs es una **concatenación** de varios archivos cpio: microcódigo, firmware, tu override, y por último el cpio principal comprimido. GNU `cpio` se detiene en el `TRAILER!!!` del primero.

```bash
cpio -it < /boot/initrd.img-$(uname -r)     # solo ves el microcódigo
```

Vas a concluir que tu tabla no se instaló cuando en realidad sí está. Usa:

```bash
./verify_initrd.py /boot/initrd.img-$(uname -r) IVRS.patched.aml
```

que recorre todos los cpio del prefijo y compara el hash.

---

## Qué hay en cada archivo

| Archivo | Para qué |
|---|---|
| `check.sh` | Diagnóstico de solo lectura: lockdown, config del kernel, qué dispositivos tienen reservas `direct` |
| `dump_ivrs.py` | Decodifica la tabla IVRS y te dice si un dispositivo cae dentro de una IVMD |
| `patch_ivrs.py` | Genera la tabla parcheada, con nueve verificaciones antes de escribir |
| `verify_initrd.py` | Comprueba que la tabla viaja dentro del initramfs (donde `cpio -it` falla) |
| `install.sh` | Empaqueta, instala el hook, hace backup del initrd y regenera |
| `uninstall.sh` | Rollback completo |
| `hooks/acpi_ivrs_override` | El hook de initramfs-tools; usa `prepend_earlyinitramfs` |
| `examples/` | Tablas real y parcheada de la máquina de referencia, para comparar |

---

## Mantenimiento

**Tras cada actualización de kernel**, comprueba:

```bash
dmesg | grep -i 'Table Upgrade'
```

El hook se ejecuta solo en cada `update-initramfs`, así que debería seguir funcionando. Pero si ese mensaje desaparece un día, el passthrough se cae — merece la pena tenerlo controlado.

**Si actualizas el BIOS**, hay que regenerar la tabla parcheada desde el nuevo volcado. El parche se construye a partir de la IVRS real de tu máquina, no de valores fijos:

```bash
sudo cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml
./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01
sudo ./install.sh IVRS.patched.aml
```

---

## Riesgos

Las reservas IVMD son rangos que el firmware pidió para todos los dispositivos. Al sacar el tuyo de esa lista, el IOMMU pasa a traducir normalmente esas direcciones **solo para él**.

En la máquina de referencia el riesgo era bajo: 12 KB en tres páginas, declaradas como rangos de exclusión con los bits `IR`/`IW` en 0 — algo ya de por sí anómalo. Pero es firmware, y el firmware a veces sabe algo que tú no. Vale la pena mirar qué direcciones son en tu caso (`dump_ivrs.py` te las enseña) y contrastarlas con `/proc/iomem`.

Es completamente reversible: `./uninstall.sh` y un reinicio.

**Secure Boot queda desactivado de forma permanente** mientras uses esto. Recuperarlo exigiría firmar el override con llaves propias (MOK), que es otro proyecto.

---

## Contexto útil si estás depurando esto

- El mensaje de error **no está en el código de VFIO**. `vfio_pci_core` es un módulo, y el string no aparece en ningún `.ko`; está compilado dentro de `vmlinux`. El guard vive en el core del IOMMU, en `__iommu_device_set_domain()` de `drivers/iommu/iommu.c`. Si buscas la línea a parchear en `drivers/vfio/`, no la vas a encontrar.
- `iommu=pt` **no ayuda**. El rechazo ocurre cuando VFIO adjunta el *blocking domain*, antes de que el tipo de dominio por defecto importe.
- Desactivar *Above 4G Decoding* o *Resizable BAR* tampoco ayuda: no tienen relación con las entradas IVMD.
- La señal fiable no es que la VM arranque, sino:
  ```bash
  cat /sys/bus/pci/devices/<DIR>/iommu_group/reserved_regions
  ```
  Si ya no hay líneas `direct`, el problema está resuelto a nivel de kernel.

---

## Licencia

Dominio público / CC0. Úsalo, cópialo y modifícalo sin restricciones.
