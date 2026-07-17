"""Sink that persists enriched events into a :class:`SessionRepository`.

The sink writes the PNG to disk, inserts a ``screenshot_artifacts`` row,
inserts a ``resolved_elements`` row (when a click resolved something), and
finally inserts the ``raw_events`` row that references them.

Screenshot filenames are derived from the event's ``processed_at``
timestamp with microsecond precision, plus a collision suffix: grabs
happen on the SOURCE threads (click listener, window poll), so two
events queued concurrently can carry timestamps closer together than
the enrichment time between the worker's ``utcnow()`` stamps -- and a
clock step-back (NTP) can repeat a microsecond outright. A silent
``write_bytes`` overwrite would destroy one event's visual evidence
while leaving two DB rows pointing at one file; the suffix loop makes
that impossible.

Failure ordering: the raw event row is the evidentiary core, so a
failed screenshot write/insert no longer aborts the whole persist --
the event is saved without its image and the failure is logged. (The
reverse window -- append_event failing after the screenshot row
committed -- leaves an orphaned artifact row; harmless, no event
references it, and integrity verify still passes it.)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from inscription.capture.engine import EnrichedEvent
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


def _filename_for(processed_at: datetime) -> str:
    """Return a sortable PNG filename (uniquified by the caller).

    Example: ``event-20260424T072150-123456.png``.
    """
    return "event-" + processed_at.strftime("%Y%m%dT%H%M%S-%f") + ".png"


def _unique_target(directory: Path, filename: str) -> Path:
    """Return a path in ``directory`` that does not exist yet.

    Appends ``-1``, ``-2``, ... before the extension on collision.
    Two events can share a ``processed_at`` microsecond (concurrent
    source-thread grabs, NTP step-back); overwriting would silently
    destroy the earlier event's screenshot.
    """
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    n = 1
    while True:
        candidate = directory / f"{stem}-{n}.png"
        if not candidate.exists():
            return candidate
        n += 1


class SessionSink:
    """Persists captures to a live :class:`SessionRepository`.

    Implements the :class:`inscription.capture.engine.CaptureSink` protocol
    by duck-typing — it provides a ``handle`` method with the right
    signature.
    """

    def __init__(self, repository: SessionRepository) -> None:
        self._repo = repository

    def handle(self, event: EnrichedEvent) -> None:
        raw = event.raw

        screenshot_id: int | None = None
        if raw.png_bytes:
            # A screenshot failure must not take the event down with
            # it: the raw event row is the evidentiary core. Persist
            # the event without its image rather than losing both.
            try:
                shots_dir = self._repo.session.root / "screenshots"
                shots_dir.mkdir(parents=True, exist_ok=True)
                target = _unique_target(
                    shots_dir, _filename_for(event.processed_at)
                )
                relative = f"screenshots/{target.name}"
                target.write_bytes(raw.png_bytes)
                artifact = self._repo.add_screenshot(
                    relative_path=relative,
                    captured_at=event.processed_at,
                    width=raw.png_width,
                    height=raw.png_height,
                    sha256=event.image_sha256,
                    origin_left=raw.png_left,
                    origin_top=raw.png_top,
                )
                screenshot_id = artifact.id
            except OSError:
                logger.exception(
                    "Screenshot persist failed; saving event without image"
                )

        resolved_id: int | None = None
        if event.resolved is not None and event.resolved.confidence > 0:
            stored = self._repo.add_resolved_element(event.resolved)
            resolved_id = stored.id

        persisted = self._repo.append_event(
            kind=raw.kind,
            occurred_at=raw.occurred_at,
            button=raw.button,
            x=raw.x,
            y=raw.y,
            key=raw.key,
            text=raw.text,
            window_title=event.foreground.window_title or None,
            process_name=event.foreground.process_name or None,
            screenshot_id=screenshot_id,
            resolved_element_id=resolved_id,
        )
        # Stamp the ids onto the event so downstream sinks (e.g. the live
        # step generator) can reference them without re-querying.
        event.persisted_event_id = persisted.id
        event.persisted_screenshot_id = screenshot_id
        event.persisted_resolved_id = resolved_id
        logger.debug(
            "Persisted %s event id=%s (screenshot=%s, resolved=%s)",
            raw.kind.value,
            persisted.id,
            screenshot_id,
            resolved_id,
        )
