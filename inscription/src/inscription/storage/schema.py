"""SQLite schema and forward-only migration runner.

Schema for a session database:

- ``session_info`` — single row with name/timestamps/schema version.
- ``raw_events`` — the captured timeline.
- ``resolved_elements`` — UIA metadata for clickable targets.
- ``screenshot_artifacts`` — image files referenced by events/steps.
- ``draft_steps`` — generated, editable procedural steps.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from inscription.model import SCHEMA_VERSION
from inscription.storage.errors import SchemaVersionError

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

logger = logging.getLogger(__name__)


INITIAL_SCHEMA_SQL = """
CREATE TABLE session_info (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    name              TEXT    NOT NULL,
    started_at        TEXT    NOT NULL,
    ended_at          TEXT,
    recorder_version  TEXT    NOT NULL DEFAULT '',
    schema_version    INTEGER NOT NULL
);

CREATE TABLE resolved_elements (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT,
    control_type         TEXT,
    automation_id        TEXT,
    class_name           TEXT,
    role                 TEXT,
    confidence           REAL    NOT NULL DEFAULT 0,
    method               TEXT    NOT NULL DEFAULT 'none',
    bounding_rect        TEXT,   -- JSON [left, top, right, bottom] in screen px
    owner_process_name   TEXT,   -- process that owns the element, for taskbar/shell
    nearby_text          TEXT    -- pipe-joined sibling labels, for icon-only buttons
);

CREATE TABLE screenshot_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT    NOT NULL UNIQUE,
    captured_at     TEXT    NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    sha256          TEXT    NOT NULL DEFAULT '',
    highlight_rect  TEXT,
    origin_left     INTEGER NOT NULL DEFAULT 0,  -- capture region's global-screen origin
    origin_top      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE raw_events (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence             INTEGER NOT NULL,
    occurred_at          TEXT    NOT NULL,
    kind                 TEXT    NOT NULL,
    button               TEXT,
    x                    INTEGER,
    y                    INTEGER,
    key                  TEXT,
    text                 TEXT,
    window_title         TEXT,
    process_name         TEXT,
    screenshot_id        INTEGER REFERENCES screenshot_artifacts(id),
    resolved_element_id  INTEGER REFERENCES resolved_elements(id)
);

CREATE INDEX idx_raw_events_sequence ON raw_events(sequence);

CREATE TABLE draft_steps (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    sequence          INTEGER NOT NULL,
    action            TEXT    NOT NULL DEFAULT '',
    result            TEXT    NOT NULL DEFAULT '',
    source_event_ids  TEXT    NOT NULL DEFAULT '[]',
    screenshot_id     INTEGER REFERENCES screenshot_artifacts(id),
    suppressed        INTEGER NOT NULL DEFAULT 0,
    manual_edit       INTEGER NOT NULL DEFAULT 0,
    evidentiary       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_draft_steps_sequence ON draft_steps(sequence);
"""


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``resolved_elements.bounding_rect`` for crop-and-highlight."""
    conn.execute("ALTER TABLE resolved_elements ADD COLUMN bounding_rect TEXT")


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add ``resolved_elements.owner_process_name`` for cross-process click text."""
    conn.execute("ALTER TABLE resolved_elements ADD COLUMN owner_process_name TEXT")


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Add ``draft_steps.evidentiary`` for downstream report-builder integration."""
    conn.execute("ALTER TABLE draft_steps ADD COLUMN evidentiary INTEGER NOT NULL DEFAULT 0")


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Split draft_steps.text into action + result columns.

    Forensic exam notes are kept as Time/Date / Action / Result; the
    single ``text`` field can't carry the Action vs. Result semantics
    cleanly, so we split it. Existing rows get their full text moved into
    ``action`` and an empty ``result``; the user can then fill in
    observations as needed.
    """
    conn.execute("ALTER TABLE draft_steps ADD COLUMN action TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE draft_steps ADD COLUMN result TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE draft_steps SET action = text")
    conn.execute("ALTER TABLE draft_steps DROP COLUMN text")


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add ``resolved_elements.nearby_text`` for sibling-label context.

    Many forensic tools (Cellebrite, AXIOM, X-Ways) render their UI in
    custom WPF / WinUI canvases where UIA hands back generic
    ``"Pane"`` / ``"Custom"`` elements with empty names. Capturing a
    short snapshot of nearby text labels (siblings of the clicked
    element) gives the LLM rewriter enough context to produce a
    meaningful step description even when the element's own name
    isn't useful. Existing rows stay NULL.
    """
    conn.execute("ALTER TABLE resolved_elements ADD COLUMN nearby_text TEXT")


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    """Add ``screenshot_artifacts.origin_left`` / ``origin_top``.

    Per-monitor screenshots have their pixel (0,0) at the monitor's
    global-screen origin, but UIA bounding rects and click points are
    global-screen coordinates. Without the origin stored alongside the
    image, the export crop treated screen coords as image coords and
    cropped the WRONG region for any click on a monitor whose origin
    isn't (0,0). Legacy rows default to 0/0 — the primary-monitor
    assumption they were captured under, so their behaviour is
    unchanged.
    """
    conn.execute(
        "ALTER TABLE screenshot_artifacts ADD COLUMN origin_left INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute(
        "ALTER TABLE screenshot_artifacts ADD COLUMN origin_top INTEGER NOT NULL DEFAULT 0"
    )


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    2: _migrate_v1_to_v2,
    3: _migrate_v2_to_v3,
    4: _migrate_v3_to_v4,
    5: _migrate_v4_to_v5,
    6: _migrate_v5_to_v6,
    7: _migrate_v6_to_v7,
}


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='session_info'"
    ).fetchone()
    if row is None:
        return 0
    result = conn.execute("SELECT schema_version FROM session_info WHERE id = 1").fetchone()
    if result is None:
        return 0
    version: int = result[0]
    return version


def initialise_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(INITIAL_SCHEMA_SQL)
    conn.commit()
    logger.debug("Initial schema applied (version %d)", SCHEMA_VERSION)


def migrate_to_latest(conn: sqlite3.Connection) -> None:
    version = current_schema_version(conn)
    if version > SCHEMA_VERSION:
        msg = (
            f"Session database schema version {version} is newer than this "
            f"Inscription build supports (max {SCHEMA_VERSION}). Update "
            f"Inscription or use a compatible build."
        )
        raise SchemaVersionError(msg)
    while version < SCHEMA_VERSION:
        target = version + 1
        migration = MIGRATIONS.get(target)
        if migration is None:
            msg = f"No migration registered for schema version {target}"
            raise SchemaVersionError(msg)
        logger.info("Migrating schema %d -> %d", version, target)
        migration(conn)
        conn.execute("UPDATE session_info SET schema_version = ? WHERE id = 1", (target,))
        conn.commit()
        version = target
