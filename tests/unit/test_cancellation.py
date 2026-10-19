"""Cooperative cancellation: the token itself, subprocess handling and disk copies."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from openqvm.core.cancellation import CancellationToken, cancelled_token
from openqvm.core.errors import CancelledError, ErrorCode
from openqvm.storage.disks import DiskManager
from openqvm.storage.tools import run_tool


class CancelAfter(CancellationToken):
    """Token that cancels itself on the *n*-th check, to cancel exactly mid-operation."""

    def __init__(self, checks: int) -> None:
        super().__init__()
        self.remaining = checks

    def raise_if_cancelled(self, message: str = "The operation was cancelled.") -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self.cancel("cancelled by the test")
        super().raise_if_cancelled(message)


def test_token_starts_active_and_records_the_reason():
    token = CancellationToken()
    assert not token.is_cancelled
    token.raise_if_cancelled()  # no-op while active
    token.cancel("user pressed Cancel")
    assert token.is_cancelled
    assert token.reason == "user pressed Cancel"


def test_raise_if_cancelled_reports_the_cancelled_error_code():
    token = cancelled_token("cancelled before it started")
    with pytest.raises(CancelledError) as excinfo:
        token.raise_if_cancelled("The export was cancelled.")
    assert excinfo.value.code == ErrorCode.CANCELLED
    assert excinfo.value.message == "The export was cancelled."
    assert excinfo.value.hint == "cancelled before it started"


def test_sleep_returns_false_as_soon_as_the_token_is_cancelled():
    token = CancellationToken()
    threading.Timer(0.1, token.cancel).start()
    started = time.monotonic()
    assert token.sleep(30) is False
    assert time.monotonic() - started < 5


def test_sleep_waits_when_the_token_stays_active():
    token = CancellationToken()
    started = time.monotonic()
    assert token.sleep(0.05) is True
    assert time.monotonic() - started >= 0.04


def test_run_tool_terminates_a_cancelled_subprocess():
    token = CancellationToken()
    threading.Timer(0.4, token.cancel).start()
    started = time.monotonic()
    with pytest.raises(CancelledError) as excinfo:
        run_tool(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            timeout=60,
            tool_name="sleeper",
            cancel=token,
        )
    assert excinfo.value.code == ErrorCode.CANCELLED
    assert "sleeper" in excinfo.value.message
    assert time.monotonic() - started < 10


def test_run_tool_without_a_token_still_returns_output():
    result = run_tool([sys.executable, "-c", "print('hello')"], timeout=60, tool_name="python")
    assert result.ok
    assert result.stdout.strip() == "hello"


def test_run_tool_rejects_a_pre_cancelled_token_without_starting_the_tool():
    with pytest.raises(CancelledError, match="cancelled before it started"):
        run_tool([sys.executable, "-c", "print('never')"], cancel=cancelled_token())


def test_disk_copy_removes_the_partial_file_when_cancelled(tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"x" * (10 * 1024 * 1024))
    destination = tmp_path / "copy.qcow2"
    disks = DiskManager(None)

    with pytest.raises(CancelledError):
        disks.copy(source, destination, cancel=CancelAfter(2))

    assert not destination.exists()
    assert source.is_file()


def test_disk_copy_keeps_the_file_when_it_finishes(tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"x" * 1024)
    destination = tmp_path / "copy.qcow2"
    disks = DiskManager(None)

    disks.copy(source, destination, cancel=CancellationToken())

    assert destination.read_bytes() == source.read_bytes()


def test_pre_cancelled_copy_does_not_create_the_destination(tmp_path):
    source = tmp_path / "source.qcow2"
    source.write_bytes(b"x" * 1024)
    destination = tmp_path / "copy.qcow2"

    with pytest.raises(CancelledError):
        DiskManager(None).copy(source, destination, cancel=cancelled_token())

    assert not Path(destination).exists()
