"""macOS-specific capability detection."""

from __future__ import annotations

import shutil
from dataclasses import dataclass


@dataclass(frozen=True)
class PlatformFeatures:
    bridged_supported: bool
    bridged_detail: str
    host_only_supported: bool
    host_only_detail: str
    shared_folders_supported: bool
    shared_folders_detail: str
    tpm_supported: bool
    tpm_detail: str
    diagnostics: tuple[str, ...]


def describe_features() -> PlatformFeatures:
    diagnostics = [
        "QEMU uses Apple's Hypervisor.framework (hvf) when available.",
        "Bridged and host-only networks require Apple vmnet entitlements and are not implemented yet.",
    ]
    return PlatformFeatures(
        bridged_supported=False,
        bridged_detail="Bridged networking requires a privileged vmnet helper; not implemented in this release.",
        host_only_supported=False,
        host_only_detail="Host-only networking requires a privileged vmnet helper; not implemented in this release.",
        shared_folders_supported=False,
        shared_folders_detail="Shared folders are not enabled on macOS in this release.",
        tpm_supported=False,
        tpm_detail="TPM emulation is not available on macOS in this release.",
        diagnostics=tuple(diagnostics),
    )


def swtpm_available() -> bool:
    return shutil.which("swtpm") is not None
