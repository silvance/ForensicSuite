"""Foreground-window change capture source.

Polls the foreground inspector on a timer. Whenever the active window
changes, submits a :data:`EventKind.WINDOW_FOCUS` event with a screenshot
of the new foreground. Polling is fine here — UIA window events require
per-process hooks and a 250 ms poll is invisible to users while still
catching every practical transition.

A window is identified by its native handle (``hwnd`` on Windows), not
by its title. The title changes as the user types (``*h - Notepad``,
``*he - Notepad``, …) and keying on title would produce one spurious
"switch window" event per keystroke. When ``hwnd`` isn't available (the
non-Windows stub inspector), the source falls back to title + process.

The screenshot is captured on this source's own poll thread, matching the
click source pattern.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from inscription.capture.engine import CaptureSource
from inscription.capture.events import RawCaptureEvent
from inscription.model import EventKind, utcnow
from inscription.platform import create_screen_capturer, safe_close
from inscription.platform.version import read_file_version

if TYPE_CHECKING:
    from collections.abc import Callable

    from inscription.capture.engine import CaptureEngine
    from inscription.platform import (
        CapturedImage,
        ForegroundInfo,
        ForegroundInspector,
        ScreenCapturer,
    )

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL_S = 0.25


def _identity(info: ForegroundInfo) -> tuple[int | str, str]:
    """Return a stable identity for a window.

    Prefer the native handle (``hwnd``) so title changes inside the same
    window don't register as transitions. Fall back to the title when no
    handle is available (non-Windows).
    """
    if info.hwnd is not None:
        return (info.hwnd, info.process_name or "")
    return (info.window_title or "", info.process_name or "")


class WindowFocusSource(CaptureSource):
    """Polls foreground window and emits an event on transition."""

    def __init__(
        self,
        *,
        inspector: ForegroundInspector,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        auto_screenshot: bool = True,
        version_reader: Callable[[str | None], str | None] = read_file_version,
    ) -> None:
        self._inspector = inspector
        self._interval = interval_s
        self._auto_screenshot = auto_screenshot
        self._version_reader = version_reader
        self._engine: CaptureEngine | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._screen: ScreenCapturer | None = None
        self._last_identity: tuple[int | str, str] | None = None
        # Process paths we've already announced this session. First focus
        # of a new process emits a MARKER carrying its name + file
        # version; subsequent focus events on the same process don't
        # repeat the metadata.
        self._announced_processes: set[str] = set()

    def start(self, engine: CaptureEngine) -> None:
        self._engine = engine
        self._stop.clear()
        thread = threading.Thread(target=self._run, name="inscription-window-watch", daemon=True)
        thread.start()
        self._thread = thread

    def set_auto_screenshot(self, enabled: bool) -> None:
        """Toggle screenshot capture while recording is in progress.

        Called from the GUI thread; the poll thread reads ``_auto_screenshot``
        on the next window-focus event. CPython bool assignment is atomic.
        """
        self._auto_screenshot = enabled

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2 * self._interval + 0.5)
            self._thread = None
        safe_close(self._screen)
        self._screen = None
        self._engine = None

    def _run(self) -> None:
        # ScreenCapturer is owned by this thread — mss isn't thread-safe.
        self._screen = create_screen_capturer()
        while not self._stop.wait(self._interval):
            try:
                self._tick()
            except Exception:
                logger.exception("Window-focus poll failed")

    def _tick(self) -> None:
        engine = self._engine
        if engine is None:
            return
        info = self._inspector.inspect()
        identity = _identity(info)
        if identity == self._last_identity:
            return
        previous = self._last_identity
        self._last_identity = identity
        # Ignore the very first observation -- it's the window that was
        # already active when recording started, not a transition.
        if previous is None:
            return
        self._announce_process_if_new(engine, info)
        png, w, h, ox, oy = (
            self._capture(info) if self._auto_screenshot else (None, 0, 0, 0, 0)
        )
        engine.submit(
            RawCaptureEvent(
                kind=EventKind.WINDOW_FOCUS,
                occurred_at=utcnow(),
                png_bytes=png,
                png_width=w,
                png_height=h,
                png_left=ox,
                png_top=oy,
            )
        )

    def _announce_process_if_new(self, engine: CaptureEngine, info: ForegroundInfo) -> None:
        """Emit a MARKER with process name + version on first focus.

        Lets the timeline carry "Magnet AXIOM Examine v8.6.0.42301"
        as a discrete, rendered step rather than burying it in the
        WINDOW_FOCUS row -- the AI rewrite pass picks it up as
        provenance and the exported notes include it verbatim.

        Skipped silently when there's no executable path to read from
        (non-Windows fallback inspector, or psutil refused) or when
        we've already announced this binary in the current session.
        """
        path = info.process_path
        if not path or path in self._announced_processes:
            return
        self._announced_processes.add(path)
        version = self._version_reader(path)
        process_name = info.process_name or path
        if version:
            text = f"Foreground app: {process_name} v{version}"
        else:
            text = f"Foreground app: {process_name}"
        engine.submit(
            RawCaptureEvent(
                kind=EventKind.MARKER,
                occurred_at=utcnow(),
                text=text,
            )
        )

    def _capture(
        self, info: ForegroundInfo
    ) -> tuple[bytes | None, int, int, int, int]:
        """Capture the monitor holding ``info``'s window.

        Without this, ``mss`` defaults to monitor 1 — which on many Windows
        multi-monitor setups is *not* the display the window is on. We use
        the window rect's center to pick the right monitor. The trailing
        pair in the return tuple is the captured monitor's global-screen
        origin (see :class:`CapturedImage`).
        """
        if self._screen is None:  # pragma: no cover - defensive
            return None, 0, 0, 0, 0
        try:
            image = self._grab_for(info)
        except Exception:
            logger.exception("Screenshot failed on window focus")
            return None, 0, 0, 0, 0
        return image.png_bytes, image.width, image.height, image.left, image.top

    def _grab_for(self, info: ForegroundInfo) -> CapturedImage:
        assert self._screen is not None
        rect = info.window_rect
        if rect is None:
            return self._screen.capture()
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        return self._screen.capture_at(cx, cy)
