"""Scheduled VM backups.

A backup is an ``.oqv`` export written to the backups directory and recorded in
``<vm>/backups.json``.  Backups are taken while the VM is stopped so the exported
disks are consistent; the scheduler skips running VMs instead of failing and keeps
a bounded number of packages per VM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from openqvm.core.errors import ValidationError
from openqvm.core.models import (
    DEFAULT_BACKUP_INTERVAL_HOURS,
    DEFAULT_BACKUP_KEEP,
    MAX_BACKUP_INTERVAL_HOURS,
    MAX_BACKUP_KEEP,
    BackupEntry,
)
from openqvm.core.paths import AppPaths
from openqvm.store import AppSettings

BACKUP_SUFFIX = ".oqv"
DEFAULT_INTERVAL_HOURS = DEFAULT_BACKUP_INTERVAL_HOURS
DEFAULT_KEEP = DEFAULT_BACKUP_KEEP
MAX_KEEP = MAX_BACKUP_KEEP
MAX_INTERVAL_HOURS = MAX_BACKUP_INTERVAL_HOURS

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    cleaned = _SLUG_RE.sub("-", name).strip("-._")
    return cleaned[:48] or "vm"


@dataclass(frozen=True)
class BackupPolicy:
    """When and how many backups to keep."""

    directory: Path
    enabled: bool = False
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    keep: int = DEFAULT_KEEP

    def __post_init__(self) -> None:
        if isinstance(self.interval_hours, bool) or not isinstance(self.interval_hours, int):
            raise ValidationError("The backup interval must be a whole number of hours.")
        if not 1 <= self.interval_hours <= MAX_INTERVAL_HOURS:
            raise ValidationError(
                f"The backup interval must be between 1 and {MAX_INTERVAL_HOURS} hours."
            )
        if isinstance(self.keep, bool) or not isinstance(self.keep, int):
            raise ValidationError("The number of backups to keep must be a whole number.")
        if not 1 <= self.keep <= MAX_KEEP:
            raise ValidationError(f"Between 1 and {MAX_KEEP} backups can be kept per VM.")

    @classmethod
    def from_settings(cls, settings: AppSettings, paths: AppPaths) -> BackupPolicy:
        directory = (
            Path(settings.backup_dir).expanduser() if settings.backup_dir else paths.backups_dir
        )
        return cls(
            directory=directory,
            enabled=settings.backup_enabled,
            interval_hours=settings.backup_interval_hours,
            keep=settings.backup_keep,
        )

    @property
    def interval(self) -> timedelta:
        return timedelta(hours=self.interval_hours)


def parse_timestamp(value: str) -> datetime | None:
    """Parse an ISO timestamp, returning ``None`` when it is not usable."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def latest_backup(entries: list[BackupEntry]) -> BackupEntry | None:
    dated = [(parse_timestamp(entry.created_at), entry) for entry in entries]
    known = [(stamp, entry) for stamp, entry in dated if stamp is not None]
    if not known:
        return entries[-1] if entries else None
    return max(known, key=lambda item: item[0])[1]


def is_due(entries: list[BackupEntry], policy: BackupPolicy, now: datetime) -> bool:
    """True when the VM has no backup or its newest backup is older than the interval."""

    newest = latest_backup(entries)
    if newest is None:
        return True
    stamp = parse_timestamp(newest.created_at)
    if stamp is None:
        return True
    return now - stamp >= policy.interval


def backups_to_prune(entries: list[BackupEntry], keep: int) -> list[BackupEntry]:
    """Return the oldest entries that fall outside the retention window."""

    ordered = sorted(entries, key=lambda entry: entry.created_at, reverse=True)
    return ordered[max(1, keep) :]


def backup_filename(name: str, vm_id: str, moment: datetime) -> str:
    stamp = moment.astimezone(UTC).strftime(_STAMP_FORMAT)
    return f"{_slug(name)}-{vm_id[:8]}-{stamp}{BACKUP_SUFFIX}"


def unique_backup_path(directory: Path, name: str, vm_id: str, moment: datetime) -> Path:
    """Pick a package path that does not collide with an existing file."""

    directory = Path(directory)
    base = backup_filename(name, vm_id, moment)[: -len(BACKUP_SUFFIX)]
    candidate = directory / f"{base}{BACKUP_SUFFIX}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{base}-{counter}{BACKUP_SUFFIX}"
        counter += 1
    return candidate


@dataclass
class BackupResult:
    entry: BackupEntry
    pruned: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return Path(self.entry.path)


@dataclass
class BackupRun:
    """Outcome of one scheduled backup pass."""

    results: list[BackupResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def created(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict[str, object]:
        return {
            "created": [result.entry.to_dict() for result in self.results],
            "errors": list(self.errors),
            "skipped": list(self.skipped),
        }
