"""Mouse-wheel capture source.

pynput emits ``on_scroll`` once per wheel notch (or per touchpad delta).
A continuous scroll produces dozens of events per second, which would
flood the timeline and the LLM prompt with noise. This source
accumulates scroll deltas during a quiet window (``DEBOUNCE_S``) and
emits a single :data:`EventKind.SCROLL` event when the user stops or
the source shuts down — with the cumulative amount encoded in the
event's ``text`` field as e.g. ``"down 8"`` or ``"up 3, right 2"``.

No screenshot is captured (scrolling is fluid; a frozen frame mid-scroll
isn't useful for the guide). The event still gets foreground info on
the engine worker so the rendered step can say *"Scroll down 8 in
Chrome"* rather than *"Scroll down 8."*.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from inscription.capture.engine import CaptureSource
from inscription.capture.events import RawCaptureEvent
from inscription.model import EventKind, utcnow

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

#: Quiet time after the last scroll notch before the accumulated delta is
#: flushed as one event. Long enough to bundle a flick into a single
#: step, short enough that "scroll, click, scroll" stays in order.
DEBOUNCE_S = 0.6


class ScrollSource(CaptureSource):
    """Debounced mouse-wheel source. Emits one event per scroll burst."""

    def __init__(self, *, debounce_s: float = DEBOUNCE_S) -> None:
        self._debounce_s = debounce_s
        self._engine: CaptureEngine | None = None
        self._listener: Any = None
        self._lock = threading.Lock()
        self._dx = 0
        self._dy = 0
        self._last_x = 0
        self._last_y = 0
        self._flush_timer: threading.Timer | None = None
        # Wall-clock time of the FIRST wheel tick in the current burst.
        # The flushed event is stamped with this, not flush time: the
        # debounce delays the flush by up to debounce_s after scrolling
        # stopped, and stamping at flush inverted the timeline -- a
        # click landing in the quiet window was timestamped BEFORE the
        # scroll that physically preceded it.
        self._burst_started_at: datetime | None = None

    def start(self, engine: CaptureEngine) -> None:
        self._engine = engine
        if not _PYNPUT_AVAILABLE:
            logger.warning("pynput.mouse unavailable; ScrollSource will not fire")
            return
        listener = _pynput_mouse.Listener(on_scroll=self._on_scroll)
        listener.daemon = True
        listener.start()
        self._listener = listener

    def stop(self) -> None:
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception as exc:
                logger.warning("Error stopping scroll listener: %s", exc)
            self._listener = None
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._flush_locked()
        self._engine = None

    def _on_scroll(self, x: int, y: int, dx: int, dy: int) -> None:
        with self._lock:
            if self._dx == 0 and self._dy == 0:
                self._burst_started_at = utcnow()
            self._dx += int(dx)
            self._dy += int(dy)
            self._last_x = int(x)
            self._last_y = int(y)
            if self._flush_timer is not None:
                self._flush_timer.cancel()
            timer = threading.Timer(self._debounce_s, self._flush)
            timer.daemon = True
            self._flush_timer = timer
            timer.start()

    def _flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if self._dx == 0 and self._dy == 0:
            return
        engine = self._engine
        descriptor = _describe(self._dx, self._dy)
        x, y = self._last_x, self._last_y
        started_at = self._burst_started_at or utcnow()
        self._dx = 0
        self._dy = 0
        self._burst_started_at = None
        self._flush_timer = None
        if engine is None:
            return
        engine.submit(
            RawCaptureEvent(
                kind=EventKind.SCROLL,
                occurred_at=started_at,
                x=x,
                y=y,
                text=descriptor,
            )
        )


def _describe(dx: int, dy: int) -> str:
    """Format a scroll burst as a human-readable string.

    pynput convention: ``dy > 0`` is up, ``dy < 0`` is down;
    ``dx > 0`` is right, ``dx < 0`` is left.
    """
    parts: list[str] = []
    if dy != 0:
        parts.append(f"{'up' if dy > 0 else 'down'} {abs(dy)}")
    if dx != 0:
        parts.append(f"{'right' if dx > 0 else 'left'} {abs(dx)}")
    return ", ".join(parts) if parts else "0"
