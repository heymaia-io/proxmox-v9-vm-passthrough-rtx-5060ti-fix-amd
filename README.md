# Fixing "Firmware has requested this device have a 1:1 IOMMU mapping"

Tools to diagnose and fix this PCIe passthrough failure on AMD boards, **without recompiling the kernel**:

```
vfio-pci 0000:01:00.0: Firmware has requested this device have a 1:1 IOMMU mapping,
rejecting configuring the device without a 1:1 mapping. Contact your platform vendor.

TASK ERROR: start failed: QEMU exited with code 1
```

Tested on Proxmox VE 9.1.6, kernel 6.17, MSI PRO B850M-A WIFI (BIOS 2.A30, AGESA), passing an NVIDIA RTX 5060 Ti through to a VM. The method applies to any AMD board and PCIe device hitting the same wall.

---

## The problem in short

Some AMD boards declare **IVMD** entries in their ACPI **IVRS** table that cover an **entire range of device IDs**, not a specific device. The kernel turns those into `IOMMU_RESV_DIRECT` reservations and sets `require_direct=1` on every device in the range.

When VFIO claims one of them for a VM, it first attaches a *blocking domain*. The IOMMU core sees `require_direct` and rejects it with `-EINVAL`, which is the message above.

**Important consequence: the device is not at fault.** On the machine this was developed against, the range covered PCI buses `00` through `0f`, so the NVMe drive, the network card and the PCIe bridges were equally blocked. Swapping the GPU would have changed nothing.

## The fix

Replace the IVRS table with a patched copy, injected via **early-initramfs**, in which the IVMD ranges are split in two so they skip your device's bus:

```
firmware:  IVMD 0x0000 .. 0x0fff
patched:   IVMD 0x0000 .. 0x00ff   +   IVMD 0x0200 .. 0x0fff
                             ^^^ bus 01 falls in the gap
```

No reservation is deleted: the rest of the system keeps exactly what the firmware asked for. Only your device is carved out.

Compared to compiling a patched kernel, this takes seconds instead of hours and **survives kernel updates**, because it lives as an `initramfs-tools` hook and is re-applied on every `update-initramfs`.

---

## Requirements

| Requirement | How to check |
|---|---|
| AMD IOMMU (the table is called IVRS) | `ls /sys/firmware/acpi/tables/IVRS` |
| `CONFIG_ACPI_TABLE_UPGRADE=y` | `grep ACPI_TABLE_UPGRADE /boot/config-$(uname -r)` |
| `initramfs-tools` >= 0.140 | `dpkg -l initramfs-tools` |
| **Secure Boot disabled** | `cat /sys/kernel/security/lockdown` must say `[none]` |
| Python 3 | already present on Proxmox |

**Secure Boot is a hard blocker, not a recommendation.** Under `integrity` lockdown, `acpi_table_upgrade()` is skipped entirely and the override does nothing. You have to disable it in the BIOS. An unsigned patched kernel would carry the same requirement.

---

## Usage

### 1. Diagnose

```bash
sudo ./check.sh 0000:01:00.0
```

Find your device in the output. If it shows `direct` lines, continue:

```bash
sudo ./dump_ivrs.py --device 0000:01:00.0
```

It will tell you whether the device falls inside an IVMD entry and which bus to exclude. If it says the device is not covered, **your problem is something else** and this repo will not help.

### 2. Patch the table

```bash
sudo cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml
./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01
```

The bus comes from the PCI address: in `0000:01:00.0` it is `01`. The script runs nine checks and **writes nothing if any of them fails**.

### 3. Install

```bash
sudo ./install.sh IVRS.patched.aml
```

Verifies the requirements, packs the table into a cpio archive, installs the hook, **backs up the current initrd under `./backup/`** and regenerates the initramfs for the running kernel.

> **It regenerates a single kernel on purpose.** Using `update-initramfs -u -k all` would apply the override to your older kernels too, and those are exactly your rollback path if the system fails to boot.

### 4. Reboot and verify

```bash
dmesg | grep -i 'Table Upgrade'
#   -> ACPI: Table Upgrade: override [IVRS-...]

stat -c%s /sys/firmware/acpi/tables/IVRS
#   -> the size of your patched table, not the original

cat /sys/bus/pci/devices/0000:01:00.0/iommu_group/reserved_regions
#   -> the 'direct' lines should be gone
```

If all three look right, start the VM.

### Rollback

```bash
sudo ./uninstall.sh
sudo reboot
```

If the system **does not boot**, pick an older kernel from the GRUB menu (`Advanced options for ...`) — its initrd carries no override — and run the uninstaller from there.

---

## The two traps that will cost you time

### 1. `oem_revision` — the silent failure

This is the one that burns hours. The kernel **requires the new table's `oem_revision` to be strictly greater** than the firmware's. From `drivers/acpi/tables.c`:

```c
if (test_and_set_bit(table_index, acpi_initrd_installed) ||
    existing_table->oem_revision >= table->oem_revision) {
        acpi_os_unmap_memory(table, ACPI_HEADER_SIZE);
        goto next_table;        /* SILENT discard */
}
```

If you copy the header verbatim, that `>=` discards your table and **no error or warning is printed**. The kernel even tells you it found the file:

```
ACPI: IVRS ACPI table found in initrd [kernel/firmware/acpi/IVRS.aml][0x25e]
```

...and then happily enumerates the firmware's one. The only way to notice is comparing sizes: `0x25e` (606) is yours, `0x1fe` (510) is the firmware's.

`patch_ivrs.py` bumps `oem_revision` automatically.

Watch out for the opposite too: `signature`, `oem_id` and `oem_table_id` **must stay identical**, because they are the criteria the kernel uses to match your table against the one it is replacing.

### 2. `cpio -it` lies to you

The initramfs is a **concatenation** of several cpio archives: microcode, firmware, your override, and finally the main compressed one. GNU `cpio` stops at the first `TRAILER!!!`.

```bash
cpio -it < /boot/initrd.img-$(uname -r)     # you only see the microcode
```

You will conclude your table was not installed when in fact it is right there. Use:

```bash
./verify_initrd.py /boot/initrd.img-$(uname -r) IVRS.patched.aml
```

which walks every cpio archive in the prefix and compares hashes.

---

## What each file does

| File | Purpose |
|---|---|
| `check.sh` | Read-only diagnostics: lockdown, kernel config, which devices carry `direct` reservations |
| `dump_ivrs.py` | Decodes the IVRS table and tells you whether a device falls inside an IVMD entry |
| `patch_ivrs.py` | Generates the patched table, with nine checks before writing anything |
| `verify_initrd.py` | Confirms the table travels inside the initramfs, where `cpio -it` fails |
| `install.sh` | Packs, installs the hook, backs up the initrd and regenerates |
| `uninstall.sh` | Full rollback |
| `hooks/acpi_ivrs_override` | The initramfs-tools hook; uses `prepend_earlyinitramfs` |
| `examples/` | Real and patched tables from the reference machine, for comparison |

---

## Maintenance

**After every kernel update**, check:

```bash
dmesg | grep -i 'Table Upgrade'
```

The hook runs automatically on every `update-initramfs`, so it should keep working. But if that message ever disappears, passthrough breaks — worth keeping an eye on.

**If you update the BIOS**, regenerate the patched table from the new dump. The patch is built from your machine's actual IVRS table, not from hardcoded values:

```bash
sudo cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml
./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01
sudo ./install.sh IVRS.patched.aml
```

---

## Risks

IVMD reservations are ranges the firmware requested for every device. Carving yours out means the IOMMU will translate those addresses normally **for that device only**.

On the reference machine the risk was low: 12 KB across three pages, declared as exclusion ranges with the `IR`/`IW` bits cleared — already odd in itself. But this is firmware, and firmware sometimes knows something you do not. Look at which addresses are involved in your case (`dump_ivrs.py` shows them) and cross-check them against `/proc/iomem`.

It is fully reversible: `./uninstall.sh` and a reboot.

**Secure Boot stays disabled** for as long as you use this. Getting it back would mean signing the override with your own keys (MOK), which is a separate project.

---

## Useful context if you are debugging this

- The error message is **not in VFIO's code**. `vfio_pci_core` is a module, and the string does not appear in any `.ko`; it is built into `vmlinux`. The guard lives in the IOMMU core, in `__iommu_device_set_domain()` in `drivers/iommu/iommu.c`. If you go looking for the line to patch under `drivers/vfio/`, you will not find it.
- `iommu=pt` **does not help**. The rejection happens when VFIO attaches the *blocking domain*, before the default domain type matters.
- Disabling *Above 4G Decoding* or *Resizable BAR* does not help either: they have nothing to do with IVMD entries.
- The reliable signal is not whether the VM boots, but:
  ```bash
  cat /sys/bus/pci/devices/<ADDR>/iommu_group/reserved_regions
  ```
  If the `direct` lines are gone, the problem is solved at the kernel level.

---

## License

Public domain / CC0. Use, copy and modify without restriction.
