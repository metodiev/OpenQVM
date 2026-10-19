"""Platform capability detection and reporting."""

from __future__ import annotations

import os
import platform as host_platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from openqvm.core.logging import get_logger
from openqvm.platform import linux, macos, windows
from openqvm.platform.qemu_discovery import QemuInstall, discover_qemu

log = get_logger("platform.capabilities")

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
_tpm_probe_cache: dict[str, bool] = {}


def qemu_tpm_supported(system_path: Path | None, *, timeout: float = 15.0) -> bool:
    """Ask the QEMU binary whether it was built with TPM support.

    Windows builds of QEMU usually omit ``-tpmdev``; guessing from installed
    helper tools would advertise a device QEMU cannot create.
    """

    if system_path is None:
        return False
    key = str(system_path)
    if key in _tpm_probe_cache:
        return _tpm_probe_cache[key]
    supported = False
    try:
        completed = subprocess.run(
            [str(system_path), "-tpmdev", "help"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
            check=False,
        )
        supported = completed.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Could not probe %s for TPM support: %s", system_path, exc)
    _tpm_probe_cache[key] = supported
    return supported


@dataclass(frozen=True)
class AcceleratorInfo:
    name: str
    available: bool
    detail: str = ""


@dataclass(frozen=True)
class UefiFirmware:
    code_path: Path
    vars_template: Path


@dataclass
class PlatformCapabilities:
    os_family: str
    architecture: str
    qemu: QemuInstall | None
    accelerators: list[AcceleratorInfo] = field(default_factory=list)
    default_accelerator: str | None = None
    uefi: UefiFirmware | None = None
    bridged_supported: bool = False
    bridged_detail: str = ""
    host_only_supported: bool = False
    host_only_detail: str = ""
    shared_folders_supported: bool = False
    shared_folders_detail: str = ""
    tpm_supported: bool = False
    tpm_detail: str = ""
    tpm_binary: str = ""
    diagnostics: list[str] = field(default_factory=list)
    detected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @property
    def backend_available(self) -> bool:
        return self.qemu is not None

    @property
    def qemu_version(self) -> str | None:
        return self.qemu.version if self.qemu else None

    @property
    def accelerator_names(self) -> list[str]:
        return [item.name for item in self.accelerators if item.available]

    def accelerator_detail(self, name: str) -> str:
        for item in self.accelerators:
            if item.name == name:
                return item.detail
        return ""

    def effective_accelerator(self, override: str | None = None) -> str | None:
        if override:
            return override
        return self.default_accelerator

    def to_dict(self) -> dict[str, object]:
        return {
            "os_family": self.os_family,
            "architecture": self.architecture,
            "qemu": self.qemu.as_dict() if self.qemu else None,
            "accelerators": [
                {"name": item.name, "available": item.available, "detail": item.detail}
                for item in self.accelerators
            ],
            "default_accelerator": self.default_accelerator,
            "uefi": (
                {"code_path": str(self.uefi.code_path), "vars_template": str(self.uefi.vars_template)}
                if self.uefi
                else None
            ),
            "bridged_supported": self.bridged_supported,
            "bridged_detail": self.bridged_detail,
            "host_only_supported": self.host_only_supported,
            "host_only_detail": self.host_only_detail,
            "shared_folders_supported": self.shared_folders_supported,
            "shared_folders_detail": self.shared_folders_detail,
            "tpm_supported": self.tpm_supported,
            "tpm_detail": self.tpm_detail,
            "tpm_binary": self.tpm_binary,
            "diagnostics": list(self.diagnostics),
            "detected_at": self.detected_at,
        }


def _os_family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _platform_features():  # type: ignore[no-untyped-def]
    os_family = _os_family()
    if os_family == "windows":
        return windows.describe_features()
    if os_family == "macos":
        return macos.describe_features()
    return linux.describe_features()


def _accelerator_availability(names: list[str], features) -> list[AcceleratorInfo]:  # type: ignore[no-untyped-def]
    os_family = _os_family()
    details: dict[str, tuple[bool, str]] = {}
    if os_family == "linux":
        kvm_ok, kvm_detail = linux.kvm_usable()
        details["kvm"] = (kvm_ok, kvm_detail)
    elif os_family == "windows":
        present = windows.hypervisor_present()
        details["whpx"] = (
            present,
            "Windows Hypervisor Platform is enabled." if present else "Enable the Windows Hypervisor Platform feature.",
        )
    elif os_family == "macos":
        details["hvf"] = (True, "Hypervisor.framework is used when the host supports it.")
    details["tcg"] = (True, "Software emulation (TCG). Slower than hardware acceleration.")

    infos: list[AcceleratorInfo] = []
    for name in names:
        available, detail = details.get(name, (True, ""))
        infos.append(AcceleratorInfo(name=name, available=available, detail=detail))
    # Always surface TCG even when the binary does not list accelerators.
    if not any(info.name == "tcg" for info in infos):
        infos.append(AcceleratorInfo("tcg", True, details["tcg"][1]))
    return infos


def _preferred_accelerator(infos: list[AcceleratorInfo]) -> str | None:
    for candidate in ("kvm", "hvf", "whpx"):
        for info in infos:
            if info.name == candidate and info.available:
                return candidate
    for info in infos:
        if info.name == "tcg" and info.available:
            return "tcg"
    return None


def _find_uefi(qemu: QemuInstall | None) -> UefiFirmware | None:
    roots: list[Path] = []
    if qemu and qemu.share_dir:
        roots.append(qemu.share_dir)
    roots.extend(
        [
            Path("/usr/share/qemu"),
            Path("/usr/share/OVMF"),
            Path("/usr/share/edk2/x64"),
            Path("/usr/share/edk2/ovmf"),
            Path("/usr/local/share/qemu"),
            Path("/opt/homebrew/share/qemu"),
        ]
    )
    code_names = [
        "edk2-x86_64-code.fd",
        "edk2/ovmf/OVMF_CODE.fd",
        "OVMF_CODE.fd",
        "OVMF_CODE_4M.fd",
    ]
    vars_names = [
        "edk2-i386-vars.fd",
        "edk2-x86_64-vars.fd",
        "edk2/ovmf/OVMF_VARS.fd",
        "OVMF_VARS.fd",
        "OVMF_VARS_4M.fd",
    ]
    for root in roots:
        try:
            if not root.is_dir():
                continue
        except OSError:
            continue
        for code_name in code_names:
            code = root / code_name
            if not code.is_file():
                continue
            for vars_name in vars_names:
                variables = root / vars_name
                if variables.is_file():
                    return UefiFirmware(code_path=code, vars_template=variables)
    return None


def detect_capabilities(*, qemu_dir: str | Path | None = None) -> PlatformCapabilities:
    """Probe the host for QEMU, accelerators and optional features.

    This function performs real process probes and file-system checks; it does
    not invent capabilities that have not been observed.
    """

    qemu = discover_qemu(qemu_dir)
    features = _platform_features()
    raw_accels = list(qemu.accelerators) if qemu else []
    if not raw_accels:
        raw_accels = ["kvm", "hvf", "whpx", "tcg"]
    accelerators = _accelerator_availability(raw_accels, features)
    arch = host_platform.machine() or "unknown"
    native_x86 = arch.lower() in {"x86_64", "amd64"}
    if not native_x86:
        # KVM/HVF/WHPX accelerate the host architecture, not an emulated x86_64
        # guest.  Do not default to an accelerator that cannot run this guest.
        adjusted: list[AcceleratorInfo] = []
        for item in accelerators:
            if item.name in {"kvm", "hvf", "whpx"}:
                adjusted.append(
                    AcceleratorInfo(
                        name=item.name,
                        available=False,
                        detail=f"{item.name} accelerates native {arch} guests; x86_64 guests use TCG.",
                    )
                )
            else:
                adjusted.append(item)
        accelerators = adjusted
    default = _preferred_accelerator(accelerators) if qemu else None
    uefi = _find_uefi(qemu)

    swtpm = shutil.which("swtpm") or shutil.which("swtpm.exe")
    qemu_tpm = qemu_tpm_supported(qemu.system_path if qemu else None)
    tpm_supported = qemu_tpm and swtpm is not None
    if qemu is None:
        tpm_detail = "QEMU was not found."
    elif not qemu_tpm:
        tpm_detail = "This QEMU build has no TPM support (the -tpmdev option is unavailable)."
    elif swtpm is None:
        tpm_detail = features.tpm_detail or "Install swtpm to enable an emulated TPM 2.0 device."
    else:
        tpm_detail = f"swtpm found at {swtpm}."

    diagnostics: list[str] = []
    if qemu:
        diagnostics.append(f"QEMU {qemu.version} at {qemu.system_path}")
        diagnostics.append(f"Accelerators reported by QEMU: {', '.join(qemu.accelerators) or 'none'}")
        if uefi:
            diagnostics.append(f"UEFI firmware: {uefi.code_path}")
        else:
            diagnostics.append("UEFI firmware was not found; BIOS boot is available.")
        diagnostics.append(f"TPM 2.0: {'available' if tpm_supported else 'unavailable'} - {tpm_detail}")
    else:
        diagnostics.append(
            "QEMU was not found. Install QEMU 8+ and ensure qemu-system-x86_64 and qemu-img are on PATH, "
            "or set the QEMU directory in Settings."
        )
    diagnostics.extend(features.diagnostics)
    if not native_x86:
        diagnostics.append(
            f"Host architecture is {arch}; x86_64 guests run under software emulation (TCG)."
        )

    capabilities = PlatformCapabilities(
        os_family=_os_family(),
        architecture=arch,
        qemu=qemu,
        accelerators=accelerators,
        default_accelerator=default,
        uefi=uefi,
        bridged_supported=features.bridged_supported,
        bridged_detail=features.bridged_detail,
        host_only_supported=features.host_only_supported,
        host_only_detail=features.host_only_detail,
        shared_folders_supported=features.shared_folders_supported,
        shared_folders_detail=features.shared_folders_detail,
        tpm_supported=tpm_supported,
        tpm_detail=tpm_detail,
        tpm_binary=str(swtpm) if swtpm else "",
        diagnostics=diagnostics,
    )
    log.info(
        "Detected platform %s/%s, QEMU=%s, accelerator=%s",
        capabilities.os_family,
        capabilities.architecture,
        capabilities.qemu_version or "missing",
        capabilities.default_accelerator or "none",
    )
    return capabilities


def qemu_environment() -> dict[str, str]:
    """Environment passed to QEMU (never inherit injection-prone variables)."""

    env = {
        "PATH": os.environ.get("PATH", ""),
    }
    if sys.platform == "win32":
        for name in ("SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA", "USERPROFILE"):
            if name in os.environ:
                env[name] = os.environ[name]
    else:
        for name in ("HOME", "TMPDIR", "LANG", "LC_ALL"):
            if name in os.environ:
                env[name] = os.environ[name]
    return env
