"""QEMU TPM capability probe behaviour."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from openqvm.platform import capabilities as capabilities_module


def _reset_cache() -> None:
    capabilities_module._tpm_probe_cache.clear()


def test_probe_reports_support_and_caches_the_result(tmp_path, monkeypatch):
    _reset_cache()
    binary = tmp_path / "qemu-system-x86_64"
    binary.write_bytes(b"")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="tpmdev emulator", stderr="")

    monkeypatch.setattr(capabilities_module.subprocess, "run", fake_run)
    assert capabilities_module.qemu_tpm_supported(binary) is True
    assert capabilities_module.qemu_tpm_supported(binary) is True
    assert calls == [[str(binary), "-tpmdev", "help"]]
    assert calls[0][1:] == ["-tpmdev", "help"]


def test_probe_reports_missing_support(tmp_path, monkeypatch):
    _reset_cache()
    binary = tmp_path / "qemu-system-x86_64"
    binary.write_bytes(b"")

    monkeypatch.setattr(
        capabilities_module.subprocess,
        "run",
        lambda argv, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="invalid option"),
    )
    assert capabilities_module.qemu_tpm_supported(binary) is False


def test_probe_treats_an_unusable_binary_as_unsupported(tmp_path, monkeypatch):
    _reset_cache()
    binary = tmp_path / "qemu-system-x86_64"
    binary.write_bytes(b"")

    def explode(argv, **kwargs):  # type: ignore[no-untyped-def]
        raise subprocess.TimeoutExpired(cmd=argv, timeout=1)

    monkeypatch.setattr(capabilities_module.subprocess, "run", explode)
    assert capabilities_module.qemu_tpm_supported(binary) is False
    assert capabilities_module.qemu_tpm_supported(None) is False


def test_detected_capabilities_report_tpm_honestly():
    detected = capabilities_module.detect_capabilities()
    assert isinstance(detected.tpm_supported, bool)
    assert detected.tpm_detail
    if not detected.tpm_supported:
        assert "TPM" in detected.tpm_detail or "QEMU" in detected.tpm_detail
        assert Path("does-not-exist") != Path(detected.tpm_binary)  # empty string when absent
    assert "TPM 2.0:" in " ".join(detected.diagnostics)
