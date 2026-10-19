"""Application composition root shared by the GUI and CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import OperationFailedError
from openqvm.core.logging import get_logger, setup_logging
from openqvm.core.paths import AppPaths
from openqvm.platform.capabilities import PlatformCapabilities, detect_capabilities
from openqvm.services.metrics import MetricsService
from openqvm.services.operations import OperationLog
from openqvm.store import AppSettings, SettingsStore, VmStore
from openqvm.vm.manager import VmManager

log = get_logger("app.context")


@dataclass
class AppContext:
    paths: AppPaths
    settings_store: SettingsStore
    settings: AppSettings
    capabilities: PlatformCapabilities
    store: VmStore
    operations: OperationLog
    manager: VmManager
    metrics: MetricsService

    @classmethod
    def create(
        cls,
        *,
        data_dir: Path | None = None,
        qemu_dir: str | None = None,
    ) -> AppContext:
        paths = AppPaths(Path(data_dir).expanduser().resolve()) if data_dir else AppPaths.default()
        try:
            paths.ensure()
            setup_logging(paths.app_log, console=False)
        except OSError as exc:
            raise OperationFailedError(
                f"The OpenQVM data directory is not usable: {paths.root}",
                hint="Choose a writable data directory or fix its permissions.",
                cause=exc,
            ) from exc
        settings_store = SettingsStore(paths.settings_file)
        settings = settings_store.load()
        capabilities = detect_capabilities(qemu_dir=qemu_dir or settings.qemu_dir)
        store = VmStore(paths)
        operations = OperationLog(paths.operations_log)
        manager = VmManager(paths, capabilities, store=store, operations=operations)
        manager.reconcile()
        context = cls(
            paths=paths,
            settings_store=settings_store,
            settings=settings,
            capabilities=capabilities,
            store=store,
            operations=operations,
            manager=manager,
            metrics=MetricsService(paths.root),
        )
        log.info(
            "OpenQVM ready: data=%s, backend=%s, accelerator=%s",
            paths.root,
            capabilities.qemu_version or "unavailable",
            capabilities.default_accelerator or "none",
        )
        return context

    def save_settings(self, settings: AppSettings) -> None:
        self.settings_store.save(settings)
        self.settings = settings

    def update_settings(self, **changes) -> AppSettings:
        updated = AppSettings(
            theme=changes.get("theme", self.settings.theme),
            qemu_dir=changes.get("qemu_dir", self.settings.qemu_dir),
            console_fit=changes.get("console_fit", self.settings.console_fit),
            last_page=changes.get("last_page", self.settings.last_page),
            schema_version=self.settings.schema_version,
        )
        self.save_settings(updated)
        return updated

    def redetect(self, *, qemu_dir: str | None = None) -> PlatformCapabilities:
        capabilities = detect_capabilities(qemu_dir=qemu_dir if qemu_dir is not None else self.settings.qemu_dir)
        self.capabilities = capabilities
        self.manager.reconfigure(capabilities)
        return capabilities

    def close(self) -> None:
        """Release background resources (no VM is stopped by design)."""

        log.info("OpenQVM application context closed")
