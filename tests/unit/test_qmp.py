"""QMP handshake, commands, events and error handling."""

from __future__ import annotations

import json
import socket
import threading

import pytest

from openqvm.core.errors import BackendError
from openqvm.vm.qemu.qmp import QmpClient, QmpCommandError


class FakeQmpServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.commands: list[dict] = []
        self.error: Exception | None = None

    def run(self) -> None:
        try:
            connection, _ = self.socket.accept()
            with connection:
                handle = connection.makefile("rwb")
                handle.write(b'{"QMP": {"version": {"qemu": {"major": 11}}, "capabilities": []}}\n')
                handle.flush()
                while True:
                    line = handle.readline()
                    if not line:
                        return
                    request = json.loads(line)
                    self.commands.append(request)
                    command = request.get("execute")
                    if command == "qmp_capabilities":
                        response = {"return": {}}
                    elif command == "query-status":
                        # Interleave a real QEMU-style event before the response.
                        handle.write(b'{"event": "RESET", "data": {}}\n')
                        handle.flush()
                        response = {"return": {"status": "running"}}
                    elif command == "human-monitor-command":
                        response = {"return": "ok\r\n"}
                    elif command == "explode":
                        response = {"error": {"class": "GenericError", "desc": "boom"}}
                    else:
                        response = {"return": None}
                    handle.write((json.dumps(response) + "\n").encode())
                    handle.flush()
        except Exception as exc:  # pragma: no cover - test helper
            self.error = exc

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass


@pytest.fixture
def server():
    instance = FakeQmpServer()
    instance.start()
    yield instance
    instance.close()
    instance.join(timeout=2)
    assert instance.error is None


def test_handshake_and_commands(server):
    client = QmpClient("127.0.0.1", server.port, timeout=5)
    client.connect()
    try:
        assert client.query_status() == "running"
        assert client.human_monitor("info block") == "ok\r\n"
        assert client.events and client.events[0].name == "RESET"
    finally:
        client.close()
    assert server.commands[0]["execute"] == "qmp_capabilities"
    assert any(command.get("execute") == "human-monitor-command" for command in server.commands)


def test_qmp_error_is_typed(server):
    client = QmpClient("127.0.0.1", server.port, timeout=5)
    client.connect()
    try:
        with pytest.raises(QmpCommandError, match="boom"):
            client.execute("explode")
    finally:
        client.close()


def test_connect_failure_is_actionable():
    client = QmpClient("127.0.0.1", 1, timeout=0.3)
    with pytest.raises(BackendError, match="control interface"):
        client.connect()
