"""ScrollSource: debounced accumulation of pynput scroll events.

We can't drive pynput directly in a unit test (it needs an X/Win32
display), so the tests poke ``_on_scroll`` to simulate the listener
callback and assert the source emits the right cumulative events.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import inscription.capture.scroll_source as mod
from inscription.capture.scroll_source import ScrollSource, _describe
from inscription.model import EventKind

if TYPE_CHECKING:
    from inscription.capture import CaptureEngine, RawCaptureEvent


class _FakeEngine:
    """Captures whatever ``submit`` receives."""

    def __init__(self) -> None:
        self.events: list[RawCaptureEvent] = []
        self._lock = threading.Lock()

    def submit(self, event: RawCaptureEvent) -> bool:
        with self._lock:
            self.events.append(event)
        return True


def _wait_for(predicate, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("predicate never became true")


# ----------------------------------------------------------- describe


def test_describe_down() -> None:
    assert _describe(0, -5) == "down 5"


def test_describe_up() -> None:
    assert _describe(0, 3) == "up 3"


def test_describe_diagonal() -> None:
    assert _describe(2, -4) == "down 4, right 2"


def test_describe_zero() -> None:
    assert _describe(0, 0) == "0"


# ----------------------------------------------------------- accumulation


def test_burst_collapses_into_one_event() -> None:
    engine = _FakeEngine()
    src = ScrollSource(debounce_s=0.05)
    src._engine = cast("CaptureEngine", engine)  # skip the listener
    for _ in range(7):
        src._on_scroll(100, 200, 0, -1)

    _wait_for(lambda: len(engine.events) == 1, timeout=2.0)

    assert len(engine.events) == 1
    event = engine.events[0]
    assert event.kind is EventKind.SCROLL
    assert event.text == "down 7"
    assert event.x == 100
    assert event.y == 200
    assert event.png_bytes is None  # scroll events carry no screenshot


def test_pause_emits_separate_events() -> None:
    engine = _FakeEngine()
    src = ScrollSource(debounce_s=0.05)
    src._engine = cast("CaptureEngine", engine)

    for _ in range(3):
        src._on_scroll(0, 0, 0, -1)
    _wait_for(lambda: len(engine.events) == 1, timeout=2.0)

    for _ in range(2):
        src._on_scroll(0, 0, 0, 1)
    _wait_for(lambda: len(engine.events) == 2, timeout=2.0)

    assert engine.events[0].text == "down 3"
    assert engine.events[1].text == "up 2"


def test_stop_flushes_pending_burst() -> None:
    engine = _FakeEngine()
    src = ScrollSource(debounce_s=10.0)  # never fires from the timer
    src._engine = cast("CaptureEngine", engine)
    src._on_scroll(0, 0, 0, -2)
    src._on_scroll(0, 0, 0, -3)
    # Engine isn't called yet — the timer hasn't elapsed.
    assert engine.events == []

    src.stop()
    assert len(engine.events) == 1
    assert engine.events[0].text == "down 5"


def test_stop_with_no_pending_burst_emits_nothing() -> None:
    engine = _FakeEngine()
    src = ScrollSource(debounce_s=0.05)
    src._engine = cast("CaptureEngine", engine)
    src.stop()
    assert engine.events == []


def test_scroll_event_is_stamped_at_burst_start(monkeypatch) -> None:
    """The flushed SCROLL event carries the FIRST tick's timestamp.

    Regression: it was stamped at flush time -- up to debounce_s
    after scrolling stopped -- so a click landing in the quiet window
    was timestamped BEFORE the scroll that physically preceded it,
    inverting the exhibit's timeline.
    """
    src = mod.ScrollSource(debounce_s=999)  # never auto-flush
    submitted = []

    class _Engine:
        def submit(self, ev):
            submitted.append(ev)
            return True

    src._engine = _Engine()

    times = iter(
        [
            datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),   # first tick
            datetime(2026, 1, 1, 12, 0, 5, tzinfo=UTC),   # flush-time fallback (unused)
        ]
    )
    monkeypatch.setattr(mod, "utcnow", lambda: next(times))

    src._on_scroll(10, 10, 0, -1)
    src._on_scroll(10, 12, 0, -1)  # same burst; no new stamp
    with src._lock:
        src._flush_locked()

    assert len(submitted) == 1
    assert submitted[0].occurred_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
