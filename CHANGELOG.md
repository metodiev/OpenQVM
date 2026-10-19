# Changelog

All notable changes to OpenQVM are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Event-driven UI updates: `VmManager` publishes typed events for VM
  create/update/delete, lifecycle transitions, snapshot changes and finished
  operations. The window and console react immediately instead of waiting for the
  polling interval.
- Disk consistency checks with `qemu-img check` in the VM Storage tab, in
  `openqvm-cli check`, and per-disk JSON results. Checks are refused while the VM
  is running.
- Live storage operations: the CD-ROM drive is always present so media can be
  inserted, replaced or ejected while the VM runs, and disks can be grown through
  QEMU `block_resize`. QEMU's media lock is respected by default; `--force` is
  available for explicit, documented overrides.
- Operation lifecycle events and progress: `operation.started` and
  `operation.progress` join `operation.finished`, so the dashboard shows running
  operations immediately. Exports, imports, clones, VM creation and disk checks
  report byte- or disk-accurate progress, which the GUI renders as a determinate
  status-bar indicator and `openqvm-cli` prints on an interactive terminal.
- Automatic backups: a schedule (enable, interval, retention, folder) in Settings
  backs up every stopped VM whose newest backup is older than the interval, keeps
  the newest packages per VM and prunes the rest. Backups are ordinary `.oqv`
  packages recorded in `backups.json`; the VM details page lists them with a
  "Back up now" action, and `openqvm-cli backup`/`backups` expose the same
  functionality, including `--due` for cron-style scripting.
- TPM 2.0: VMs can request an emulated TPM 2.0 device. OpenQVM probes the QEMU
  binary for `-tpmdev` support, supervises an `swtpm` process (loopback TCP,
  readiness wait, crash cleanup) and passes `-chardev`/`-tpmdev emulator`/
  `-device tpm-tis` to QEMU. Capability detection no longer assumes that an
  installed swtpm is enough: Windows QEMU builds have no TPM support and are
  reported as unavailable. The wizard and VM details expose the switch, and
  `openqvm-cli create --tpm` sets it.
- Hardening: property-based tests (Hypothesis) assert that arbitrary package
  bytes, archive layouts, manifests, settings and VM configurations are rejected
  with structured errors and leave no partial state; a new CI job runs
  `pip-audit` over the installed dependencies.
- Cooperative cancellation for long-running operations: a `CancellationToken`
  (``openqvm.core.cancellation``) is threaded through disk, snapshot and transfer
  work, checked between chunks, and surfaced in the GUI as a Cancel button in the
  status bar. Cancelled work removes partial packages, staging directories and
  half-written disks instead of leaving debris.
- Integration and end-to-end coverage for the event bus, disk checks,
  event-driven refresh and live storage operations.
- Guest compatibility and console input: an absolute USB tablet (no pointer
  drift), RTC in local time for Windows guests and UTC for others, a
  Ctrl+Alt+Del action and "type text" input that works without a guest agent.
  The real-QEMU test logs in over VNC and executes a command to prove input
  reaches the guest.

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
