"""Mouse click capture source (``pynput`` backed).

Listens for mouse button presses and submits :class:`RawCaptureEvent`
objects to the engine.

Screenshots are grabbed on a dedicated grabber thread, NOT inside the
pynput callback. On Windows the callback runs inside a low-level mouse
hook (``WH_MOUSE_LL``); if a hook callback exceeds the OS timeout
(``LowLevelHooksTimeout``, ~300 ms by default) Windows silently removes
the hook — after which every subsequent click goes unrecorded with no
error anywhere. A first-click ``mss`` init plus a 4K monitor grab can
plausibly blow that budget, so the hook callback now only classifies
the click and hands ``(x, y, kind, ...)`` to the grabber queue,
returning in microseconds.

The grab happens single-digit milliseconds later on the grabber
thread — still before the target application has repainted in response
to the click in practice, and the grabber owns the ``mss`` instance for
its whole life (created at thread start, so there is no first-click
init spike at all). FIFO ordering through the queue preserves click
order into the engine, and ``occurred_at`` is stamped in the hook
callback so event timestamps are true click times regardless of grab
latency.

Double-clicks are detected here — the engine does no temporal
correlation.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from inscription.capture.engine import CaptureSource
from inscription.capture.events import RawCaptureEvent
from inscription.model import EventKind, utcnow
from inscription.platform import create_screen_capturer, safe_close

try:
    from pynput import mouse as _pynput_mouse

    _PYNPUT_AVAILABLE = True
except Exception:
    _pynput_mouse = None
    _PYNPUT_AVAILABLE = False

if TYPE_CHECKING:
    from datetime import datetime

    from inscription.capture.engine import CaptureEngine

logger = logging.getLogger(__name__)

#: Two clicks at the same point within this window merge into DOUBLE_CLICK.
DOUBLE_CLICK_WINDOW_S = 0.4
#: Pixel radius for the double-click position match.
DOUBLE_CLICK_RADIUS_PX = 4

#: Bound on clicks waiting for their screenshot. If the grabber falls
#: this far behind (pathologically slow capture backend), further
#: clicks are recorded WITHOUT screenshots rather than blocking the
#: hook callback or growing without limit.
_GRAB_QUEUE_MAX = 32

_STOP = object()


@dataclass(frozen=True, slots=True)
class _PendingClick:
    """A classified click waiting for its screenshot on the grabber thread."""

    kind: EventKind
    occurred_at: datetime
    button: str
    x: int
    y: int
    want_screenshot: bool


class ClickSource(CaptureSource):
    """Convert pynput mouse press events into :class:`RawCaptureEvent`."""

    def __init__(self, *, auto_screenshot: bool = True) -> None:
        self._auto_screenshot = auto_screenshot
        self._engine: CaptureEngine | None = None
        self._listener: Any = None
        self._lock = threading.Lock()
        self._last_click_ts: float = 0.0
        self._last_click_xy: tuple[int, int] | None = None
        self._last_click_button: str | None = None
        self._grab_queue: queue.Queue[object] = queue.Queue(maxsize=_GRAB_QUEUE_MAX)
        self._grabber: threading.Thread | None = None

    def start(self, engine: CaptureEngine) -> None:
        self._engine = engine
        if not _PYNPUT_AVAILABLE:
            logger.warning("pynput.mouse unavailable; ClickSource will not fire")
            return
        grabber = threading.Thread(
            target=self._grab_loop, name="inscription-click-grab", daemon=True
        )
        grabber.start()
        self._grabber = grabber
        listener = _pynput_mouse.Listener(on_click=self._on_click)
        listener.daemon = True
        listener.start()
        self._listener = listener

    def set_auto_screenshot(self, enabled: bool) -> None:
        """Toggle screenshot capture while recording is in progress.

        Called from the GUI thread; the pynput listener thread reads
        ``_auto_screenshot`` on the next click. CPython bool assignment is
        atomic, so no lock is needed.
        """
        self._auto_screenshot = enabled

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as exc:
                logger.warning("Error stopping mouse listener: %s", exc)
            self._listener = None
        if self._grabber is not None:
            # Sentinel after the listener stops, so every already-queued
            # click still gets its screenshot before the thread exits.
            try:
                self._grab_queue.put_nowait(_STOP)
            except queue.Full:
                logger.warning("Grab queue full at stop; pending screenshots dropped")
            self._grabber.join(timeout=5.0)
            self._grabber = None
        self._engine = None

    # ------------------------------------------------------- hook callback

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        """pynput hook callback. MUST return fast (see module docstring)."""
        if not pressed:
            return
        if self._engine is None:
            return
        button_name = getattr(button, "name", str(button))
        kind = self._classify(x, y, button_name)
        pending = _PendingClick(
            kind=kind,
            occurred_at=utcnow(),
            button=button_name,
            x=int(x),
            y=int(y),
            want_screenshot=self._auto_screenshot,
        )
        try:
            self._grab_queue.put_nowait(pending)
        except queue.Full:
            # Grabber has fallen pathologically far behind. Record the
            # click WITHOUT a screenshot rather than blocking the hook
            # or silently dropping the event.
            logger.warning("Grab queue full; recording click without screenshot")
            self._submit(pending, png=None, w=0, h=0, ox=0, oy=0)

    def _classify(self, x: int, y: int, button_name: str) -> EventKind:
        now = time.monotonic()
        with self._lock:
            if (
                self._last_click_xy is not None
                and button_name == self._last_click_button
                and now - self._last_click_ts <= DOUBLE_CLICK_WINDOW_S
                and abs(x - self._last_click_xy[0]) <= DOUBLE_CLICK_RADIUS_PX
                and abs(y - self._last_click_xy[1]) <= DOUBLE_CLICK_RADIUS_PX
            ):
                # Reset so a triple-click doesn't also count as double.
                self._last_click_ts = 0.0
                self._last_click_xy = None
                self._last_click_button = None
                return EventKind.DOUBLE_CLICK
            self._last_click_ts = now
            self._last_click_xy = (x, y)
            self._last_click_button = button_name
            return EventKind.CLICK

    # ------------------------------------------------------ grabber thread

    def _grab_loop(self) -> None:
        # The grabber owns the mss instance for its whole life -- mss is
        # not thread-safe and creating it here (not lazily on first
        # click) means no init spike ever lands on a click.
        screen = create_screen_capturer()
        try:
            while True:
                item = self._grab_queue.get()
                if item is _STOP:
                    return
                if not isinstance(item, _PendingClick):  # pragma: no cover
                    continue
                png, w, h, ox, oy = None, 0, 0, 0, 0
                if item.want_screenshot:
                    try:
                        image = screen.capture_at(item.x, item.y)
                        png, w, h = image.png_bytes, image.width, image.height
                        ox, oy = image.left, image.top
                    except Exception:
                        logger.exception(
                            "Screenshot failed on click at (%d, %d)", item.x, item.y
                        )
                self._submit(item, png=png, w=w, h=h, ox=ox, oy=oy)
        finally:
            safe_close(screen)

    def _submit(
        self,
        pending: _PendingClick,
        *,
        png: bytes | None,
        w: int,
        h: int,
        ox: int,
        oy: int,
    ) -> None:
        engine = self._engine
        if engine is None:
            return
        engine.submit(
            RawCaptureEvent(
                kind=pending.kind,
                occurred_at=pending.occurred_at,
                button=pending.button,
                x=pending.x,
                y=pending.y,
                png_bytes=png,
                png_width=w,
                png_height=h,
                png_left=ox,
                png_top=oy,
            )
        )
