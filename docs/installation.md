# Installation

## Supported hosts

OpenQVM is designed for Windows 10/11, modern Linux distributions and macOS. The
current release has been executed on **Windows 11/AMD64** with QEMU 11.1 (WHPX and
TCG), including a real Alpine Linux boot. The Linux/KVM path is the intended
primary deployment target, but it was not executed in the development environment
used for this release. macOS is untested.

## Common requirements

- Python 3.11 or newer (3.13 tested)
- QEMU 8 or newer:
  - `qemu-system-x86_64`
  - `qemu-img`
- A desktop session for the GUI (the CLI and tests do not need one)
- Legal installation media for the guest operating system. OpenQVM does not
  download, bundle or authorize operating-system images.

The application never requires administrator/root rights. Hardware acceleration
may require enabling a host feature or joining the `kvm` group.

## Windows

1. Install QEMU:

   ```powershell
   winget install SoftwareFreedomConservancy.QEMU
   ```

   The default installation is `C:\Program Files\qemu`. OpenQVM finds it
   automatically, or you can set the directory in **Settings → Virtualization
   backend**.

2. Enable the Windows Hypervisor Platform (recommended):

   ```powershell
   # Run in an elevated PowerShell, then reboot
   Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All
   ```

   If WHPX is unavailable, OpenQVM falls back to TCG (software emulation) and
   reports it. WHPX and Hyper-V can coexist; VirtualBox and other hypervisors may
   conflict.

3. Create an environment and install:

   ```powershell
   py -3.13 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install --upgrade pip
   pip install -e ".[dev]"
   ```

4. Verify the backend:

   ```powershell
   openqvm-cli doctor --probe
   ```

   The probe starts QEMU briefly and stops it. `whpx: available` means hardware
   acceleration is usable.

5. Start the GUI:

   ```powershell
   openqvm
   ```

The Windows QEMU installer also provides UEFI firmware under `share` (edk2
`x86_64` code plus the i386/OVMF variable template). OpenQVM detects and uses it
for UEFI VMs.

## Linux

### Debian/Ubuntu

```bash
sudo apt update
sudo apt install qemu-system-x86 qemu-utils ovmf python3-venv \
                 libgl1 libegl1 libxkbcommon-x11-0 libxcb-cursor0
sudo usermod -aG kvm "$USER"
# Log out and back in so the group membership is applied
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
openqvm-cli doctor --probe
openqvm
```

### Fedora

```bash
sudo dnf install qemu-system-x86 qemu-img edk2-ovmf python3-virtualenv \
                 mesa-libGL libxkbcommon-x11
sudo usermod -aG kvm "$USER"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
openqvm-cli doctor --probe
```

If `/dev/kvm` is not writable, the VM starts with TCG but hardware acceleration is
unavailable. The diagnostic message tells you which group or kernel module needs
attention.

Bridged networking additionally requires a configured host bridge and
`qemu-bridge-helper`; OpenQVM detects the helper but does not modify host network
configuration. Host-only networking is not implemented.

## macOS

```bash
brew install qemu python@3.13
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
openqvm-cli doctor --probe
openqvm
```

On Apple Silicon, QEMU's `qemu-system-x86_64` runs x86_64 guests with TCG, which is
slow; HVF accelerates ARM guests only. OpenQVM currently targets x86_64 guests and
reports the host architecture in `doctor`. Apple Silicon native guest support is
on the roadmap. Bridged/host-only networking and shared folders are not available
on macOS in this release.

## Installation media

Download an ISO from the operating system vendor, for example:

- Alpine Linux: <https://alpinelinux.org/downloads/> (`virtual` or `standard`)
- Ubuntu: <https://ubuntu.com/download/desktop>
- Fedora: <https://fedoraproject.org/workstation/download/>
- Debian: <https://www.debian.org/download>

OpenQVM validates the file extension, size and ISO-9660 volume descriptor before
using it, but it cannot guarantee that the image is trustworthy. Verify published
checksums and signatures from the vendor.

For automated testing, a small Alpine `virt` ISO is sufficient. Do not commit ISO
files to the repository.

## Data locations

OpenQVM stores configuration, disks, logs and the trash under one data directory:

| Platform | Default |
| --- | --- |
| Windows | `%LOCALAPPDATA%\OpenQVM` |
| Linux | `$XDG_DATA_HOME/openqvm` or `~/.local/share/openqvm` |
| macOS | `~/Library/Application Support/OpenQVM` |

Set `OPENQVM_HOME` to override it for all commands, or pass `--data-dir` to
`openqvm-cli`. Inside the directory:

```
vms/<uuid>/       one directory per VM
iso/              suggested location for installation images
exports/          default destination for .oqv packages
logs/             application log
trash/            deleted VM directories (recoverable until purged)
settings.json     theme, QEMU directory and UI preferences
operations.jsonl  operation history
```

Back up the whole data directory while all VMs are stopped. Exporting a VM
produces a portable package that includes its disks and snapshot metadata.

## Uninstall

1. Stop all virtual machines.
2. Remove the Python environment (`.venv`).
3. Optionally remove the OpenQVM data directory. This permanently deletes VMs,
   disks and the trash. Secure erase is not guaranteed on SSDs.

QEMU itself is removed with the platform package manager.

## Troubleshooting

| Symptom | Meaning and fix |
| --- | --- |
| `QEMU is not available` | QEMU is not installed or `qemu-system-x86_64`/`qemu-img` cannot be executed. Install QEMU or set the directory in Settings. |
| `Accelerator: tcg` | No hardware acceleration. On Linux check `/dev/kvm`; on Windows enable HypervisorPlatform and reboot; on macOS use an x86_64-capable host or expect TCG. |
| `QEMU exited during startup` | The dialog includes the QEMU log tail. Common causes: unavailable accelerator, missing firmware, an invalid disk path or a port conflict. |
| `Could not connect to the QEMU control interface` | QEMU crashed before QMP came up. Inspect `qemu.log` in the VM folder. |
| Guest installer cannot see the disk | The selected bus needs guest drivers. Use IDE for Windows installers without VirtIO drivers, or provide the drivers. |
| Black console | The guest may still be booting, VNC may be disabled in the VM configuration, or the guest does not initialize the display. Check the serial console tab. |
| Snapshot is "disk only" | The accelerator cannot save guest memory (WHPX commonly). Restore it while the VM is stopped. |
| `The QEMU control interface is unavailable` | The VM process is alive but QMP is unreachable. Use **Force stop** before disk operations. |
| Import rejected | The package failed traversal, entry, compression-ratio or checksum validation. Obtain a new export. |

Logs are available in the VM folder (`qemu.log`, `serial.log`) and in
`logs/openqvm.log`. `openqvm-cli logs <vm>` prints both tails.
