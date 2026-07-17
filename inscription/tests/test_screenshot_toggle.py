"""Auto-screenshot toggle.

Verifies that ``ClickSource(auto_screenshot=False)`` and
``WindowFocusSource(auto_screenshot=False)`` skip the screenshot grab
and emit events without ``png_bytes``. We can't drive pynput in a unit
test, but we can poke the source's internal callbacks the same way
``test_scroll_source.py`` does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from inscription.capture import click_source as mod
from inscription.capture.click_source import ClickSource
from inscription.capture.window_source import WindowFocusSource
from inscription.platform import ForegroundInfo, ForegroundInspector

if TYPE_CHECKING:
    from inscription.capture import CaptureEngine, RawCaptureEvent


class _Engine:
    def __init__(self) -> None:
        self.events: list[RawCaptureEvent] = []

    def submit(self, event: RawCaptureEvent) -> bool:
        self.events.append(event)
        return True


class _StubInspector(ForegroundInspector):
    def __init__(self, *, hwnd: int, title: str = "App") -> None:
        self._hwnd = hwnd
        self._title = title

    def inspect(self) -> ForegroundInfo:
        return ForegroundInfo(
            window_title=self._title,
            process_name="app.exe",
            process_id=1,
            hwnd=self._hwnd,
            window_rect=(0, 0, 800, 600),
        )


def test_click_source_skips_screenshot_when_disabled() -> None:
    engine = _Engine()
    src = ClickSource(auto_screenshot=False)
    src._engine = cast("CaptureEngine", engine)

    # Drive the listener callback as pynput would. The Mouse.Button enum
    # isn't reachable headlessly; a stub object with .name suffices.
    class _Btn:
        name = "left"

    src._on_click(120, 240, _Btn(), True)

    # Screenshots are grabbed on a dedicated grabber thread (never in
    # the hook callback -- see click_source docstring). Drain the queue
    # the way the grabber would: the pending click carries
    # want_screenshot=False, so no capturer is ever touched.
    pending = src._grab_queue.get_nowait()
    assert pending.want_screenshot is False
    src._submit(pending, png=None, w=0, h=0, ox=0, oy=0)

    assert len(engine.events) == 1
    event = engine.events[0]
    assert event.png_bytes is None
    assert event.x == 120
    assert event.y == 240


def test_window_focus_source_skips_screenshot_when_disabled() -> None:
    engine = _Engine()
    inspector = _StubInspector(hwnd=11)
    src = WindowFocusSource(inspector=inspector, auto_screenshot=False)
    src._engine = cast("CaptureEngine", engine)

    # Prime the "previous identity" so the next tick fires an event.
    src._tick()  # establishes hwnd=11 as the baseline; no event
    src._inspector = _StubInspector(hwnd=22)
    src._tick()

    assert len(engine.events) == 1
    assert engine.events[0].png_bytes is None


def test_click_hook_callback_does_no_capture_work() -> None:
    """The pynput callback must only classify + enqueue.

    On Windows the callback runs inside a WH_MOUSE_LL hook; exceeding
    LowLevelHooksTimeout (~300 ms) makes the OS silently remove the
    hook -- every later click then goes unrecorded with no error. So
    the callback must never touch mss: the grab happens on the
    dedicated grabber thread.
    """
    engine = _Engine()
    src = ClickSource(auto_screenshot=True)
    src._engine = cast("CaptureEngine", engine)

    class _Btn:
        name = "left"

    src._on_click(50, 60, _Btn(), True)

    # Nothing submitted yet -- the click is parked in the grab queue
    # with its true click-time timestamp, waiting for the grabber.
    assert engine.events == []
    pending = src._grab_queue.get_nowait()
    assert pending.want_screenshot is True
    assert (pending.x, pending.y) == (50, 60)
    assert pending.occurred_at is not None


def test_click_queue_overflow_records_event_without_screenshot() -> None:
    """A pathologically slow grabber must not lose clicks: when the
    queue is full the click is recorded immediately without an image."""
    engine = _Engine()
    src = ClickSource(auto_screenshot=True)
    src._engine = cast("CaptureEngine", engine)

    class _Btn:
        name = "left"

    # Fill the queue to capacity, then one more.
    for i in range(mod._GRAB_QUEUE_MAX):
        src._on_click(i, 0, _Btn(), True)
    src._on_click(999, 999, _Btn(), True)

    # The overflow click was submitted directly, without a screenshot.
    assert len(engine.events) == 1
    assert engine.events[0].x == 999
    assert engine.events[0].png_bytes is None
