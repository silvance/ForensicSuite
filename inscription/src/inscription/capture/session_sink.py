"""Sink that persists enriched events into a :class:`SessionRepository`.

The sink writes the PNG to disk, inserts a ``screenshot_artifacts`` row,
inserts a ``resolved_elements`` row (when a click resolved something), and
finally inserts the ``raw_events`` row that references them.

Screenshot filenames are derived from the event's ``processed_at``
timestamp with microsecond precision. The engine worker processes events
serially and each ``mss.grab`` takes milliseconds, so two events cannot
land in the same microsecond. That makes filenames unique without a
sink-local counter that has to be seeded correctly across recording
restarts.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from inscription.capture.engine import EnrichedEvent
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


def _filename_for(processed_at: datetime) -> str:
    """Return a sortable, collision-resistant PNG filename.

    Example: ``event-20260424T072150-123456.png``.
    """
    return "event-" + processed_at.strftime("%Y%m%dT%H%M%S-%f") + ".png"


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
            relative = f"screenshots/{_filename_for(event.processed_at)}"
            target = self._repo.session.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
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
