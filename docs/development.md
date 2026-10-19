# Development guide

## Setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
source .venv/bin/activate
pip install -e ".[dev]"
```

The editable install exposes `openqvm` (GUI) and `openqvm-cli`. QEMU is required
only for real VM operations; the unit and integration suites use fakes.

## Repository layout

```
src/openqvm/
  core/          models, validation, atomic writes, locking, logging, errors, cancellation
  platform/      QEMU discovery, capability detection, OS-specific adapters
  vm/            VmManager, backend protocol, QEMU process/QMP/RFB, command builder
  storage/       qemu-img wrapper, ISO inspection, .oqv transfer
  snapshots/     offline qemu-img snapshots and online QMP snapshots
  networking/    QEMU netdev/device argument construction
  services/      operation history, backup policy, psutil metrics
  app/           AppContext composition root
  ui/            Qt pages, theme, async runner, VNC widget
tests/
  unit/          models, validation, store, QMP, RFB, command builder, security, cancellation
  integration/   manager orchestration with a fake backend and fake disks
  end_to_end/    real QEMU boot, console, snapshot, export/import, clone
docs/            architecture, installation, security, this guide
packaging/       optional PyInstaller specification
scripts/         helper scripts
```

No platform-specific logic belongs in the UI. Add it to `openqvm.platform` and
return capabilities that validation can enforce.

## Running tests

```bash
pytest tests/unit tests/integration -q
pytest tests/end_to_end -m e2e -q -s
ruff check src tests --select F,E9
```

- GUI tests set `QT_QPA_PLATFORM=offscreen` in `tests/conftest.py`.
- The end-to-end test is skipped unless QEMU is installed and an Alpine ISO is
  available. Point `OPENQVM_TEST_ISO` at an ISO to make it deterministic.
- Unit tests do not start QEMU. Integration tests use `FakeBackend`/`FakeDisks`,
  which exercise manager orchestration but do not claim real virtualization.
- Property-based tests live in `tests/unit/test_import_fuzz.py`. They generate
  arbitrary package bytes, archive layouts, manifests, settings and VM
  configurations and assert the parser contract: an `OpenQVMError` or a valid
  object, never an unexpected exception or a leftover staging directory. Add a
  strategy there instead of a hand-written malformed-input case.
- No test may report hardware acceleration or a successful boot unless it actually
  happened. The real e2e test is the only test that asserts a guest boot.

### What the e2e test verifies

1. A VM is created with a real qcow2 disk.
2. QEMU starts with the detected accelerator, an absolute USB pointer, and the
   expected RTC mode.
3. The guest reaches an Alpine login prompt on the serial console.
4. A full VNC framebuffer is captured and contains non-black pixels.
5. Keyboard input sent over VNC reaches the guest: the test logs in and runs a
   command that writes a marker to the serial port.
6. Pause and resume work through QMP.
7. An online snapshot is created (memory-inclusive where supported, otherwise the
   documented disk-only fallback).
8. A running disk is grown and the new size is verified with `qemu-img` after
   shutdown.
9. The VM is stopped, the snapshot is restored offline, and the VM boots again.
10. The VM is exported and re-imported with checksum verification.
11. The VM is cloned and all temporary VMs are removed.
12. A second test changes the CD-ROM medium on a running VM through QMP.

Run it before releasing changes that touch process control, storage or the
transfer format.

## Manual verification

```bash
openqvm-cli doctor --probe
openqvm-cli create --name "Test VM" --os linux --cpus 2 --memory 512 --disk 2 --iso /path/to/alpine.iso
openqvm-cli start <vm-id>
openqvm-cli logs <vm-id>
openqvm-cli status <vm-id>
openqvm-cli pause <vm-id>
openqvm-cli resume <vm-id>
openqvm-cli stop <vm-id> --force
```

The GUI can be inspected without a real display for layout changes:

```python
# QT_QPA_PLATFORM=offscreen
from PySide6.QtWidgets import QApplication
from openqvm.app.context import AppContext
from openqvm.ui.main_window import MainWindow
app = QApplication([])
window = MainWindow(AppContext.create())
window.show()
window.grab().save("dashboard.png")
```

## Design rules

- **Never use a shell.** All external commands are argv lists. Validate values
  that become QEMU options and escape commas.
- **Do not fake success.** A backend operation returns only after QEMU/QMP or
  `qemu-img` confirms it. Errors carry a code, message, hint and details.
- **Do not fake data.** Metrics come from psutil; unavailable values are `None`
  and rendered as "Unavailable".
- **Keep platform code isolated.** `openqvm.platform` reports capabilities;
  `VmManager` enforces them before start and create.
- **Treat imported packages and ISOs as untrusted.** Extend the allow-list and
  limits in `storage/transfer.py` carefully.
- **Persist atomically.** Use `atomic_write_json` and keep the `.bak` recovery
  behavior.
- **Lock per VM.** Mutations take the VM lock; the lock file lives outside the VM
  directory so deletion can quarantine it safely.
- **Test the contract.** Add unit tests for validation/argv/parsing and an
  integration test for orchestration. Real QEMU behavior belongs in e2e tests.
- **Publish state changes as events.** `VmManager` publishes typed events through
  `openqvm.core.events.EventBus`. Widgets must bridge them with a Qt signal (see
  `MainWindow._subscribe_events`) because publishing may happen on a worker
  thread; never touch widgets from the subscriber callback.

## Live storage operations

- Offline changes use `qemu-img`.
- Live disk growth uses QMP `block_resize` with the drive's `id` (`drive<N>`) and
  then verifies the size QEMU reports. The guest may need to rescan the block
  device and grow its file system.
- The CD-ROM drive (`cdrom0`) always exists. Live media changes use
  `blockdev-change-medium`/tray commands keyed by the device's QOM path.
- QEMU's media lock is respected: if the guest is using the medium, the operation
  fails with an actionable message. `force=True` (CLI `--force`) overrides the
  lock and is documented as potentially disruptive.
- Never bypass the lock automatically; a forced eject can break a guest that has
  the media mounted.

## Backups

- A backup is a normal `.oqv` export. There is no separate archive format and no
  in-place restore: importing a backup creates a new VM, while snapshots provide
  in-place rollback.
- Backups require a stopped VM. `VmManager.backup_vm` refuses to run against a
  busy runtime instead of silently exporting inconsistent disks.
- The backup index lives in `<vm>/backups.json` (`VmStore.load_backups`/
  `save_backups`, `BackupEntry` in `openqvm.core.models`). Packages default to the
  data directory's `backups` folder.
- Retention is per VM: `backups_to_prune` keeps the newest `keep` entries and
  `backup_vm` deletes the rest after the index is written, so a crash between the
  two never loses the index.
- Scheduling is a caller concern: the GUI checks every 15 minutes (and once 45
  seconds after startup) and `openqvm-cli backup --due` runs the same
  `run_scheduled_backups` pass for cron. `is_due` decides from the newest entry
  and the policy interval; `BackupPolicy` validates the bounds.
- Publishing: `backups.changed` fires after a successful backup so open pages
  refresh through the event bus instead of polling.

## Progress reporting

- Operations accept an optional `progress: ProgressCallback`
  (`openqvm.core.progress`) receiving `(fraction, message)`, where `fraction` is
  `None` when the remaining work cannot be estimated.
- `VmManager` republishes every report as `operation.progress`; callers that pass
  their own callback still receive updates because the publisher composes them.
- Byte-based work uses `ByteProgress`, which converts handled bytes into a
  fraction of a fixed total. Exports count the write pass and the verification
  pass; imports count the manifest's stored bytes.
- Use `report_progress` for step-based work (one disk of N) rather than inventing
  percentages, and keep the reports monotonic.

## Cancelling operations

- Long operations take an optional `cancel: CancellationToken`
  (`openqvm.core.cancellation`). Pass it down instead of catching Ctrl+C or
  checking a global flag.
- Check it between chunks (`raise_if_cancelled`) and before irreversible steps
  (registering a VM, replacing an archive). A cancelled operation must leave no
  partial file, package or VM directory behind.
- `run_tool` polls its child process and terminates it; a single in-flight
  `qemu-img` or QMP call cannot be interrupted mid-request, so cancellation is
  cooperative rather than instant.
- Signal cancellation with `CancelledError`; the GUI shows it in the status bar
  instead of an error dialog (`MainWindow._report_error`).
- `AsyncRunner.submit` returns the job's token and `cancel_active()` cancels every
  running job; callbacks receive the token as their only argument.

## Snapshot semantics

- Offline (`qemu-img snapshot`): disk-only, crash-consistent at best.
- Online (`savevm`): includes RAM when the accelerator supports VM state.
- WHPX fallback (`blockdev-snapshot-internal-sync`): disk-only and labelled as
  such; online restore is rejected because QEMU's `loadvm` refuses disk-only
  snapshots and can leave the VM in a transient state.
- Snapshots require qcow2; raw disks are rejected before any command runs.
- Snapshot metadata (description, memory inclusion, VM state) lives in
  `snapshots.json`; the snapshot tags live inside the qcow2 files.

## Adding a backend

Implement the `VirtualizationBackend` protocol in `vm/backend.py` and provide
`BackendCapabilities`. Wire it into `VmManager._configure_components`. Validation,
storage and transfer remain backend-independent if the backend supports the
selected disk formats. Add capability checks in
`platform/compatibility.py` rather than scattering `if platform` branches through
the UI.

For a native Windows Hyper-V or macOS Virtualization.framework backend, keep the
management process unprivileged and treat the platform helper as a separate trust
boundary.

## Logging and diagnostics

Use `openqvm.core.logging.get_logger`. Logs are written to
`<data>/logs/openqvm.log` with rotation and secret redaction. Include command
names and exit codes, not full environments. `openqvm-cli doctor --probe` is the
first diagnostic for accelerator issues; VM-specific logs are in the VM folder.

## Packaging and release

The project builds as a standard Python package:

```bash
python -m pip install build
python -m build
```

This produces a wheel and sdist under `dist/`. An optional PyInstaller
specification is in `packaging/openqvm.spec`; it is **experimental and not covered
by automated tests**. No signed installers are produced yet.

Release steps:

1. Update the version in `pyproject.toml` and `src/openqvm/__init__.py`.
2. Update `CHANGELOG.md`.
3. Run lint, unit/integration tests and the real e2e test on each supported host.
4. Build and test the wheel in a clean environment.
5. Audit dependency licenses and vulnerabilities (`pip-audit`, `pip-licenses` or
   equivalent) and update `THIRD_PARTY_NOTICES.md`.
6. Tag the release and attach artifacts. State which platforms were actually
   tested.

## CI

`.github/workflows/ci.yml` runs lint and tests on Linux, Windows and macOS for
Python 3.11 and 3.13. The real-QEMU e2e job runs on Ubuntu for manual dispatches
and on a schedule; GitHub runners usually lack KVM, so it uses TCG and states
that in the job name. A separate `dependency-audit` job installs the project with
its dev extras and runs `pip-audit`, so a known vulnerability in any dependency
fails CI.
