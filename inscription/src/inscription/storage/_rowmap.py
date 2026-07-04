"""SQLite row → dataclass mappers for :class:`SessionRepository`.

Pulled out of :mod:`inscription.storage.repository` so the mapper
logic stays close to the schema (and stays separately testable)
while ``repository.py`` itself is dominated by the API surface and
transaction discipline.

Every mapper here is a pure function: ``sqlite3.Row in -> dataclass
out``, no side effects, no DB access. The repository imports these
and binds them to its read methods.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from inscription.model import (
    DraftStep,
    EventKind,
    RawEvent,
    ResolvedElement,
    ScreenshotArtifact,
)

if TYPE_CHECKING:
    import sqlite3


def parse_iso(s: str) -> datetime:
    """Round-trip helper for ``occurred_at`` / ``captured_at`` strings.

    Repository rows store timestamps as ISO-8601 text (sqlite has no
    native datetime type); :func:`datetime.fromisoformat` is the
    inverse of :func:`datetime.isoformat` we use on write.
    """
    return datetime.fromisoformat(s)


def loads_rect(raw: str | None) -> tuple[int, int, int, int] | None:
    """Decode a JSON-array bounding rect from the DB.

    Bounding rects round-trip as ``[left, top, right, bottom]`` JSON
    arrays so they can be stored in a single ``TEXT`` column without
    blowing up the schema. A ``NULL`` column means "no rect known"
    and maps to :class:`None` here.
    """
    if raw is None:
        return None
    parts = json.loads(raw)
    return (int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]))


def row_to_event(row: sqlite3.Row) -> RawEvent:
    """Map a ``raw_events`` row to :class:`RawEvent`."""
    return RawEvent(
        id=row["id"],
        sequence=row["sequence"],
        occurred_at=parse_iso(row["occurred_at"]),
        kind=EventKind(row["kind"]),
        button=row["button"],
        x=row["x"],
        y=row["y"],
        key=row["key"],
        text=row["text"],
        window_title=row["window_title"],
        process_name=row["process_name"],
        screenshot_id=row["screenshot_id"],
        resolved_element_id=row["resolved_element_id"],
    )


def row_to_step(row: sqlite3.Row) -> DraftStep:
    """Map a ``draft_steps`` row to :class:`DraftStep`."""
    return DraftStep(
        id=row["id"],
        sequence=row["sequence"],
        action=row["action"],
        result=row["result"],
        source_event_ids=tuple(json.loads(row["source_event_ids"])),
        screenshot_id=row["screenshot_id"],
        suppressed=bool(row["suppressed"]),
        manual_edit=bool(row["manual_edit"]),
        evidentiary=bool(row["evidentiary"]),
    )


def row_to_screenshot(row: sqlite3.Row) -> ScreenshotArtifact:
    """Map a ``screenshot_artifacts`` row to :class:`ScreenshotArtifact`."""
    return ScreenshotArtifact(
        id=row["id"],
        relative_path=row["relative_path"],
        captured_at=parse_iso(row["captured_at"]),
        width=row["width"],
        height=row["height"],
        sha256=row["sha256"],
        highlight_rect=loads_rect(row["highlight_rect"]),
        origin_left=row["origin_left"],
        origin_top=row["origin_top"],
    )


def row_to_element(row: sqlite3.Row) -> ResolvedElement:
    """Map a ``resolved_elements`` row to :class:`ResolvedElement`.

    ``nearby_text`` was added in schema v6; older sessions opened by
    a newer build don't have the column populated, so we fall back to
    None when ``row.keys()`` doesn't include it. ``sqlite3.Row``
    doesn't implement ``__contains__`` so the ``.keys()`` is required
    even though SIM118 suggests dropping it.
    """
    nearby = (
        row["nearby_text"]
        if "nearby_text" in row.keys()  # noqa: SIM118 - sqlite3.Row needs the explicit call
        else None
    )
    return ResolvedElement(
        id=row["id"],
        name=row["name"],
        control_type=row["control_type"],
        automation_id=row["automation_id"],
        class_name=row["class_name"],
        role=row["role"],
        confidence=row["confidence"],
        method=row["method"],
        bounding_rect=loads_rect(row["bounding_rect"]),
        owner_process_name=row["owner_process_name"],
        nearby_text=nearby,
    )
