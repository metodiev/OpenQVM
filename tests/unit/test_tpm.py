"""TPM 2.0 emulator supervision (swtpm process lifecycle).

The stub below stands in for swtpm: it parses the ``--server`` argument and
listens on the same TCP port, which lets the tests verify the real start, readiness
wait and stop logic without a TPM implementation on the test host.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from openqvm.core.errors import OperationFailedError
from openqvm.vm.qemu.tpm import TpmEmulator

STUB = """
import socket, sys
server = [arg for arg in sys.argv[1:] if arg.startswith("type=tcp")][0]
port = int(server.split("port=")[1].split(",")[0])
with socket.socket() as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", port))
    sock.listen(5)
    while True:
        conn, _ = sock.accept()
        conn.close()
"""


def _stub_command(tmp_path: Path) -> list[str]:
    script = tmp_path / "stub_swtpm.py"
    script.write_text(STUB, encoding="utf-8")
    return [sys.executable, str(script)]


def test_argv_uses_the_state_dir_and_requested_port(tmp_path):
    emulator = TpmEmulator(["swtpm"], tmp_path / "tpm")
    argv = emulator.argv(9999)
    assert argv[:2] == ["swtpm", "socket"]
    assert "--tpm2" in argv
    assert f"dir={tmp_path / 'tpm'}" in argv
    assert "type=tcp,port=9999,bindaddr=127.0.0.1" in argv
    assert "not-need-init,startup-clear" in argv


def test_emulator_starts_listens_and_stops(tmp_path):
    emulator = TpmEmulator(_stub_command(tmp_path), tmp_path / "tpm", startup_timeout=20)
    session = emulator.start()
    try:
        assert session.port > 0
        assert emulator.session_file.is_file()
        with socket.create_connection(("127.0.0.1", session.port), timeout=2):
            pass
    finally:
        emulator.stop()

    assert not emulator.session_file.exists()
    assert not psutil.pid_exists(session.pid) or not psutil.Process(session.pid).is_running()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", session.port), timeout=0.5)


def test_start_reaps_a_leftover_emulator(tmp_path):
    state_dir = tmp_path / "tpm"
    state_dir.mkdir()
    leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    create_time = psutil.Process(leftover.pid).create_time()
    emulator = TpmEmulator(_stub_command(tmp_path), state_dir, startup_timeout=20)
    emulator.session_file.write_text(
        json.dumps({"pid": leftover.pid, "create_time": create_time, "port": 1}), encoding="utf-8"
    )

    session = emulator.start()
    try:
        assert not psutil.pid_exists(leftover.pid) or not psutil.Process(leftover.pid).is_running()
        assert session.port != 1
    finally:
        emulator.stop()
        leftover.kill()


def test_start_fails_when_the_emulator_never_listens(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(60)"]
    emulator = TpmEmulator(command, tmp_path / "tpm", startup_timeout=1.5)

    started = time.monotonic()
    with pytest.raises(OperationFailedError, match="did not start listening"):
        emulator.start()

    assert time.monotonic() - started < 30
    assert not emulator.session_file.exists()


def test_stop_leftover_reports_whether_an_emulator_was_running(tmp_path):
    state_dir = tmp_path / "tpm"
    state_dir.mkdir()
    assert TpmEmulator.stop_leftover(state_dir) is False

    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    create_time = psutil.Process(sleeper.pid).create_time()
    (state_dir / "session.json").write_text(
        json.dumps({"pid": sleeper.pid, "create_time": create_time, "port": 1}), encoding="utf-8"
    )
    try:
        assert TpmEmulator.stop_leftover(state_dir) is True
        assert not psutil.pid_exists(sleeper.pid) or not psutil.Process(sleeper.pid).is_running()
        assert not (state_dir / "session.json").exists()
    finally:
        sleeper.kill()
