"""Screen capture abstraction.

The default implementation uses ``mss`` for speed and multi-monitor support.
A null fallback exists for environments where ``mss`` cannot initialise
(e.g. headless CI without a display); it returns a 1x1 placeholder PNG so
the rest of the pipeline keeps working.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

import mss
import mss.tools

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class MonitorInfo:
    """Geometry of one monitor, as reported by the capturer."""

    index: int
    """1-based index. Monitor 0 is traditionally "all monitors stitched"."""
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True, slots=True, kw_only=True)
class CapturedImage:
    """Raw image bytes plus metadata from a single capture.

    ``left`` / ``top`` are the captured region's origin in GLOBAL
    screen coordinates. A secondary monitor to the right of a
    1920-wide primary has ``left=1920``; one above the primary has a
    negative ``top``. Consumers that overlay screen-space geometry
    (UIA bounding rects, click points) onto the image must subtract
    this origin first — the PNG's pixel (0,0) is (left, top) in
    screen space, not (0,0).
    """

    png_bytes: bytes
    width: int
    height: int
    monitor_index: int
    left: int = 0
    top: int = 0


class ScreenCapturer(ABC):
    """Abstract capture interface. See :mod:`inscription.platform` for wiring."""

    @abstractmethod
    def list_monitors(self) -> list[MonitorInfo]:
        """Return all available monitors, 1-indexed."""

    @abstractmethod
    def capture(self, monitor_index: int | None = None) -> CapturedImage:
        """Capture ``monitor_index`` (default: primary).

        Args:
            monitor_index: 1-based index into :meth:`list_monitors`.
                ``None`` means primary monitor (typically index 1).
        """

    def capture_at(self, x: int, y: int) -> CapturedImage:
        """Capture whichever monitor contains ``(x, y)``.

        Useful for click events on multi-monitor setups: the default
        implementation scans :meth:`list_monitors` for the monitor whose
        bbox covers the point and calls :meth:`capture` with that index.
        Falls back to the primary monitor if no monitor matches.
        """
        for mon in self.list_monitors():
            if mon.index == 0:
                # Index 0 is the virtual "all monitors" entry in mss; skip.
                continue
            if mon.left <= x < mon.left + mon.width and mon.top <= y < mon.top + mon.height:
                return self.capture(monitor_index=mon.index)
        return self.capture()

    def capture_to_file(self, target: Path, monitor_index: int | None = None) -> CapturedImage:
        """Capture and write the PNG bytes to ``target``.

        Returns the same :class:`CapturedImage` as :meth:`capture`.
        """
        image = self.capture(monitor_index=monitor_index)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(image.png_bytes)
        return image

    def close(self) -> None:  # noqa: B027 - intentional no-op default
        """Release any platform resources. Safe to call multiple times.

        Default is a no-op for subclasses with no resources to release;
        override when cleanup is needed.
        """


class MssScreenCapturer(ScreenCapturer):
    """``mss``-backed screen capturer.

    ``mss`` objects are not thread-safe; each thread that calls
    :meth:`capture` must own its own instance. The capture engine handles
    this by creating the capturer on the worker thread.
    """

    def __init__(self) -> None:
        self._sct = mss.mss()

    def list_monitors(self) -> list[MonitorInfo]:
        return [
            MonitorInfo(
                index=i,
                left=mon["left"],
                top=mon["top"],
                width=mon["width"],
                height=mon["height"],
            )
            for i, mon in enumerate(self._sct.monitors)
        ]

    def capture(self, monitor_index: int | None = None) -> CapturedImage:
        # mss exposes the primary display as index 1 on most platforms.
        target_index = monitor_index if monitor_index is not None else 1
        monitors = self._sct.monitors
        if target_index < 0 or target_index >= len(monitors):
            msg = (
                f"Monitor index {target_index} out of range; "
                f"available indices 0..{len(monitors) - 1}"
            )
            raise IndexError(msg)

        shot = self._sct.grab(monitors[target_index])
        # mss.tools.to_png returns bytes when no output path is supplied.
        png = mss.tools.to_png(shot.rgb, shot.size)
        assert png is not None, "mss.tools.to_png returned None with no output path"
        return CapturedImage(
            png_bytes=png,
            width=shot.size[0],
            height=shot.size[1],
            monitor_index=target_index,
            left=monitors[target_index]["left"],
            top=monitors[target_index]["top"],
        )

    def close(self) -> None:
        try:
            self._sct.close()
        except Exception as exc:  # pragma: no cover - defensive
            # mss on Windows routinely fails ReleaseDC on shutdown — the DC
            # is already gone by the time cleanup runs. It's harmless.
            logger.debug("mss close raised %s (harmless on Windows)", exc)


class _NullScreenCapturer(ScreenCapturer):
    """Fallback returning a tiny synthetic PNG. Used when ``mss`` is absent."""

    # 1x1 transparent PNG (RGBA). Verified-decodable; used as the
    # placeholder when no real capture backend is available.
    _ONE_PX_PNG = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae"
        "426082"
    )

    def list_monitors(self) -> list[MonitorInfo]:
        return [MonitorInfo(index=1, left=0, top=0, width=1, height=1)]

    def capture(self, monitor_index: int | None = None) -> CapturedImage:
        return CapturedImage(
            png_bytes=self._ONE_PX_PNG,
            width=1,
            height=1,
            monitor_index=monitor_index or 1,
        )


def create_screen_capturer() -> ScreenCapturer:
    """Return a capturer appropriate for the current environment."""
    try:
        return MssScreenCapturer()
    except Exception as exc:
        logger.warning(
            "mss initialisation failed (%s); falling back to null capturer. "
            "Screenshots will be 1x1 placeholders.",
            exc,
        )
        return _NullScreenCapturer()


def safe_close(capturer: ScreenCapturer | None) -> None:
    """Close ``capturer`` if non-None, swallowing any error.

    Sources call this from their ``stop()`` paths — losing the capturer
    cleanly should never propagate up and trip the engine's source-stop
    handler.
    """
    if capturer is None:
        return
    try:
        capturer.close()
    except Exception as exc:
        logger.warning("Error closing screen capturer: %s", exc)
