"""Atomic persistence, backup recovery and quarantine behavior."""

from __future__ import annotations

import json

import pytest

from openqvm.core.errors import CorruptDataError, NotFoundError
from openqvm.core.models import VMRuntime, VMStatus
from openqvm.store import AppSettings, SettingsStore


def test_save_and_load(store, linux_spec):
    store.save_spec(linux_spec)
    loaded = store.load_spec(linux_spec.id)
    assert loaded == linux_spec
    scan = store.scan()
    assert [spec.id for spec in scan.specs] == [linux_spec.id]
    assert not scan.errors


def test_corrupt_config_recovers_from_backup(store, linux_spec):
    store.save_spec(linux_spec)
    store.save_spec(linux_spec)
    spec_file = store.paths.vm_spec_file(linux_spec.id)
    spec_file.write_text("{ not valid json", encoding="utf-8")
    recovered = store.load_spec(linux_spec.id)
    assert recovered.id == linux_spec.id
    assert json.loads(spec_file.read_text(encoding="utf-8"))["id"] == linux_spec.id


def test_corrupt_config_without_backup_is_reported(store, linux_spec):
    store.save_spec(linux_spec)
    store.paths.vm_spec_file(linux_spec.id).write_text("{ broken", encoding="utf-8")
    store.paths.vm_spec_file(linux_spec.id).with_suffix(".json.bak").unlink(missing_ok=True)
    with pytest.raises(CorruptDataError):
        store.load_spec(linux_spec.id)
    scan = store.scan()
    assert scan.errors and scan.errors[0].vm_id == linux_spec.id


def test_runtime_roundtrip(store, linux_spec):
    store.save_spec(linux_spec)
    runtime = VMRuntime(vm_id=linux_spec.id, status=VMStatus.PAUSED, pid=99, qmp_port=1234)
    store.save_runtime(runtime)
    assert store.load_runtime(linux_spec.id) == runtime


def test_snapshot_metadata_roundtrip(store, linux_spec):
    from openqvm.core.models import SnapshotRecord

    store.save_spec(linux_spec)
    records = [SnapshotRecord(name="snap1", description="first", includes_memory=False)]
    store.save_snapshots(linux_spec.id, records)
    assert store.load_snapshots(linux_spec.id) == records


def test_quarantine_moves_directory(store, linux_spec):
    store.save_spec(linux_spec)
    destination = store.quarantine_vm(linux_spec.id)
    assert destination.is_dir()
    assert not store.paths.vm_dir(linux_spec.id).exists()
    with pytest.raises(NotFoundError):
        store.load_spec(linux_spec.id)


def test_unknown_vm_raises(store):
    with pytest.raises(NotFoundError):
        store.load_spec("6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11")


def test_settings_tolerate_unknown_fields(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "light", "future_option": 42}), encoding="utf-8")
    settings = SettingsStore(path).load()
    assert settings.theme == "light"
    store = SettingsStore(path)
    store.save(AppSettings(theme="dark", qemu_dir=None))
    assert SettingsStore(path).load().theme == "dark"


def test_settings_invalid_theme_falls_back(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"theme": "neon"}), encoding="utf-8")
    assert SettingsStore(path).load().theme == "dark"
