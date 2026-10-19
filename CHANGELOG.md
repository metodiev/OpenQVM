# Changelog

All notable changes to OpenQVM are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-10-19

### Added

- Qt 6 desktop application with dashboard, VM inventory, creation wizard,
  details, storage, networking, snapshots, logs and a danger zone.
- Embedded guest display: RFB 3.8 client with keyboard and pointer forwarding,
  plus serial console and QEMU log streaming.
- QEMU backend with detached process supervision, QMP control, PID + creation-time
  identity checks and strict failure reporting.
- Real QEMU boot verification: the end-to-end test boots Alpine Linux, waits for a
  login prompt, captures the VNC framebuffer, pauses/resumes, snapshots, restores,
  exports, imports and clones.
- Platform detection for Linux KVM, Windows WHPX, macOS HVF and TCG, with UEFI,
  bridged/host-only networking, shared folder and TPM capability reporting.
- Persistent VM configuration with atomic writes, backup recovery, per-VM
  advisory locks and quarantine-on-delete.
- Storage management: qcow2/raw disks, VirtIO/IDE buses, grow-only resize, data
  disk add/remove, ISO attach/eject and ISO-9660 inspection.
- Snapshots: offline disk-only, online memory-inclusive where QEMU supports it, and
  an explicitly labelled disk-only fallback for accelerators such as WHPX. Online
  restore of disk-only snapshots is refused with a clear explanation.
- Clone, export/import of self-contained `.oqv` packages with SHA-256 checksums,
  traversal/symlink/zip-bomb defenses.
- NAT networking with validated port forwarding and security notes.
- CLI (`openqvm-cli`) with doctor/probe, create, list, lifecycle, snapshot, clone,
  export/import, resize, ISO and log commands, plus JSON output.
- Host and per-VM metrics through psutil; unavailable values are shown as such.
- Dark and light themes, keyboard shortcuts, progress indicators and structured
  error dialogs.
- Apache License 2.0, security policy, contribution guide, architecture,
  installation, development, security and roadmap documentation.
- GitHub Actions CI for Linux, Windows and macOS with an optional real-QEMU e2e
  job.

### Known limitations

- Validated on Windows 11/AMD64 with QEMU 11.1, WHPX and TCG. Linux/KVM was not
  executed in the development environment; macOS is untested.
- Bridged and host-only networking are not implemented.
- Shared folders are only available on Linux hosts.
- No TPM 2.0 support; Windows 11 guests warn about this.
- No live ISO change, live resize, clipboard integration or USB passthrough.
- Clones do not include snapshot history.
- QMP/VNC are loopback-only but unauthenticated.
- No installers or signed releases yet.
