"""Mouse click capture source (``pynput`` backed).

Listens for mouse button presses and submits :class:`RawCaptureEvent`
objects to the engine. Each click carries a PNG captured on the pynput
listener thread *before* the event is enqueued, so the image reflects the
UI at click time rather than after queue latency.

Double-clicks are detected here — the engine does no temporal correlation.
"""

from __future__ import annotations

import logging
import threading
import time
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
    from inscription.capture.engine import CaptureEngine
    from inscription.platform import ScreenCapturer

logger = logging.getLogger(__name__)

#: Two clicks at the same point within this window merge into DOUBLE_CLICK.
DOUBLE_CLICK_WINDOW_S = 0.4
#: Pixel radius for the double-click position match.
DOUBLE_CLICK_RADIUS_PX = 4


class ClickSource(CaptureSource):
    """Convert pynput mouse press events into :class:`RawCaptureEvent`."""

    def __init__(self, *, auto_screenshot: bool = True) -> None:
        self._auto_screenshot = auto_screenshot
        self._engine: CaptureEngine | None = None
        self._listener: Any = None
        self._lock = threading.Lock()
        self._screen: ScreenCapturer | None = None
        self._last_click_ts: float = 0.0
        self._last_click_xy: tuple[int, int] | None = None
        self._last_click_button: str | None = None

    def start(self, engine: CaptureEngine) -> None:
        self._engine = engine
        if not _PYNPUT_AVAILABLE:
            logger.warning("pynput.mouse unavailable; ClickSource will not fire")
            return
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
        safe_close(self._screen)
        self._screen = None
        self._engine = None

    def _on_click(self, x: int, y: int, button: Any, pressed: bool) -> None:
        if not pressed:
            return
        engine = self._engine
        if engine is None:
            return
        button_name = getattr(button, "name", str(button))
        kind = self._classify(x, y, button_name)
        if self._auto_screenshot:
            png, w, h, ox, oy = self._capture(int(x), int(y))
        else:
            png, w, h, ox, oy = None, 0, 0, 0, 0
        engine.submit(
            RawCaptureEvent(
                kind=kind,
                occurred_at=utcnow(),
                button=button_name,
                x=int(x),
                y=int(y),
                png_bytes=png,
                png_width=w,
                png_height=h,
                png_left=ox,
                png_top=oy,
            )
        )

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

    def _capture(self, x: int, y: int) -> tuple[bytes | None, int, int, int, int]:
        """Grab a screenshot of whichever monitor holds ``(x, y)``.

        Returns ``(png, width, height, origin_left, origin_top)`` where
        the origin is the captured monitor's top-left in global screen
        coordinates — export subtracts it to map UIA rects into image
        space.

        The ``ScreenCapturer`` is created lazily on first click because
        ``mss`` must be owned by the thread that uses it, and pynput's
        listener thread only exists after :meth:`start`.
        """
        if self._screen is None:
            self._screen = create_screen_capturer()
        try:
            image = self._screen.capture_at(x, y)
        except Exception:
            logger.exception("Screenshot failed on click at (%d, %d)", x, y)
            return None, 0, 0, 0, 0
        return image.png_bytes, image.width, image.height, image.left, image.top
