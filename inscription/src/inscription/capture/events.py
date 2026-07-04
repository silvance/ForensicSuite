"""Capture event types flowing from sources into the engine.

Sources that want a screenshot associated with an event capture it on their
own thread (``mss`` is not thread-safe, so each source owns its own
capturer) and attach the PNG bytes here. The engine doesn't take any
screenshots — it only enriches the event with foreground info and, for
clicks, a resolved UI element.

Doing the grab on the listener thread is what gives clicks a pre-click
screenshot: the image is taken before the event enters the engine queue,
so it reflects the UI as the user saw it at click time rather than after
the drain latency has let the UI respond.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from inscription.model import EventKind, utcnow

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RawCaptureEvent:
    """One event submitted by a source."""

    kind: EventKind
    occurred_at: datetime = field(default_factory=utcnow)
    button: str | None = None
    x: int | None = None
    y: int | None = None
    key: str | None = None
    text: str | None = None
    #: PNG bytes captured on the source's thread, or None for events (e.g.
    #: milestone keys) that don't warrant a screenshot.
    png_bytes: bytes | None = None
    png_width: int = 0
    png_height: int = 0
    #: Origin of the captured region in GLOBAL screen coordinates.
    #: Nonzero whenever the grab came from a monitor whose top-left
    #: isn't the virtual desktop's (0,0) — secondary displays. Export
    #: needs this to translate screen-space UIA rects / click points
    #: into image space before cropping.
    png_left: int = 0
    png_top: int = 0
