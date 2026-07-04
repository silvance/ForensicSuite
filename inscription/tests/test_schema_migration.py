"""Forward-only schema migrations.

Simulates a v1 session database (the shipped alpha), runs the migration
runner, and asserts the v2 ``bounding_rect`` column is present afterwards
and the repository can read/write it.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

from inscription.model import DraftStep, EventKind, ResolvedElement, utcnow
from inscription.storage import SessionRepository

if TYPE_CHECKING:
    from pathlib import Path

# Inline copy of the v1 INITIAL_SCHEMA_SQL. Pinned here so future changes
# to the current schema don't silently retune this test — the whole point
# is to exercise the migration from the actually-shipped v1 shape.
_V1_SCHEMA = """
CREATE TABLE session_info (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    name              TEXT    NOT NULL,
    started_at        TEXT    NOT NULL,
    ended_at          TEXT,
    recorder_version  TEXT    NOT NULL DEFAULT '',
    schema_version    INTEGER NOT NULL
);

CREATE TABLE resolved_elements (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,
    control_type    TEXT,
    automation_id   TEXT,
    class_name      TEXT,
    role            TEXT,
    confidence      REAL    NOT NULL DEFAULT 0,
    method          TEXT    NOT NULL DEFAULT 'none'
);

CREATE TABLE screenshot_artifacts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT    NOT NULL UNIQUE,
    captured_at     TEXT    NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    sha256          TEXT    NOT NULL DEFAULT '',
    highlight_rect  TEXT
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
    text              TEXT    NOT NULL DEFAULT '',
    source_event_ids  TEXT    NOT NULL DEFAULT '[]',
    screenshot_id     INTEGER REFERENCES screenshot_artifacts(id),
    suppressed        INTEGER NOT NULL DEFAULT 0,
    manual_edit       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_draft_steps_sequence ON draft_steps(sequence);
"""


def _build_v1_session(root: Path, name: str) -> None:
    """Lay out a session folder matching what a v1 build would have left."""
    slug = name
    session_root = root / slug
    session_root.mkdir()
    (session_root / "screenshots").mkdir()
    (session_root / "exports").mkdir()
    (session_root / ".inscription").mkdir()
    (session_root / "manifest.json").write_text(
        '{"name": "' + name + '", "started_at": "2026-04-24T00:00:00+00:00",'
        ' "ended_at": null, "event_count": 0, "step_count": 0,'
        ' "schema_version": 1, "tags": []}\n',
        encoding="utf-8",
    )
    conn = sqlite3.connect(session_root / "session.db")
    conn.executescript(_V1_SCHEMA)
    conn.execute(
        "INSERT INTO session_info (id, name, started_at, schema_version) "
        "VALUES (1, ?, '2026-04-24T00:00:00+00:00', 1)",
        (name,),
    )
    # Pre-existing row — exercises ALTER TABLE backfilling NULL into new column.
    conn.execute(
        "INSERT INTO resolved_elements (name, control_type, confidence, method) "
        "VALUES ('Legacy', 'Button', 0.9, 'uia')"
    )
    conn.commit()
    conn.close()


def test_v1_session_migrates_all_the_way_forward(tmp_path: Path) -> None:
    """A v1 session DB should come up fully migrated on every new column."""
    _build_v1_session(tmp_path, "Legacy-Session")

    repo = SessionRepository.open_existing(workspace_root=tmp_path, slug="Legacy-Session")
    try:
        # Pre-existing row survives all ALTER TABLEs; new columns are NULL/0.
        legacy = repo.get_resolved_element(1)
        assert legacy is not None
        assert legacy.name == "Legacy"
        assert legacy.bounding_rect is None
        assert legacy.owner_process_name is None
        # v5 → v6 added nearby_text; legacy rows leave it None.
        assert legacy.nearby_text is None

        # New inserts write and read every field end-to-end.
        fresh = repo.add_resolved_element(
            ResolvedElement(
                id=None,
                name="New",
                control_type="Button",
                confidence=0.9,
                method="uia",
                bounding_rect=(10, 20, 110, 50),
                owner_process_name="explorer.exe",
                nearby_text="Pictures | 2,341 hits",
            )
        )
        assert fresh.id is not None
        reloaded = repo.get_resolved_element(fresh.id)
        assert reloaded is not None
        assert reloaded.bounding_rect == (10, 20, 110, 50)
        assert reloaded.owner_process_name == "explorer.exe"
        assert reloaded.nearby_text == "Pictures | 2,341 hits"

        # v3 → v4 added draft_steps.evidentiary, v4 → v5 split text into
        # action + result. Existing rows / new rows respect every column.
        event = repo.append_event(kind=EventKind.CLICK, x=1, y=1, button="left")
        assert event.id is not None
        saved = repo.replace_steps(
            [
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Loaded E01 file",
                    result="Hash verified",
                    source_event_ids=(event.id,),
                    evidentiary=True,
                )
            ]
        )
        assert saved[0].action == "Loaded E01 file"
        assert saved[0].result == "Hash verified"
        assert saved[0].evidentiary is True
        repo_steps = repo.list_steps()
        assert repo_steps[0].action == "Loaded E01 file"
        assert repo_steps[0].result == "Hash verified"
        assert repo_steps[0].evidentiary is True
    finally:
        repo.close()


def test_v1_session_keeps_existing_step_text_as_action(tmp_path: Path) -> None:
    """A v1 draft_steps row's ``text`` should land in ``action`` after v4→v5."""
    _build_v1_session(tmp_path, "Legacy-WithSteps")

    # Pre-seed a draft_steps row in the v1 shape.
    db_path = tmp_path / "Legacy-WithSteps" / "session.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO raw_events (sequence, occurred_at, kind, button, x, y) "
            "VALUES (1, '2026-04-24T00:00:00+00:00', 'click', 'left', 1, 1)"
        )
        event_id = conn.execute("SELECT id FROM raw_events").fetchone()[0]
        conn.execute(
            "INSERT INTO draft_steps (sequence, text, source_event_ids) VALUES (?, ?, ?)",
            (1, "Original v1 text", f"[{event_id}]"),
        )
        conn.commit()
    finally:
        conn.close()

    repo = SessionRepository.open_existing(workspace_root=tmp_path, slug="Legacy-WithSteps")
    try:
        steps = repo.list_steps()
        assert len(steps) == 1
        assert steps[0].action == "Original v1 text"
        assert steps[0].result == ""
    finally:
        repo.close()


def test_v6_screenshot_rows_get_zero_origin_and_new_rows_round_trip(
    tmp_path: Path,
) -> None:
    """v6 -> v7 adds screenshot_artifacts.origin_left / origin_top.

    Legacy rows (captured under the primary-monitor-at-(0,0)
    assumption) must default to origin (0, 0) so their export crops
    behave exactly as before; new rows must round-trip a nonzero
    origin so secondary-monitor crops can translate screen-space
    rects into image space.
    """
    _build_v1_session(tmp_path, "Legacy-Screens")

    # Pre-seed a screenshot row in the pre-v7 shape.
    db_path = tmp_path / "Legacy-Screens" / "session.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO screenshot_artifacts "
            "(relative_path, captured_at, width, height) "
            "VALUES ('screenshots/old.png', '2026-04-24T00:00:00+00:00', 800, 600)"
        )
        conn.commit()
    finally:
        conn.close()

    repo = SessionRepository.open_existing(workspace_root=tmp_path, slug="Legacy-Screens")
    try:
        legacy = repo.list_screenshots()[0]
        assert legacy.origin_left == 0
        assert legacy.origin_top == 0

        fresh = repo.add_screenshot(
            relative_path="screenshots/second-monitor.png",
            captured_at=utcnow(),
            width=800,
            height=600,
            sha256="abc",
            origin_left=1920,
            origin_top=-120,
        )
        assert fresh.origin_left == 1920
        assert fresh.origin_top == -120
        reloaded = [
            s for s in repo.list_screenshots() if s.id == fresh.id
        ]
        assert reloaded[0].origin_left == 1920
        assert reloaded[0].origin_top == -120
    finally:
        repo.close()
