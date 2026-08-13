> ## ⚠️ Read this first — use at your own risk
>
> **This is not a product, a supported tool, or professional advice. It is a written record of the steps that worked on one specific machine.**
>
> Everything here was developed against, and only ever verified on, this exact combination:
>
> | | |
> |---|---|
> | Hypervisor | Proxmox VE 9.1.6 |
> | Kernel | 6.17.13-21-pve (ZFS on root) |
> | Motherboard | MSI PRO B850M-A WIFI (MS-7E66) |
> | BIOS | 2.A30, dated 2026-01-21 |
> | Device passed through | NVIDIA RTX 5060 Ti, GB206 — `10de:2d04` |
> | Exact error | `vfio-pci: Firmware has requested this device have a 1:1 IOMMU mapping` |
>
> **It worked there. That is the entire extent of what is being claimed.**
>
> This procedure rewrites an ACPI table your firmware publishes and injects it at boot. That is deep in the path your machine uses to come up. A mistake — a wrong bus number, a bad table, a fallback kernel that was not really clean — can leave a system that **does not boot**, and recovering may require physical access to the machine.
>
> **No warranty of any kind is given, and no responsibility is accepted for any outcome.** That includes systems that fail to boot, data loss, hardware misbehaviour, or anything else — whether your setup differs from the above **or matches it exactly**. Identical versions and identical hardware are still no guarantee: firmware revisions, BIOS settings, installed modules and boot configuration all vary in ways this document cannot account for.
>
> If you use any of this, you are the one deciding to. Before you start:
>
> - **Understand each step before running it.** Do not paste commands you cannot explain.
> - **Have working backups**, and confirm you can actually restore them.
> - **Make sure you have a rollback path** — a second, genuinely clean kernel in your boot menu — and know how to reach it.
> - **Have a way in if it does not boot**: physical console, IPMI/iDRAC/iLO, or a rescue USB.
> - **Do not try this first on something you cannot afford to lose.**
>
> If any of that sounds like more than you want to take on, that is a completely reasonable conclusion. Running the workload on the host without passthrough is often the saner trade.

---

# Fixing "Firmware has requested this device have a 1:1 IOMMU mapping"

Tools to diagnose and fix this PCIe passthrough failure on AMD boards, **without recompiling the kernel**:

```
vfio-pci 0000:01:00.0: Firmware has requested this device have a 1:1 IOMMU mapping,
rejecting configuring the device without a 1:1 mapping. Contact your platform vendor.

TASK ERROR: start failed: QEMU exited with code 1
```

Tested on Proxmox VE 9.1.6, kernel 6.17, MSI PRO B850M-A WIFI (BIOS 2.A30, AGESA), passing an NVIDIA RTX 5060 Ti through to a VM. The method applies to any AMD board and PCIe device hitting the same wall.

---

## Glossary

This failure sits at the intersection of ACPI, the IOMMU and VFIO, so the terminology piles up fast. Here is everything you need to follow the rest of this README.

| Term | What it means |
|---|---|
| **Passthrough** | Handing a physical PCIe device directly to a virtual machine, so the guest drives the real hardware instead of an emulated one. |
| **IOMMU** | The chip that translates memory addresses for devices doing DMA. It is what makes passthrough safe: a device can only reach memory the IOMMU lets it reach. AMD calls its implementation AMD-Vi. |
| **VFIO** | The Linux framework that hands a device to userspace (QEMU) for passthrough. `vfio-pci` is the driver that must claim your device instead of the normal one. |
| **ACPI** | The firmware-to-OS interface. The BIOS publishes a set of tables describing the hardware; the kernel reads them at boot. |
| **IVRS** | *I/O Virtualization Reporting Structure*. The ACPI table where AMD firmware describes the IOMMU, including any memory it wants reserved. This is the table we patch. |
| **IVMD** | *I/O Virtualization Memory Definition*. An entry inside IVRS reserving a memory region. Type `0x22` entries apply to a **range of devices**, which is the heart of this bug. |
| **Device ID / BDF** | A 16-bit number identifying a PCIe device, derived from its `bus:device.function` address. `01:00.0` is device ID `0x0100`. IVMD ranges are expressed in these. |
| **`direct` reservation** | An `IOMMU_RESV_DIRECT` region: memory the firmware wants identity-mapped (1:1) for a device. Visible per device under `/sys/bus/pci/devices/<addr>/iommu_group/reserved_regions`. |
| **`require_direct`** | The kernel flag set on any device carrying a `direct` reservation. It is what ultimately makes VFIO refuse the device. |
| **Blocking domain** | An IOMMU domain that blocks all DMA. VFIO attaches one while taking ownership of a device — and that is the exact moment the `require_direct` check fires and rejects it. |
| **IOMMU group** | The smallest set of devices the IOMMU can isolate independently. Everything in a group must be passed through together, which is why a GPU's audio function comes along for the ride. |
| **initramfs / early-initramfs** | The small filesystem the kernel loads before the real root. Its uncompressed prefix (the "early" part) can carry firmware, microcode — and replacement ACPI tables. |
| **`oem_revision`** | A version number in every ACPI table header. The kernel only accepts a replacement table whose value is **higher** than the firmware's. Get this wrong and your override is discarded silently. |
| **Lockdown / Secure Boot** | A kernel mode that blocks operations which could inject code into the kernel — including ACPI table overrides. Must be off for this fix to apply. |

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
| **A second kernel installed** | `ls /boot/vmlinuz-*` — see below |
| Python 3 | already present on Proxmox |

**Secure Boot is a hard blocker, not a recommendation.** Under `integrity` lockdown, `acpi_table_upgrade()` is skipped entirely and the override does nothing. You have to disable it in the BIOS. An unsigned patched kernel would carry the same requirement.

### About that second kernel

The rollback plan is "boot the other kernel from the GRUB menu", so you need one whose initrd carries no override. Many hosts already have two because of a past upgrade. If yours only has one, install a second **before** installing the override:

> Installing a kernel runs `update-initramfs` for it, which executes every hook — including this one. A fallback kernel added *after* the override would carry the override too, and would be worthless as a rollback path.

**Stay inside the same upstream series.** On `6.17.x`, install another `6.17.x` — not `6.14.x`, not `6.10.x`. Dropping to an older branch looks safer and is not: out-of-tree modules you may need to boot at all (ZFS above all, plus any DKMS drivers) are built against a specific kernel ABI. On a ZFS-on-root host, a fallback whose ZFS module will not load leaves you unable to mount your root pool — a worse place than the bug you are fixing.

Within the series: if you are on the newest release, install the previous one; if you are on an older one, install the newest.

```bash
apt-cache search '^proxmox-kernel-6\.17\.[0-9]' | sort   # see what exists
apt install proxmox-kernel-6.17.13-2-pve-signed          # pick a neighbour
```

Never uninstall the old kernel to tidy up — it is the safety net.

---

## Usage

### Let an AI agent do it

This repo ships [`PROMPT.md`](PROMPT.md), a complete runbook written for AI coding agents (Claude Code, Cursor, Copilot CLI, Codex, or anything else with shell access). Clone the repo onto the affected machine and say:

> Read `PROMPT.md` in this repo and follow it to fix my PCIe passthrough error.

The runbook makes the agent work read-only until you authorise changes, forces it through a decision gate that confirms this root cause actually applies before touching anything, backs up what it modifies, and — importantly — tells it when to stop and conclude the fix does **not** apply to your case.

Prefer to drive it yourself? Everything below is the same procedure by hand.

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
| `PROMPT.md` | Runbook for AI agents: decision gates, safety rules, verification steps, and when to conclude this fix does not apply |
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
