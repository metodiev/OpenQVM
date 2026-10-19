"""Minimal, synchronous QMP (QEMU Machine Protocol) client.

QMP is reachable only on the loopback interface with an ephemeral port.  The
client is intentionally small: it performs the capabilities handshake, runs
commands and exposes the human monitor for operations that have no stable QMP
equivalent (snapshot management).
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from typing import Any

from openqvm.core.errors import BackendError, OperationFailedError, TimeoutError_
from openqvm.core.logging import get_logger

log = get_logger("qemu.qmp")


class QmpCommandError(BackendError):
    def __init__(self, command: str, qmp_class: str, description: str) -> None:
        super().__init__(
            f"QEMU rejected the '{command}' command: {description}",
            hint="Inspect the QEMU log for details.",
            details={"command": command, "qmp_class": qmp_class, "description": description},
        )
        self.command = command
        self.qmp_class = qmp_class


@dataclass(frozen=True)
class QmpEvent:
    name: str
    data: dict[str, Any]


class QmpClient:
    def __init__(self, host: str, port: int, *, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = b""
        self.events: list[QmpEvent] = []

    def __enter__(self) -> QmpClient:
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.close()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def connect(self, *, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=min(2.0, self.timeout))
                sock.settimeout(min(2.0, self.timeout))
                self._socket = sock
                self._buffer = b""
                greeting = self._read_message(timeout=self.timeout)
                if "QMP" not in greeting:
                    self.close()
                    raise BackendError("The QMP endpoint did not send a greeting.")
                self.execute("qmp_capabilities", timeout=self.timeout)
                sock.settimeout(0.5)
                return
            except (OSError, BackendError) as exc:
                last_error = exc
                self.close()
                time.sleep(0.2)
        raise BackendError(
            "Could not connect to the QEMU control interface.",
            hint="The VM may have failed to start. Check the QEMU log.",
            details={"host": self.host, "port": self.port},
            cause=last_error,
        )

    def execute(self, command: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        if self._socket is None:
            raise BackendError("The QMP client is not connected.")
        payload: dict[str, Any] = {"execute": command}
        if arguments:
            payload["arguments"] = arguments
        try:
            self._socket.sendall(json.dumps(payload).encode("utf-8") + b"\n")
        except OSError as exc:
            raise BackendError(
                "The QEMU control connection was lost.",
                details={"command": command},
                cause=exc,
            ) from exc
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError_(
                    f"QEMU did not answer the '{command}' command in time.",
                    hint="Check whether the VM is still responsive.",
                )
            message = self._read_message(timeout=remaining)
            if "event" in message:
                self.events.append(QmpEvent(str(message["event"]), message.get("data") or {}))
                continue
            if "error" in message:
                error = message["error"] or {}
                raise QmpCommandError(
                    command,
                    str(error.get("class", "GenericError")),
                    str(error.get("desc", "unknown error")),
                )
            if "return" in message:
                return message["return"]
            log.debug("Ignoring unexpected QMP message: %s", message)

    def _read_message(self, *, timeout: float) -> dict[str, Any]:
        if self._socket is None:
            raise BackendError("The QMP client is not connected.")
        deadline = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError_("Timed out waiting for a QMP response.")
            self._socket.settimeout(max(0.05, min(0.5, remaining)))
            try:
                chunk = self._socket.recv(65536)
            except TimeoutError:
                continue
            except OSError as exc:
                raise BackendError("The QEMU control connection was lost.", cause=exc) from exc
            if not chunk:
                raise BackendError(
                    "QEMU closed the control connection.",
                    hint="The VM process may have exited. Check the QEMU log.",
                )
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise OperationFailedError("QEMU sent an invalid control message.", cause=exc) from exc
        if not isinstance(message, dict):
            raise OperationFailedError("QEMU sent an invalid control message.")
        return message

    # -- convenience commands -------------------------------------------------

    def query_status(self) -> str:
        result = self.execute("query-status")
        if not isinstance(result, dict):
            raise OperationFailedError("QEMU returned an invalid status response.")
        return str(result.get("status", "unknown"))

    def query_name(self) -> str | None:
        result = self.execute("query-name")
        if isinstance(result, dict):
            return result.get("name")
        return None

    def stop(self) -> None:
        self.execute("stop")

    def cont(self) -> None:
        self.execute("cont")

    def system_powerdown(self) -> None:
        self.execute("system_powerdown")

    def quit(self) -> None:
        try:
            self.execute("quit", timeout=2.0)
        except (BackendError, TimeoutError_):
            # QEMU exits without replying; a closed connection is expected here.
            pass

    def human_monitor(self, command_line: str) -> str:
        result = self.execute("human-monitor-command", {"command-line": command_line})
        return str(result) if result is not None else ""

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    def __del__(self) -> None:
        self.close()
