"""Compatibility checks between a VM definition and the detected platform."""

from __future__ import annotations

from openqvm.core.errors import ValidationIssue
from openqvm.core.models import Firmware, NetworkMode, VMSpec
from openqvm.platform.capabilities import PlatformCapabilities


def check_compatibility(spec: VMSpec, capabilities: PlatformCapabilities) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    if not capabilities.backend_available:
        issues.append(
            ValidationIssue(
                "backend",
                "QEMU is not installed or could not be executed. Install QEMU and restart OpenQVM.",
            )
        )
        return issues

    if spec.firmware is Firmware.UEFI and capabilities.uefi is None:
        issues.append(
            ValidationIssue(
                "firmware",
                "UEFI firmware (edk2/OVMF) was not found. Install the edk2 firmware package or switch the VM to BIOS.",
            )
        )

    if spec.accelerator and spec.accelerator not in capabilities.accelerator_names:
        issues.append(
            ValidationIssue(
                "accelerator",
                f"The accelerator '{spec.accelerator}' is not available on this host. "
                f"Available: {', '.join(capabilities.accelerator_names) or 'none'}.",
            )
        )

    for index, nic in enumerate(spec.nics):
        if nic.mode is NetworkMode.BRIDGED and not capabilities.bridged_supported:
            issues.append(
                ValidationIssue(
                    f"nics[{index}].mode",
                    f"Bridged networking is not available: {capabilities.bridged_detail}",
                )
            )
        if nic.mode is NetworkMode.HOST_ONLY and not capabilities.host_only_supported:
            issues.append(
                ValidationIssue(
                    f"nics[{index}].mode",
                    f"Host-only networking is not available: {capabilities.host_only_detail}",
                )
            )

    if spec.shared_folders and not capabilities.shared_folders_supported:
        issues.append(
            ValidationIssue(
                "shared_folders",
                f"Shared folders are not available: {capabilities.shared_folders_detail}",
            )
        )

    if spec.tpm and not capabilities.tpm_supported:
        issues.append(
            ValidationIssue(
                "tpm",
                f"TPM 2.0 is not available: {capabilities.tpm_detail or 'swtpm was not detected.'}",
            )
        )

    if not capabilities.default_accelerator:
        issues.append(
            ValidationIssue(
                "accelerator",
                "No usable QEMU accelerator was detected. Install or enable KVM/WHPX/HVF.",
            )
        )

    return issues
