"""Host and per-VM metrics.

All values come from ``psutil``.  When a value cannot be measured the metric is
reported as ``None`` and the UI must label it as unavailable instead of
inventing a number.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a hard dependency in practice
    psutil = None  # type: ignore[assignment]


@dataclass(frozen=True)
class VmMetrics:
    vm_id: str
    cpu_percent: float | None
    memory_rss_bytes: int | None
    process_count: int | None


@dataclass
class HostMetricsSnapshot:
    cpu_percent: float | None = None
    cpu_count: int | None = None
    memory_used_bytes: int | None = None
    memory_total_bytes: int | None = None
    disk_used_bytes: int | None = None
    disk_total_bytes: int | None = None
    qemu_process_count: int | None = None
    unavailable_reason: str | None = None
    per_vm: dict[str, VmMetrics] = field(default_factory=dict)


class MetricsService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self._cpu_samples: dict[int, tuple[float, object]] = {}

    @property
    def available(self) -> bool:
        return psutil is not None

    def snapshot(self, runtimes: list[tuple[str, int | None, float | None]]) -> HostMetricsSnapshot:
        snapshot = HostMetricsSnapshot()
        if psutil is None:
            snapshot.unavailable_reason = "psutil is not installed"
            return snapshot
        try:
            snapshot.cpu_percent = psutil.cpu_percent(interval=None)
            snapshot.cpu_count = psutil.cpu_count(logical=True)
            memory = psutil.virtual_memory()
            snapshot.memory_used_bytes = int(memory.total - memory.available)
            snapshot.memory_total_bytes = int(memory.total)
        except Exception as exc:  # pragma: no cover - defensive
            snapshot.unavailable_reason = f"Host metrics unavailable: {exc}"

        try:
            usage = psutil.disk_usage(str(self.data_dir))
            snapshot.disk_used_bytes = int(usage.used)
            snapshot.disk_total_bytes = int(usage.total)
        except OSError:
            pass

        qemu_count = 0
        for vm_id, pid, expected_create_time in runtimes:
            metrics = self._process_metrics(vm_id, pid, expected_create_time)
            if metrics is not None:
                snapshot.per_vm[vm_id] = metrics
                if metrics.process_count:
                    qemu_count += metrics.process_count
        snapshot.qemu_process_count = qemu_count if snapshot.per_vm else 0
        return snapshot

    def _process_metrics(self, vm_id: str, pid: int | None, expected_create_time: float | None) -> VmMetrics | None:
        if pid is None or psutil is None:
            return None
        try:
            process = psutil.Process(pid)
            create_time = process.create_time()
            if expected_create_time is not None and abs(create_time - expected_create_time) > 2.0:
                return VmMetrics(vm_id, None, None, None)
            if expected_create_time is None:
                name = (process.name() or "").lower()
                if "qemu" not in name:
                    return VmMetrics(vm_id, None, None, None)
            cpu = process.cpu_percent(interval=None)
            rss = process.memory_info().rss
            return VmMetrics(vm_id, round(cpu, 1), int(rss), 1)
        except Exception:
            return VmMetrics(vm_id, None, None, None)

    def qemu_process_count(self) -> int | None:
        if psutil is None:
            return None
        count = 0
        for process in psutil.process_iter(["name"]):
            try:
                name = (process.info.get("name") or "").lower()
            except Exception:
                continue
            if name.startswith("qemu-system"):
                count += 1
        return count


def human_bytes(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def host_memory_total_mib() -> int:
    if psutil is None:
        return 0
    try:
        return int(psutil.virtual_memory().total // (1024 * 1024))
    except Exception:
        return 0
