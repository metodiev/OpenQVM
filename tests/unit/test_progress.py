"""Progress reporting helpers."""

from __future__ import annotations

from openqvm.core.progress import ByteProgress, report_progress


def test_report_progress_is_a_no_op_without_a_callback():
    report_progress(None, 0.5, "half way")  # must not raise


def test_report_progress_passes_none_through():
    calls: list[tuple[float | None, str]] = []
    report_progress(lambda fraction, message: calls.append((fraction, message)), None, "unknown")
    assert calls == [(None, "unknown")]


def test_report_progress_clamps_out_of_range_values():
    calls: list[tuple[float | None, str]] = []
    collector = lambda fraction, message: calls.append((fraction, message))  # noqa: E731
    report_progress(collector, -3.0)
    report_progress(collector, 7.5)
    assert calls == [(0.0, ""), (1.0, "")]


def test_byte_progress_accumulates_and_reports_messages():
    calls: list[tuple[float | None, str]] = []
    tracker = ByteProgress(lambda fraction, message: calls.append((fraction, message)), 100, message="Copying")
    tracker.advance(25)
    tracker.advance(25, "Verifying")
    assert calls == [(0.25, "Copying"), (0.5, "Verifying")]


def test_byte_progress_never_exceeds_one_and_can_finish():
    calls: list[tuple[float | None, str]] = []
    tracker = ByteProgress(lambda fraction, message: calls.append((fraction, message)), 10)
    tracker.advance(40)
    assert calls[-1] == (1.0, "")
    tracker = ByteProgress(lambda fraction, message: calls.append((fraction, message)), 10)
    tracker.advance(1)
    tracker.finish("Done")
    assert calls[-1] == (1.0, "Done")


def test_byte_progress_handles_an_empty_payload():
    calls: list[tuple[float | None, str]] = []
    tracker = ByteProgress(lambda fraction, message: calls.append((fraction, message)), 0)
    tracker.advance(0)
    assert calls == [(0.0, "")]

def test_cli_progress_line_renders_on_a_terminal_and_clears(monkeypatch):
    import sys

    from openqvm.cli import _ProgressLine
    from openqvm.core.events import TOPIC_OPERATION_PROGRESS, EventBus, OperationProgress

    class FakeTerminal:
        def __init__(self) -> None:
            self.text = ""

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> None:
            self.text += text

        def flush(self) -> None:
            pass

    class FakeManager:
        events = EventBus()

    terminal = FakeTerminal()
    monkeypatch.setattr(sys, "stderr", terminal)

    manager = FakeManager()
    line = _ProgressLine(manager, enabled=True)
    manager.events.publish(
        TOPIC_OPERATION_PROGRESS,
        OperationProgress(action="Export VM", fraction=0.5, message="Writing disk.qcow2"),
    )
    assert "Writing disk.qcow2 — 50 %" in terminal.text

    line.close()
    assert terminal.text.endswith("\r")
    assert manager.events.subscriber_count(TOPIC_OPERATION_PROGRESS) == 0


def test_cli_progress_line_is_silent_without_a_terminal(monkeypatch):
    import sys

    from openqvm.cli import _ProgressLine
    from openqvm.core.events import TOPIC_OPERATION_PROGRESS, EventBus, OperationProgress

    class FakePipe:
        text = ""

        def isatty(self) -> bool:
            return False

        def write(self, value: str) -> None:
            self.text += value

        def flush(self) -> None:
            pass

    class FakeManager:
        events = EventBus()

    pipe = FakePipe()
    monkeypatch.setattr(sys, "stderr", pipe)
    manager = FakeManager()
    line = _ProgressLine(manager, enabled=True)
    manager.events.publish(TOPIC_OPERATION_PROGRESS, OperationProgress(action="Export VM", fraction=0.5))
    line.close()

    assert pipe.text == ""
    assert manager.events.subscriber_count(TOPIC_OPERATION_PROGRESS) == 0
