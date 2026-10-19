"""TPM 2.0 emulator supervision (``swtpm``).

QEMU's ``tpmdev emulator`` backend speaks the TPM protocol to an external swtpm
process over a socket.  OpenQVM starts swtpm on a random loopback port before
QEMU, waits until it accepts connections, and stops it when the VM stops.  A small
session file records the emulator identity so a later start can clean up an
emulator left behind by a crashed OpenQVM process.

The TPM state files live in ``<vm>/tpm`` and are not part of exports or backups;
importing a VM therefore starts with a fresh TPM.
"""

from __future__ import annotations

import json
import socket
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from openqvm.core.errors import OperationFailedError
from openqvm.core.logging import get_logger
from openqvm.vm.qemu.process import ProcessInfo, launch_qemu, process_matches, read_log_tail, terminate_process

log = get_logger("qemu.tpm")

TPM_VERSION = "2.0"
_SESSION_FILE = "session.json"


def allocate_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _port_is_open(port: int, *, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class TpmSession:
    pid: int
    create_time: float
    port: int

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "create_time": self.create_time, "port": self.port}

    @classmethod
    def from_dict(cls, data: object) -> TpmSession | None:
        if not isinstance(data, dict):
            return None
        try:
            pid = int(data.get("pid"))  # type: ignore[arg-type]
            create_time = float(data.get("create_time"))  # type: ignore[arg-type]
            port = int(data.get("port"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if pid <= 0 or port <= 0:
            return None
        return cls(pid=pid, create_time=create_time, port=port)


class TpmEmulator:
    """Runs one swtpm instance for one VM."""

    def __init__(
        self,
        command: Sequence[str],
        state_dir: Path,
        *,
        log_path: Path | None = None,
        startup_timeout: float = 15.0,
    ) -> None:
        if not command:
            raise ValueError("The swtpm command must not be empty.")
        self.command = [str(part) for part in command]
        self.state_dir = Path(state_dir)
        self.log_path = Path(log_path) if log_path else self.state_dir / "swtpm.log"
        self.startup_timeout = startup_timeout
        self.session: TpmSession | None = None

    @property
    def session_file(self) -> Path:
        return self.state_dir / _SESSION_FILE

    def argv(self, port: int) -> list[str]:
        return [
            *self.command,
            "socket",
            "--tpm2",
            "--tpmstate",
            f"dir={self.state_dir}",
            "--server",
            f"type=tcp,port={port},bindaddr=127.0.0.1",
            "--flags",
            "not-need-init,startup-clear",
            "--log",
            f"file={self.log_path}",
        ]

    def start(self) -> TpmSession:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._reap_stale_session()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_bytes(b"")
        port = allocate_tcp_port()
        info = launch_qemu(
            self.argv(port), log_path=self.log_path, cwd=self.state_dir, label="swtpm"
        )
        try:
            self._wait_for_port(port, info)
        except Exception:
            terminate_process(info.pid, info.create_time, timeout=5)
            raise
        session = TpmSession(pid=info.pid, create_time=info.create_time, port=port)
        self.session_file.write_text(json.dumps(session.to_dict()), encoding="utf-8")
        self.session = session
        log.info("Started swtpm pid=%s port=%s", info.pid, port)
        return session

    def stop(self) -> None:
        if self.session is not None:
            terminate_process(self.session.pid, self.session.create_time, timeout=10)
            self.session = None
        self.session_file.unlink(missing_ok=True)

    def _wait_for_port(self, port: int, info: ProcessInfo) -> None:
        deadline = time.monotonic() + self.startup_timeout
        while time.monotonic() < deadline:
            if _port_is_open(port):
                return
            if not process_matches(info.pid, info.create_time):
                break
            time.sleep(0.05)
        tail = read_log_tail(self.log_path)
        message = "The TPM emulator (swtpm) did not start listening."
        if tail:
            message += f" swtpm reported: {tail[-600:]}"
        raise OperationFailedError(
            message,
            hint="Verify that swtpm is installed and that this account can write its state directory.",
            details={"port": port, "log": tail[-4000:]},
        )

    def _reap_stale_session(self) -> None:
        try:
            raw = self.session_file.read_text(encoding="utf-8")
        except OSError:
            return
        session = TpmSession.from_dict(json.loads(raw)) if raw.strip() else None
        if session is not None and process_matches(session.pid, session.create_time):
            log.warning("Stopping a leftover TPM emulator pid=%s", session.pid)
            terminate_process(session.pid, session.create_time, timeout=10)
        self.session_file.unlink(missing_ok=True)

    @classmethod
    def stop_leftover(cls, state_dir: Path) -> bool:
        """Stop an emulator recorded by a previous process; returns True if one ran."""

        state_dir = Path(state_dir)
        session_file = state_dir / _SESSION_FILE
        try:
            raw = session_file.read_text(encoding="utf-8")
        except OSError:
            return False
        session = TpmSession.from_dict(json.loads(raw)) if raw.strip() else None
        stopped = False
        if session is not None and process_matches(session.pid, session.create_time):
            stopped = terminate_process(session.pid, session.create_time, timeout=10)
        session_file.unlink(missing_ok=True)
        return stopped
