# Agent runbook

**For humans:** clone this repo onto the affected machine, then tell your AI agent:

> Read `PROMPT.md` in this repo and follow it to fix my PCIe passthrough error.

Everything below is written for the agent.

---

## ⚠️ Surface this to the user before you touch anything

This repo records what worked on **one specific machine** (Proxmox VE 9.1.6, kernel 6.17.13-21-pve on ZFS root, MSI PRO B850M-A WIFI with BIOS 2.A30, NVIDIA RTX 5060 Ti). It carries **no warranty and no guarantee**, even on identical hardware and versions.

The procedure rewrites an ACPI table and injects it at boot. Done wrong, it can leave a machine that **does not boot**, and recovery may need physical access.

Before you begin Phase 4 — the first change to the system — make sure the user has explicitly confirmed they understand and accept that. Specifically, confirm they have:

- Working backups they have actually tested
- A second, genuinely clean kernel in the boot menu (Phase 2b)
- A way in if the machine will not boot: physical console, IPMI/iDRAC/iLO, or rescue media

If the user cannot confirm those, **say so and stop**. Offer the alternative: running the workload directly on the host without passthrough avoids this entire class of problem. Do not push forward because the fix is technically available.

---

## Your mission

The user cannot pass a PCIe device through to a virtual machine. The host logs:

```
vfio-pci 0000:XX:00.0: Firmware has requested this device have a 1:1 IOMMU mapping,
rejecting configuring the device without a 1:1 mapping. Contact your platform vendor.
```

Your job is to determine whether this specific root cause applies, and if so, fix it by replacing the firmware's ACPI IVRS table with a patched copy injected via early-initramfs.

**This fix only applies to one specific root cause.** A large part of your job is deciding whether that cause is present. If it is not, say so plainly and stop — do not improvise a different fix.

---

## Hard rules

1. **Read-only until the user authorises writes.** Phases 0-2 modify nothing. Do not install, edit or delete anything before Phase 4, and only after the user explicitly agrees.
2. **Never skip a verification gate.** Each phase ends with a check. If it fails, stop and report — do not proceed hoping the next step compensates.
3. **Back up before overwriting.** The only system file this changes is one initrd. Copy it first.
4. **Never run `update-initramfs -u -k all`.** It would apply the override to every installed kernel, destroying the rollback path. Always target one kernel version explicitly.
5. **Two reboots are required**, and only the user can perform them (one also requires a BIOS change). Plan for handing control back.
6. **Do not touch VM configuration** until the kernel-level fix is verified. Conflating the two makes failures impossible to attribute.
7. **Report honestly.** If a check fails, say it failed. Do not describe the fix as working based on the absence of errors.

---

## Phase 0 — Gather facts (read-only)

```bash
uname -r
cat /proc/cmdline
lspci -nnk                                     # locate the device; note its PCI address
dmesg | grep -i '1:1 IOMMU mapping'
cat /sys/kernel/security/lockdown
grep ACPI_TABLE_UPGRADE /boot/config-$(uname -r)
```

Or just run the bundled script, which collects all of it:

```bash
sudo ./check.sh 0000:01:00.0        # substitute the user's device address
```

Record the device's PCI address. Everything downstream depends on it. In `0000:01:00.0` the **bus** is `01`.

---

## Phase 1 — Confirm this is the right bug (decision gate)

### 1a. Does the device carry `direct` reservations?

```bash
cat /sys/bus/pci/devices/0000:01:00.0/iommu_group/reserved_regions
```

- Lines ending in **`direct`** → continue.
- Only `msi` and `reserved` lines → **STOP.** This root cause is absent. Tell the user the error must come from somewhere else and do not apply this fix.

### 1b. Is the reservation range-wide rather than device-specific?

This is the step that distinguishes this bug from a genuine device-specific quirk.

```bash
for d in /sys/bus/pci/devices/*; do
    n=$(grep -c direct "$d/iommu_group/reserved_regions" 2>/dev/null)
    [ "${n:-0}" -gt 0 ] && echo "$(basename $d) direct=$n"
done
```

- **Several unrelated devices** (NVMe, NIC, PCIe bridges) share the same reservations → this is the range-wide firmware bug. Continue.
- **Only the target device** is listed → the reservation is genuinely device-specific. This fix may still work, but the risk profile is different: you would be removing a reservation the firmware made specifically for that device. Explain this to the user and let them decide.

### 1c. Confirm it in the ACPI table

```bash
sudo ./dump_ivrs.py --device 0000:01:00.0
```

Expect IVMD entries of **type `0x22` (DEVICE RANGE)** whose device-ID range contains your device, and the line:

```
Bus to exclude with patch_ivrs.py: --bus 01
```

If the script reports the device is not covered by any IVMD, **STOP** — the `direct` reservations come from somewhere other than IVRS and this repo does not address that.

### Report to the user before continuing

State plainly: which IVMD ranges exist, which devices they cover, and that the target device falls inside one. Make the point explicitly that **the device itself is not at fault** — this changes how they think about the problem, and rules out "buy a different card" as a fix.

---

## Phase 2 — Check prerequisites (read-only)

| Requirement | Command | Required value |
|---|---|---|
| ACPI override support | `grep ACPI_TABLE_UPGRADE /boot/config-$(uname -r)` | `CONFIG_ACPI_TABLE_UPGRADE=y` |
| Kernel not locked down | `cat /sys/kernel/security/lockdown` | `[none]` |
| initramfs-tools support | `grep prepend_earlyinitramfs /usr/share/initramfs-tools/hook-functions` | found |
| A second kernel installed | `ls /boot/vmlinuz-*` | at least two |

**If lockdown shows `[integrity]` or `[confidentiality]`:** Secure Boot is on and `acpi_table_upgrade()` will be skipped entirely. The override would install and silently do nothing.

Stop here and ask the user to disable Secure Boot in the BIOS. Tell them:

- It is a hard prerequisite, not a preference.
- It will stay off for as long as they use this fix.
- A patched unsigned kernel — the alternative fix circulating in forums — carries the same requirement, so this is not a downside unique to this approach.
- On most boards: `Settings → Advanced → Windows OS Configuration → Secure Boot → Disabled`.

They must reboot before you can continue. When they return, re-check lockdown before doing anything else.

### 2b. Make sure a fallback kernel exists — before Phase 4

The whole rollback strategy rests on having a second kernel in the GRUB menu whose initrd carries **no** override. If the machine has only ever run one kernel, that escape hatch does not exist and you must create it first.

```bash
ls /boot/vmlinuz-*                          # generic
proxmox-boot-tool kernel list               # Proxmox
dpkg -l 'proxmox-kernel-6*' | awk '/^ii/{print $2}'
```

If only one kernel is present, ask the user to install a second one.

<!-- ordering matters more than it looks -->
> **Install it before Phase 4, never after.** Installing a kernel runs `update-initramfs` for it, which executes every hook — including this one, once installed. A fallback kernel added *after* the hook would carry the override too, and would be useless as a rollback path. Order: install the fallback kernel → then install the override.
>
> If the user already installed the hook and only then realises they need a fallback, they must regenerate that kernel's initrd with the hook temporarily removed, or the fallback is contaminated.

#### Which kernel to install

**Stay inside the same upstream series as the running kernel.** If they are on `6.17.x`, the fallback must also be `6.17.x` — not `6.14.x`, not `6.10.x`.

Jumping to an older branch looks like a safe move and is not. Out-of-tree modules the system may depend on to boot at all — ZFS above all, plus any DKMS drivers — are built against a specific kernel ABI. On a ZFS-on-root host, a fallback kernel whose ZFS module does not load leaves the machine unable to mount its root pool, which is a far worse position than the bug you are fixing.

Within that series, pick by where they currently sit:

| Situation | Install |
|---|---|
| Running the newest release in the series | The **previous** release in the same series |
| Running an older release in the series | The **newest** release in the same series |

Either way you end up with two neighbouring versions, one of which is known to boot.

```bash
# what exists in the same series (adjust 6.17 to match)
apt-cache search '^proxmox-kernel-6\.17\.[0-9]' | sort     # Proxmox
apt-cache search '^linux-image-6\.17\.[0-9]'   | sort     # Debian/Ubuntu

# install a specific one — on Proxmox prefer the signed variant
apt install proxmox-kernel-6.17.13-2-pve-signed
```

Then confirm it is really there and bootable:

```bash
ls /boot/vmlinuz-* /boot/initrd.img-*
proxmox-boot-tool kernel list
```

**Never uninstall the old kernel** to tidy up. It is the safety net.

Ideally the user reboots once into the newly installed kernel to prove it boots, then reboots back. This is optional, but a fallback that has never been booted is an assumption, not a guarantee.

---

## Phase 3 — Build the patched table (writes only to the repo directory)

```bash
sudo cp /sys/firmware/acpi/tables/IVRS IVRS.original.aml
./patch_ivrs.py IVRS.original.aml IVRS.patched.aml --bus 01
```

**Gate:** all nine checks must print `[OK  ]`. The script refuses to write the file otherwise. Do not hand-edit the table to work around a failure.

Show the user the before/after IVMD ranges so they can see the split is surgical — their other hardware keeps its reservations.

---

## Phase 4 — Install (first system modification — requires authorisation)

Ask the user before running this. Tell them exactly what changes:

- **New:** `/usr/local/lib/acpi-override/acpi_ivrs_override.cpio`
- **New:** `/etc/initramfs-tools/hooks/acpi_ivrs_override`
- **Modified:** `/boot/initrd.img-<version>` — backed up first to `./backup/`

```bash
sudo ./install.sh IVRS.patched.aml
```

The script re-checks prerequisites, backs up the initrd, regenerates a single kernel and verifies the result.

**Gate:** the final verification must report `MATCH : True`.

If you install manually instead of using the script, verify with:

```bash
./verify_initrd.py /boot/initrd.img-$(uname -r) IVRS.patched.aml
```

Never use `cpio -it` for this — see Trap 2 below.

---

## Phase 5 — Reboot and verify

The user must reboot. Ask them to shut down VMs first.

After they return, run these three checks **in order**. Each one narrows down where a failure occurred:

```bash
# 1. Did the kernel apply the override?
dmesg | grep -i 'Table Upgrade'
#    expected: ACPI: Table Upgrade: override [IVRS-...]

# 2. Is the active table the patched one?
stat -c%s /sys/firmware/acpi/tables/IVRS
#    expected: the size of IVRS.patched.aml, not the original

# 3. Are the reservations gone from the target device?
cat /sys/bus/pci/devices/0000:01:00.0/iommu_group/reserved_regions
#    expected: no 'direct' lines

# 4. Do other devices still have theirs? (confirms the cut was surgical)
cat /sys/bus/pci/devices/0000:02:00.0/iommu_group/reserved_regions
#    expected: 'direct' lines still present
```

### Interpreting failures

| Symptom | Meaning | Action |
|---|---|---|
| No `Table Upgrade` line, but `found in initrd` is present | The table was found and rejected — almost always `oem_revision`. See Trap 1. | Rebuild the table with a bumped revision |
| No `Table Upgrade` **and** no `found in initrd` | The cpio never made it into the initramfs | Re-run `verify_initrd.py`; check the hook is executable |
| `Table Upgrade` present but `direct` lines remain | The override applied but the reservations come from elsewhere | Your Phase 1 diagnosis was wrong. Say so and stop |
| Other devices lost their reservations too | The bus split was wrong | Roll back and re-check the `--bus` argument |

Also run a health check before declaring success — storage pools, network, other VMs:

```bash
zpool status -x 2>/dev/null; ip -br addr; dmesg -l err,warn | tail -20
```

---

## Phase 6 — Hand back to the user

Do **not** configure the VM yourself. Tell the user to attach the device, and explain the options that matter:

- Include **all functions** of the device — everything in the same IOMMU group must be passed together (a GPU's audio function, for example).
- Enable **PCI-Express** if the VM machine type is `q35`.
- Leave **Primary GPU** (`x-vga=1`) **off** unless the guest genuinely drives a physical display. It is unnecessary for compute workloads and complicates host console access.

Once the VM boots, the user should confirm from inside the guest that the device is visible (`lspci`). That is the real end-to-end proof.

---

## Rollback

If anything goes wrong:

```bash
sudo ./uninstall.sh
sudo reboot
```

**If the system does not boot:** tell the user to pick an older kernel in the GRUB menu (`Advanced options for ...`). Its initrd carries no override. Once booted, run the uninstaller.

This is why Rule 4 exists. If you had regenerated all kernels, this escape hatch would not exist.

---

## The two traps

### Trap 1 — `oem_revision`, the silent discard

The kernel requires the replacement table's `oem_revision` to be **strictly greater** than the firmware's. From `drivers/acpi/tables.c`:

```c
if (test_and_set_bit(table_index, acpi_initrd_installed) ||
    existing_table->oem_revision >= table->oem_revision) {
        acpi_os_unmap_memory(table, ACPI_HEADER_SIZE);
        goto next_table;        /* silent */
}
```

There is **no error message**. The kernel prints that it found your table, then enumerates the firmware's one anyway. The only tell is the size difference in `dmesg`:

```
ACPI: IVRS ACPI table found in initrd [...][0x25e]   <- yours, 606 bytes
ACPI: IVRS 0x0000000098356000 0001FE (...)           <- firmware's, 510 bytes
```

`patch_ivrs.py` handles this. If you build a table by other means, bump the field at offset 24 (u32) and recompute the checksum at offset 9.

Conversely, `signature`, `oem_id` and `oem_table_id` **must remain identical** — they are how the kernel matches your table to the one it replaces.

### Trap 2 — `cpio -it` does not show the override

The initramfs is a concatenation of several cpio archives followed by the main compressed one. GNU `cpio` stops at the first `TRAILER!!!`, so you will only ever see the microcode and conclude the override is missing.

Always use `verify_initrd.py`, which walks every archive in the prefix.

---

## When to stop and say "this is not your problem"

Be willing to conclude the fix does not apply. Stop and report if:

- The target device has no `direct` reservations (Phase 1a)
- `dump_ivrs.py` finds no IVMD entry covering the device (Phase 1c)
- There is no IVRS table at all — the machine is likely Intel, where the equivalent table is DMAR and this repo does not handle it
- The override applies cleanly but `direct` lines persist (Phase 5)

In each case, state what you checked, what you found, and why this fix does not address it. A clear negative result is a useful outcome; a fix applied on a wrong diagnosis is not.

---

## Things that do not help (do not suggest them)

| Suggestion often found in forums | Why it fails |
|---|---|
| Patch `drivers/vfio/pci/vfio_pci_core.c` | The string is not there. `vfio_pci_core` is a module and the message is built into `vmlinux`; the guard is in `__iommu_device_set_domain()` in `drivers/iommu/iommu.c`. Verify with `grep -rl "1:1 IOMMU mapping" /lib/modules/$(uname -r)/kernel/drivers/` |
| Add or remove `iommu=pt` | The rejection happens when VFIO attaches the blocking domain, before the default domain type is relevant |
| Disable Above 4G Decoding / Resizable BAR | Unrelated to IVMD entries |
| Pass only one function of the device | The block is by device ID, not by function |
| Swap in a different GPU | The reservation covers a device-ID range, not a specific card |
| Update or downgrade the BIOS | Worth checking the changelog, but the entries are usually unchanged across versions. Downgrades risk AGESA anti-rollback |
