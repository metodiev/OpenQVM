# Roadmap

This roadmap separates work that is **implemented and verified** from work that is
planned. See the README for the honest platform-validation status.

## Phase 1 — Foundation ✅

- [x] Repository structure, packaging (`pyproject.toml`, entry points)
- [x] Domain model, validation, structured errors
- [x] Atomic persistence, backup recovery, per-VM locking
- [x] Logging with rotation and redaction
- [x] Capability detection and QEMU discovery
- [x] Qt application shell, themes, dashboard, CLI
- [x] Unit and integration tests, CI

## Phase 2 — First working virtual machine ✅

- [x] QEMU backend with detached processes
- [x] QMP client (capabilities, status, stop/cont, powerdown, quit, human monitor)
- [x] VM creation, start, stop, pause, resume and status
- [x] Boot from an ISO
- [x] Real guest framebuffer in the application (embedded VNC/RFB)
- [x] Serial console and QEMU log
- [x] Real boot verified on Windows 11/WHPX (Alpine Linux)

**Remaining for “Linux first” parity:** run the same e2e suite on Linux with KVM
and on macOS/HVF, and publish those results. The backend path is shared, but
untested platforms are not claimed as verified.

## Phase 3 — Essential VM management ✅

- [x] Persistent configuration with schema version and revision checks
- [x] Virtual disks, ISO attach/eject
- [x] Resource configuration and validation
- [x] Lifecycle error recovery and runtime reconciliation on startup
- [x] Disk inspection, consistency check API, grow-only resize
- [x] Real-time UI updates through typed manager events (no polling delay)
- [x] Add/remove data disks
- [x] UEFI firmware support when edk2/OVMF is available
- [x] Corrupt configuration recovery and trash/quarantine behavior

## Phase 4 — Advanced features (partially complete)

- [x] Snapshots (offline disk-only, online memory where supported, disk-only
      fallback with honest labelling)
- [x] Snapshot restore/delete and metadata
- [x] Clone stopped VMs with independent disks
- [x] Export/import `.oqv` packages with integrity checks
- [x] Backup workflow via stopped-VM export
- [x] NAT networking with port forwarding
- [x] Shared folders on Linux (virtio-9p; guest drivers required)
- [x] Host and per-VM metrics, operation history, `doctor` diagnostics
- [x] Live ISO insert/replace/eject through QMP (media lock respected; `--force` opt-in)
- [x] Live disk growth through QMP `block_resize`
- [x] Console input: absolute USB pointer, Ctrl+Alt+Del, type-text without a guest agent
- [x] Cooperative cancellation for long-running exports, imports, clones, disk and
      snapshot operations, with partial-artifact cleanup
- [x] Operation lifecycle events (`operation.started`, `operation.progress`,
      `operation.finished`) with byte-accurate progress for exports and imports
- [x] Scheduled automatic backups of stopped VMs with retention and a backup index
- [ ] Bridged networking (requires privileged helper/bridge configuration)
- [ ] Host-only networking
- [ ] TPM 2.0 (swtpm integration)
- [ ] Clipboard integration
- [x] Scheduled/automatic backups
- [ ] Shared folders on Windows/macOS
- [ ] USB passthrough

## Phase 5 — Cross-platform support (partial)

- [x] Windows adapter with WHPX/TCG detection and real execution
- [x] Linux adapter with KVM detection, bridge-helper detection, 9p support flag
- [x] macOS adapter with HVF detection and honest feature reporting
- [ ] Execute and publish Linux/KVM integration and e2e results
- [ ] Execute and publish macOS/HVF results
- [ ] Native Apple Silicon guest support
- [ ] Platform installers and packages (MSI/MSIX, deb/rpm, macOS app bundle)
- [ ] Code signing/notarization

## Phase 6 — Production readiness (partial)

- [x] Automated unit, integration and real-QEMU end-to-end tests
- [x] Security model and import hardening
- [x] Failure recovery for startup, snapshots and runtime reconciliation
- [x] Installation, development and troubleshooting documentation
- [ ] Independent security review
- [ ] Fuzzing of the import format and configuration parser
- [ ] Dependency vulnerability scanning in CI
- [ ] Signed releases and reproducible-build verification
- [ ] Accessibility audit with screen readers and high-contrast themes
- [ ] Localization

## Next concrete steps

1. Run the e2e suite on a Linux/KVM host and document the result.
2. Add QMP/VNC authentication (Unix socket or session token) for multi-user hosts.
3. Implement host-only networking and a privileged, authenticated bridge helper.
4. Add TPM 2.0 support for Windows 11 guests.
5. Add Ctrl+C-aware progress reporting to `openqvm-cli` long-running commands.
6. Package installers and add code signing.
