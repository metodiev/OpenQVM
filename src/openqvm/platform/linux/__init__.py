"""Linux-specific capability detection."""

from __future__ import annotations

import os
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


def kvm_usable() -> tuple[bool, str]:
    device = "/dev/kvm"
    if not os.path.exists(device):
        return False, "/dev/kvm does not exist. Enable virtualization in the firmware and load the KVM module."
    if not os.access(device, os.R_OK | os.W_OK):
        return False, "/dev/kvm is not accessible to this user. Add the user to the 'kvm' group."
    return True, "/dev/kvm is available."


def _bridge_helper() -> str | None:
    for candidate in ("/usr/lib/qemu/qemu-bridge-helper", "/usr/libexec/qemu-bridge-helper", "/usr/local/libexec/qemu-bridge-helper"):
        if os.path.exists(candidate):
            return candidate
    return None


def describe_features() -> PlatformFeatures:
    kvm_ok, kvm_detail = kvm_usable()
    helper = _bridge_helper()
    swtpm = shutil.which("swtpm")
    diagnostics = [kvm_detail]
    if helper:
        diagnostics.append(f"qemu-bridge-helper found at {helper}; bridged networking still requires host bridge configuration.")
    else:
        diagnostics.append("qemu-bridge-helper was not found, so bridged networking is unavailable.")
    return PlatformFeatures(
        bridged_supported=helper is not None,
        bridged_detail=(
            "Requires an existing host bridge and qemu-bridge-helper privileges."
            if helper
            else "Install qemu-bridge-helper and configure a host bridge to enable bridged networking."
        ),
        host_only_supported=False,
        host_only_detail="Host-only networks are not implemented yet; use NAT networking instead.",
        shared_folders_supported=True,
        shared_folders_detail="Shared folders use QEMU virtio-9p (requires guest drivers).",
        tpm_supported=swtpm is not None,
        tpm_detail="swtpm is installed." if swtpm else "Install swtpm to enable an emulated TPM 2.0 device.",
        diagnostics=tuple(diagnostics),
    )
