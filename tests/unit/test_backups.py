"""Backup policy, retention and naming rules."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from openqvm.core.errors import ValidationError
from openqvm.core.models import BackupEntry
from openqvm.core.paths import AppPaths
from openqvm.services.backups import (
    BackupPolicy,
    backup_filename,
    backups_to_prune,
    is_due,
    latest_backup,
    parse_timestamp,
    unique_backup_path,
)
from openqvm.store import AppSettings


def _entry(created_at: str, path: str = "backup.oqv") -> BackupEntry:
    return BackupEntry(
        path=path,
        vm_id="6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11",
        vm_name="Test VM",
        created_at=created_at,
        size_bytes=10,
        sha256="0" * 64,
    )


def test_policy_rejects_invalid_intervals_and_retention(tmp_path):
    with pytest.raises(ValidationError, match="interval"):
        BackupPolicy(directory=tmp_path, interval_hours=0)
    with pytest.raises(ValidationError, match="interval"):
        BackupPolicy(directory=tmp_path, interval_hours=24 * 400)
    with pytest.raises(ValidationError, match="backups"):
        BackupPolicy(directory=tmp_path, keep=0)
    with pytest.raises(ValidationError, match="backups"):
        BackupPolicy(directory=tmp_path, keep=1000)
    with pytest.raises(ValidationError, match="interval"):
        BackupPolicy(directory=tmp_path, interval_hours=True)


def test_policy_uses_the_backups_directory_by_default(tmp_path):
    paths = AppPaths(tmp_path / "data")
    policy = BackupPolicy.from_settings(AppSettings(), paths)
    assert policy.directory == paths.backups_dir
    assert not policy.enabled
    assert policy.interval == timedelta(hours=24)

    custom = BackupPolicy.from_settings(
        AppSettings(backup_enabled=True, backup_dir=str(tmp_path / "nas"), backup_keep=7), paths
    )
    assert custom.enabled
    assert custom.directory == tmp_path / "nas"
    assert custom.keep == 7


def test_settings_reject_out_of_range_backup_values():
    with pytest.raises(ValidationError, match="interval"):
        AppSettings(backup_interval_hours=0)
    with pytest.raises(ValidationError, match="backups"):
        AppSettings(backup_keep=0)


def test_is_due_without_history_or_after_the_interval(tmp_path):
    policy = BackupPolicy(directory=tmp_path, interval_hours=12)
    now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    assert is_due([], policy, now)
    recent = [_entry((now - timedelta(hours=6)).isoformat())]
    assert not is_due(recent, policy, now)
    stale = [_entry((now - timedelta(hours=13)).isoformat())]
    assert is_due(stale, policy, now)
    assert is_due([_entry("not a timestamp")], policy, now)


def test_latest_backup_ignores_unparsable_timestamps(tmp_path):
    older = _entry("2026-10-01T00:00:00+00:00", "older.oqv")
    newer = _entry("2026-10-18T00:00:00+00:00", "newer.oqv")
    broken = _entry("broken", "broken.oqv")
    assert latest_backup([older, newer, broken]) is newer
    assert latest_backup([]) is None


def test_backups_to_prune_keeps_the_newest_entries():
    entries = [
        _entry(f"2026-10-{day:02d}T00:00:00+00:00", f"backup-{day}.oqv") for day in (1, 2, 3, 4)
    ]
    pruned = backups_to_prune(entries, keep=2)
    assert [entry.filename for entry in pruned] == ["backup-2.oqv", "backup-1.oqv"]
    assert backups_to_prune(entries, keep=10) == []


def test_backup_filename_is_slugged_and_timestamped():
    moment = datetime(2026, 10, 19, 20, 30, 5, tzinfo=UTC)
    filename = backup_filename("My VM: / test", "6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", moment)
    assert filename == "My-VM-test-6f0c6b5a-20261019-203005.oqv"
    assert backup_filename("   ", "6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11", moment).startswith("vm-")


def test_unique_backup_path_counts_collisions(tmp_path):
    moment = datetime(2026, 10, 19, 20, 30, 5, tzinfo=UTC)
    vm_id = "6f0c6b5a-5e80-4f88-9c0b-2a1b8f1e5c11"
    first = unique_backup_path(tmp_path, "VM", vm_id, moment)
    assert first.name == "VM-6f0c6b5a-20261019-203005.oqv"
    first.write_bytes(b"x")
    second = unique_backup_path(tmp_path, "VM", vm_id, moment)
    assert second.name == "VM-6f0c6b5a-20261019-203005-2.oqv"


def test_parse_timestamp_handles_offsets_and_naive_values():
    assert parse_timestamp("2026-10-19T20:00:00Z") == datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
    assert parse_timestamp("2026-10-19T20:00:00") == datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
    assert parse_timestamp("2026-10-19T22:00:00+02:00") == datetime(2026, 10, 19, 20, 0, tzinfo=UTC)
    assert parse_timestamp("nonsense") is None


def test_backup_entry_round_trip():
    entry = _entry("2026-10-19T20:00:00+00:00", "backup.oqv")
    restored = BackupEntry.from_dict(entry.to_dict())
    assert restored == entry
    with pytest.raises(ValidationError):
        BackupEntry.from_dict({**entry.to_dict(), "size_bytes": -1})
    with pytest.raises(ValidationError):
        BackupEntry.from_dict({**entry.to_dict(), "path": ""})
