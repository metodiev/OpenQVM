"""Windows-specific capability detection."""

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


def hypervisor_present() -> bool:
    """Return True when the Windows Hypervisor Platform (WHPX) is usable.

    ``WinHvPlatform.dll`` exposes the authoritative capability query, which does
    not require administrator rights.  QEMU still reports the final error at
    launch time if the accelerator cannot be used.
    """

    try:
        import ctypes

        library = ctypes.WinDLL("WinHvPlatform.dll")
        capability = ctypes.c_uint32(0)
        written = ctypes.c_uint32(0)
        # WHvCapabilityCodeHypervisorPresent == 0
        result = library.WHvGetCapability(0, ctypes.byref(capability), 4, ctypes.byref(written))
        return result == 0 and capability.value != 0
    except (OSError, AttributeError, ValueError):
        return False


def describe_features() -> PlatformFeatures:
    swtpm = shutil.which("swtpm") or shutil.which("swtpm.exe")
    diagnostics: list[str] = []
    if hypervisor_present():
        diagnostics.append("A Windows hypervisor is present; WHPX is the preferred accelerator.")
    else:
        diagnostics.append("No Windows hypervisor detected; QEMU will use software emulation (TCG) unless WHPX is enabled.")
    diagnostics.append("Bridged networking is not implemented on Windows in this release (TAP driver integration is required).")
    return PlatformFeatures(
        bridged_supported=False,
        bridged_detail="Bridged networking requires a TAP-Windows adapter, which OpenQVM does not configure yet.",
        host_only_supported=False,
        host_only_detail="Host-only networking is not implemented on Windows yet.",
        shared_folders_supported=False,
        shared_folders_detail="Virtio-9p shared folders are not available for Windows hosts.",
        tpm_supported=swtpm is not None,
        tpm_detail="swtpm is installed." if swtpm else "Install swtpm to enable an emulated TPM 2.0 device.",
        diagnostics=tuple(diagnostics),
    )
