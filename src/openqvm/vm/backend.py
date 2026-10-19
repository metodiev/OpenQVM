"""Backend abstraction shared by all platform virtualization integrations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from openqvm.core.models import VMRuntime, VMSpec


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    supports_pause: bool
    supports_live_snapshots: bool
    supports_memory_snapshots: bool
    supports_live_iso_change: bool
    supports_online_disk_resize: bool


class VirtualizationBackend(Protocol):
    """Lifecycle operations implemented by a virtualization backend.

    Higher-level operations (create/delete/configure/clone/import/export) are
    implemented by :class:`openqvm.vm.manager.VmManager`, which combines this
    interface with storage and snapshot services.
    """

    name: str
    capabilities: BackendCapabilities

    def start(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime: ...

    def stop(self, spec: VMSpec, runtime: VMRuntime, *, force: bool = False, timeout: float = 30.0) -> VMRuntime: ...

    def pause(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime: ...

    def resume(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime: ...

    def refresh(self, spec: VMSpec, runtime: VMRuntime) -> VMRuntime: ...

    def qmp_client(self, runtime: VMRuntime): ...
