"""High-level persistence API for sessions.

This is the only module higher layers (controllers, UI) should use to talk
to session storage. It wraps SQLite, the filesystem layout, manifest I/O,
and lockfile management.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from inscription.model import (
    SCHEMA_VERSION,
    DraftStep,
    EventKind,
    RawEvent,
    ResolvedElement,
    ScreenshotArtifact,
    Session,
    SessionInfo,
    SessionManifest,
    utcnow,
)
from inscription.storage import locking
from inscription.storage._rowmap import (
    parse_iso,
    row_to_element,
    row_to_event,
    row_to_screenshot,
    row_to_step,
)
from inscription.storage.errors import (
    SessionAlreadyExistsError,
    SessionNotFoundError,
    StorageError,
)
from inscription.storage.manifest import read_manifest, write_manifest
from inscription.storage.schema import initialise_schema, migrate_to_latest

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

logger = logging.getLogger(__name__)

_SAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]")
_COLLAPSE_DASH = re.compile(r"-{2,}")


def slugify(name: str) -> str:
    """Filesystem-safe slug for ``name``.

    Alphanumerics, ``.``, ``_``, ``-`` pass through; other characters
    become ``-``; runs of ``-`` collapse; leading/trailing ``-`` strip;
    empty results raise ``ValueError``.
    """
    slug = _COLLAPSE_DASH.sub("-", _SAFE_SLUG.sub("-", name.strip())).strip("-")
    if not slug:
        msg = f"Name {name!r} produces an empty slug"
        raise ValueError(msg)
    return slug


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _dumps_rect(rect: tuple[int, int, int, int] | None) -> str | None:
    if rect is None:
        return None
    return json.dumps(list(rect))


#: A step needs at least this many source events to be splittable in two.
_MIN_SOURCE_IDS_TO_SPLIT = 2


class SessionRepository:
    """Persistence API for a single open session."""

    def __init__(self, session: Session, conn: sqlite3.Connection) -> None:
        self._session = session
        self._conn = conn
        self._lock = threading.Lock()

    def _commit(self, *, what: str) -> None:
        """Commit the open transaction, surfacing failures as StorageError.

        SQLite can fail at commit time for reasons the operator needs to
        know about — disk full, lock contention, constraint violation
        deferred to commit. The plain ``self._conn.commit()`` call would
        let those propagate as ``sqlite3.Error`` and most callers swallow
        them with a bare ``except Exception:`` that logs but doesn't
        explain. Funnel every commit through here so the message points
        at the operation that failed.
        """
        try:
            self._conn.commit()
        except sqlite3.Error as exc:
            msg = f"Failed to persist {what}: {exc}"
            raise StorageError(msg) from exc

    @contextmanager
    def _transaction(self, *, what: str):  # type: ignore[no-untyped-def]
        """Group a write into one atomic transaction.

        Acquires ``self._lock`` for the duration so other threads can't
        interleave queries. Commits on clean exit; rolls back on any
        exception before re-raising. Without the rollback, a partial
        write that raised mid-statement would leave SQLite's implicit
        transaction open, and the *next* write method's commit would
        accidentally persist that partial state along with its own --
        the failure mode the field-quality reviews flagged.

        Always yield from the same lock-held scope so callers can
        write::

            with self._transaction(what="replace_steps"):
                self._conn.execute("DELETE ...")
                for ...:
                    self._conn.execute("INSERT ...")

        Read-only methods don't need this -- they just acquire
        ``self._lock`` directly. Writes that consist of a single
        ``execute()`` could in principle skip it (a failed statement
        doesn't open a transaction), but routing every writer through
        ``_transaction`` keeps the contract uniform and protects
        against a later refactor turning a single-statement writer
        into a multi-statement one without remembering to add
        rollback handling.
        """
        with self._lock:
            try:
                yield
            except BaseException:
                try:
                    self._conn.rollback()
                except sqlite3.Error:
                    # Rollback itself failing is rare but possible
                    # (disk full, db locked); log and continue
                    # propagating the original exception.
                    logger.exception("rollback after failed %s also failed", what)
                raise
            self._commit(what=what)

    # ---------------------------------------------------------- lifecycle

    @classmethod
    def create(
        cls, *, workspace_root: Path, name: str, recorder_version: str = ""
    ) -> SessionRepository:
        """Create a new session directory and open it."""
        slug = slugify(name)
        root = workspace_root / slug
        if root.exists():
            msg = f"Session directory {root} already exists"
            raise SessionAlreadyExistsError(msg)

        now = utcnow()
        info = SessionInfo(name=name, started_at=now, recorder_version=recorder_version)
        session = Session(info=info, root=root)

        session.screenshots_dir.mkdir(parents=True, exist_ok=False)
        session.exports_dir.mkdir(parents=True, exist_ok=False)
        session.internal_dir.mkdir(parents=True, exist_ok=False)
        locking.acquire(session.internal_dir / "session.lock")

        conn = sqlite3.connect(session.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            initialise_schema(conn)
            conn.execute(
                """
                INSERT INTO session_info
                    (id, name, started_at, ended_at, recorder_version, schema_version)
                VALUES (1, ?, ?, NULL, ?, ?)
                """,
                (info.name, _iso(info.started_at), recorder_version, SCHEMA_VERSION),
            )
            conn.commit()
        except Exception:
            conn.close()
            locking.release(session.internal_dir / "session.lock")
            raise

        repo = cls(session, conn)
        repo._write_manifest()
        logger.info("Created session %r at %s", name, root)
        return repo

    @classmethod
    def open_existing(cls, *, workspace_root: Path, slug: str) -> SessionRepository:
        root = workspace_root / slug
        if not root.exists():
            msg = f"Session {slug!r} not found in {workspace_root}"
            raise SessionNotFoundError(msg)
        return cls._open_at_root(root)

    @classmethod
    def _open_at_root(cls, root: Path) -> SessionRepository:
        if not (root / "session.db").exists():
            msg = f"No session.db under {root}"
            raise SessionNotFoundError(msg)
        (root / "screenshots").mkdir(exist_ok=True)
        (root / "exports").mkdir(exist_ok=True)
        (root / ".inscription").mkdir(exist_ok=True)
        locking.acquire(root / ".inscription" / "session.lock")

        conn = sqlite3.connect(root / "session.db", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            migrate_to_latest(conn)
            info = cls._load_info(conn)
        except Exception:
            conn.close()
            locking.release(root / ".inscription" / "session.lock")
            raise

        session = Session(info=info, root=root)
        return cls(session, conn)

    def close(self) -> None:
        try:
            self._write_manifest()
        finally:
            self._conn.close()
            locking.release(self._session.internal_dir / "session.lock")
        logger.info("Closed session %r", self._session.info.name)

    def __enter__(self) -> SessionRepository:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ----------------------------------------------------------- queries

    @property
    def session(self) -> Session:
        return self._session

    @staticmethod
    def _load_info(conn: sqlite3.Connection) -> SessionInfo:
        row = conn.execute("SELECT * FROM session_info WHERE id = 1").fetchone()
        if row is None:
            msg = "session_info row missing from database"
            raise StorageError(msg)
        return SessionInfo(
            name=row["name"],
            started_at=parse_iso(row["started_at"]),
            ended_at=parse_iso(row["ended_at"]) if row["ended_at"] else None,
            recorder_version=row["recorder_version"],
            schema_version=row["schema_version"],
        )

    def list_events(self) -> list[RawEvent]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM raw_events ORDER BY sequence").fetchall()
        return [row_to_event(r) for r in rows]

    def list_steps(self, *, include_suppressed: bool = False) -> list[DraftStep]:
        with self._lock:
            if include_suppressed:
                rows = self._conn.execute("SELECT * FROM draft_steps ORDER BY sequence").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM draft_steps WHERE suppressed = 0 ORDER BY sequence"
                ).fetchall()
        return [row_to_step(r) for r in rows]

    def get_step(self, step_id: int) -> DraftStep | None:
        """Read one step by id, or ``None`` when no such row exists.

        Used by the undo machinery to snapshot a step's pre-edit state
        before a command runs.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM draft_steps WHERE id = ?", (step_id,)
            ).fetchone()
        return row_to_step(row) if row else None

    def list_screenshots(self) -> list[ScreenshotArtifact]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM screenshot_artifacts ORDER BY id").fetchall()
        return [row_to_screenshot(r) for r in rows]

    def get_screenshot(self, screenshot_id: int) -> ScreenshotArtifact | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM screenshot_artifacts WHERE id = ?", (screenshot_id,)
            ).fetchone()
        return row_to_screenshot(row) if row else None

    def get_resolved_element(self, element_id: int) -> ResolvedElement | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM resolved_elements WHERE id = ?", (element_id,)
            ).fetchone()
        return row_to_element(row) if row else None

    # --------------------------------------------------------- mutations

    def end_session(self) -> None:
        with self._transaction(what="end_session"):
            self._conn.execute(
                "UPDATE session_info SET ended_at = ? WHERE id = 1 AND ended_at IS NULL",
                (_iso(utcnow()),),
            )

    def add_screenshot(
        self,
        *,
        relative_path: str,
        captured_at: datetime,
        width: int,
        height: int,
        sha256: str = "",
        highlight_rect: tuple[int, int, int, int] | None = None,
    ) -> ScreenshotArtifact:
        with self._transaction(what="add_screenshot"):
            cursor = self._conn.execute(
                """
                INSERT INTO screenshot_artifacts
                    (relative_path, captured_at, width, height, sha256, highlight_rect)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    relative_path,
                    _iso(captured_at),
                    width,
                    height,
                    sha256,
                    _dumps_rect(highlight_rect),
                ),
            )
            screenshot_id = cursor.lastrowid
            if screenshot_id is None:
                msg = "INSERT into screenshots did not return a row id"
                raise StorageError(msg)
        return ScreenshotArtifact(
            id=screenshot_id,
            relative_path=relative_path,
            captured_at=captured_at,
            width=width,
            height=height,
            sha256=sha256,
            highlight_rect=highlight_rect,
        )

    def add_resolved_element(self, element: ResolvedElement) -> ResolvedElement:
        with self._transaction(what="add_resolved_element"):
            cursor = self._conn.execute(
                """
                INSERT INTO resolved_elements
                    (name, control_type, automation_id, class_name, role,
                     confidence, method, bounding_rect, owner_process_name,
                     nearby_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    element.name,
                    element.control_type,
                    element.automation_id,
                    element.class_name,
                    element.role,
                    element.confidence,
                    element.method,
                    _dumps_rect(element.bounding_rect),
                    element.owner_process_name,
                    element.nearby_text,
                ),
            )
            element_id = cursor.lastrowid
            if element_id is None:
                msg = "INSERT into resolved_elements did not return a row id"
                raise StorageError(msg)
        return dataclasses.replace(element, id=element_id)

    def append_event(
        self,
        *,
        kind: EventKind,
        occurred_at: datetime | None = None,
        button: str | None = None,
        x: int | None = None,
        y: int | None = None,
        key: str | None = None,
        text: str | None = None,
        window_title: str | None = None,
        process_name: str | None = None,
        screenshot_id: int | None = None,
        resolved_element_id: int | None = None,
    ) -> RawEvent:
        occurred_at = occurred_at or utcnow()
        with self._transaction(what="append_event"):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM raw_events"
            ).fetchone()
            next_seq: int = row[0]
            cursor = self._conn.execute(
                """
                INSERT INTO raw_events
                    (sequence, occurred_at, kind, button, x, y, key, text,
                     window_title, process_name, screenshot_id,
                     resolved_element_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_seq,
                    _iso(occurred_at),
                    kind.value,
                    button,
                    x,
                    y,
                    key,
                    text,
                    window_title,
                    process_name,
                    screenshot_id,
                    resolved_element_id,
                ),
            )
            event_id = cursor.lastrowid
            if event_id is None:
                msg = "INSERT into raw_events did not return a row id"
                raise StorageError(msg)
        return RawEvent(
            id=event_id,
            sequence=next_seq,
            occurred_at=occurred_at,
            kind=kind,
            button=button,
            x=x,
            y=y,
            key=key,
            text=text,
            window_title=window_title,
            process_name=process_name,
            screenshot_id=screenshot_id,
            resolved_element_id=resolved_element_id,
        )

    def replace_steps(self, steps: list[DraftStep]) -> list[DraftStep]:
        """Replace every draft step with ``steps``, reassigning sequences.

        Used by the step generator after grouping raw events.
        """
        saved: list[DraftStep] = []
        with self._transaction(what="replace_steps"):
            self._conn.execute("DELETE FROM draft_steps")
            for i, step in enumerate(steps, start=1):
                cursor = self._conn.execute(
                    """
                    INSERT INTO draft_steps
                        (sequence, action, result, source_event_ids, screenshot_id,
                         suppressed, manual_edit, evidentiary)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        i,
                        step.action,
                        step.result,
                        json.dumps(list(step.source_event_ids)),
                        step.screenshot_id,
                        1 if step.suppressed else 0,
                        1 if step.manual_edit else 0,
                        1 if step.evidentiary else 0,
                    ),
                )
                step_id = cursor.lastrowid
                if step_id is None:
                    msg = "INSERT into draft_steps did not return a row id"
                    raise StorageError(msg)
                saved.append(dataclasses.replace(step, id=step_id, sequence=i))
        return saved

    def append_step(self, step: DraftStep) -> DraftStep:
        """Insert a single step at the end of the list.

        Used by the live step generator while a recording is in progress —
        each new event either extends the previous step or starts a new
        one via this method. Sequence is auto-assigned to ``MAX(sequence) + 1``.
        """
        with self._transaction(what="append_step"):
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM draft_steps"
            ).fetchone()
            next_sequence = (row[0] or 0) + 1
            cursor = self._conn.execute(
                """
                INSERT INTO draft_steps
                    (sequence, action, result, source_event_ids, screenshot_id,
                     suppressed, manual_edit, evidentiary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    next_sequence,
                    step.action,
                    step.result,
                    json.dumps(list(step.source_event_ids)),
                    step.screenshot_id,
                    1 if step.suppressed else 0,
                    1 if step.manual_edit else 0,
                    1 if step.evidentiary else 0,
                ),
            )
            new_id = cursor.lastrowid
            if new_id is None:
                msg = "INSERT into draft_steps did not return a row id"
                raise StorageError(msg)
        return dataclasses.replace(step, id=new_id, sequence=next_sequence)

    def extend_step_sources(
        self,
        step_id: int,
        *,
        extra_event_ids: tuple[int, ...],
        screenshot_id: int | None = None,
        action: str | None = None,
    ) -> None:
        """Append more source events (and optionally a screenshot) to a step.

        Used when the live generator decides a new event collapses into
        the previous step. Pass ``action`` to also overwrite the step's
        action text (e.g. updating "Press Backspace" → "Press Backspace
        9 times" as repeats arrive). When ``action`` is ``None`` the
        text is preserved.

        Does NOT set ``manual_edit``; coalesced steps remain eligible
        for the post-recording AI rewrite.
        """
        if not extra_event_ids:
            return
        with self._transaction(what="extend_step_sources"):
            row = self._conn.execute(
                "SELECT source_event_ids, screenshot_id FROM draft_steps WHERE id = ?",
                (step_id,),
            ).fetchone()
            if row is None:
                msg = f"extend_step_sources: no step with id {step_id}"
                raise StorageError(msg)
            existing = tuple(json.loads(row["source_event_ids"]))
            combined = (*existing, *extra_event_ids)
            shot = row["screenshot_id"] if row["screenshot_id"] is not None else screenshot_id
            if action is not None:
                self._conn.execute(
                    "UPDATE draft_steps SET source_event_ids = ?, screenshot_id = ?, "
                    "action = ? WHERE id = ?",
                    (json.dumps(list(combined)), shot, action, step_id),
                )
            else:
                self._conn.execute(
                    "UPDATE draft_steps SET source_event_ids = ?, screenshot_id = ? "
                    "WHERE id = ?",
                    (json.dumps(list(combined)), shot, step_id),
                )

    def update_step_fields(
        self,
        step_id: int,
        *,
        action: str | None = None,
        result: str | None = None,
        manual_edit: bool = True,
    ) -> None:
        """Update the action and/or result columns.

        Either or both fields may be supplied; ``None`` means "leave alone".
        ``manual_edit`` defaults to True because the typical caller is the
        examiner editing in the workspace — the resulting row should be
        protected from the next Regenerate-Steps pass. The undo path
        passes ``manual_edit=False`` (or whatever the row's prior flag
        was) to restore the pre-edit state faithfully.
        """
        if action is None and result is None:
            return
        sets: list[str] = []
        params: list[object] = []
        if action is not None:
            sets.append("action = ?")
            params.append(action)
        if result is not None:
            sets.append("result = ?")
            params.append(result)
        sets.append("manual_edit = ?")
        params.append(1 if manual_edit else 0)
        params.append(step_id)
        with self._transaction(what="update_step_fields"):
            self._conn.execute(
                f"UPDATE draft_steps SET {', '.join(sets)} WHERE id = ?",
                params,
            )

    def set_step_suppressed(self, step_id: int, *, suppressed: bool) -> None:
        with self._transaction(what="set_step_suppressed"):
            self._conn.execute(
                "UPDATE draft_steps SET suppressed = ? WHERE id = ?",
                (1 if suppressed else 0, step_id),
            )

    def set_step_evidentiary(self, step_id: int, *, evidentiary: bool) -> None:
        """Mark or unmark a step as evidentiary.

        Downstream report-builder tools query this flag to pull the
        examiner-curated subset of steps into the final forensic report.
        """
        with self._transaction(what="set_step_evidentiary"):
            self._conn.execute(
                "UPDATE draft_steps SET evidentiary = ? WHERE id = ?",
                (1 if evidentiary else 0, step_id),
            )

    def reorder_steps(self, ordered_step_ids: list[int]) -> None:
        """Rewrite sequence numbers to match ``ordered_step_ids``."""
        with self._transaction(what="reorder_steps"):
            for i, step_id in enumerate(ordered_step_ids, start=1):
                self._conn.execute(
                    "UPDATE draft_steps SET sequence = ? WHERE id = ?",
                    (i, step_id),
                )

    def merge_steps(self, *, primary_id: int, other_id: int) -> DraftStep:
        """Merge ``other_id`` into ``primary_id``; delete the other row.

        The merged step keeps ``primary_id`` and its screenshot. Source
        event ids are concatenated (primary first, other appended). The
        action and result strings are joined with a space; empties on
        either side are dropped. Marks the result as ``manual_edit``
        because the user has clearly intervened.
        """
        with self._transaction(what="merge_steps"):
            primary_row = self._conn.execute(
                "SELECT * FROM draft_steps WHERE id = ?", (primary_id,)
            ).fetchone()
            other_row = self._conn.execute(
                "SELECT * FROM draft_steps WHERE id = ?", (other_id,)
            ).fetchone()
            if primary_row is None or other_row is None:
                msg = f"merge_steps: missing step ({primary_id=}, {other_id=})"
                raise StorageError(msg)

            primary = row_to_step(primary_row)
            other = row_to_step(other_row)

            combined_ids = (*primary.source_event_ids, *other.source_event_ids)
            merged_action = _join_text(primary.action, other.action)
            merged_result = _join_text(primary.result, other.result)
            # Union the per-step flags: if either half was marked
            # evidentiary the merged step stays in the report, and if
            # either was suppressed we treat the merged result as still
            # hidden until the operator explicitly un-suppresses it.
            merged_evidentiary = primary.evidentiary or other.evidentiary
            merged_suppressed = primary.suppressed or other.suppressed

            self._conn.execute(
                """
                UPDATE draft_steps
                SET action = ?, result = ?, source_event_ids = ?,
                    evidentiary = ?, suppressed = ?, manual_edit = 1
                WHERE id = ?
                """,
                (
                    merged_action,
                    merged_result,
                    json.dumps(list(combined_ids)),
                    1 if merged_evidentiary else 0,
                    1 if merged_suppressed else 0,
                    primary_id,
                ),
            )
            self._conn.execute("DELETE FROM draft_steps WHERE id = ?", (other_id,))
        return dataclasses.replace(
            primary,
            action=merged_action,
            result=merged_result,
            source_event_ids=combined_ids,
            evidentiary=merged_evidentiary,
            suppressed=merged_suppressed,
            manual_edit=True,
        )

    def split_step(self, step_id: int) -> tuple[DraftStep, DraftStep]:
        """Split ``step_id`` into two: first source event vs the rest.

        The original step keeps its ``id`` and screenshot but loses every
        source event after the first. A new row is inserted directly after
        with the remaining source events. Both halves are marked
        ``manual_edit``.

        Raises :class:`StorageError` if the step has fewer than two
        source events (nothing to split).
        """
        with self._transaction(what="split_step"):
            row = self._conn.execute(
                "SELECT * FROM draft_steps WHERE id = ?", (step_id,)
            ).fetchone()
            if row is None:
                msg = f"split_step: no step with id {step_id}"
                raise StorageError(msg)
            step = row_to_step(row)
            if len(step.source_event_ids) < _MIN_SOURCE_IDS_TO_SPLIT:
                count = len(step.source_event_ids)
                msg = f"split_step: step {step_id} has only {count} source event(s); cannot split"
                raise StorageError(msg)

            head = (step.source_event_ids[0],)
            tail = step.source_event_ids[1:]

            # Bump every later step's sequence by 1 so we can insert the
            # tail half directly after this one without sequence collisions.
            self._conn.execute(
                "UPDATE draft_steps SET sequence = sequence + 1 WHERE sequence > ?",
                (step.sequence,),
            )

            cursor = self._conn.execute(
                """
                INSERT INTO draft_steps
                    (sequence, action, result, source_event_ids, screenshot_id,
                     suppressed, evidentiary, manual_edit)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    step.sequence + 1,
                    step.action,
                    step.result,
                    json.dumps(list(tail)),
                    step.screenshot_id,
                    1 if step.suppressed else 0,
                    1 if step.evidentiary else 0,
                ),
            )
            new_id = cursor.lastrowid
            if new_id is None:
                msg = "INSERT into draft_steps (split tail) did not return a row id"
                raise StorageError(msg)

            # Trim the original to just the first event; keep its text so
            # the user can edit each half independently.
            self._conn.execute(
                """
                UPDATE draft_steps
                SET source_event_ids = ?, manual_edit = 1
                WHERE id = ?
                """,
                (json.dumps(list(head)), step_id),
            )

        first = dataclasses.replace(step, source_event_ids=head, manual_edit=True)
        second = dataclasses.replace(
            step,
            id=new_id,
            sequence=step.sequence + 1,
            source_event_ids=tail,
            manual_edit=True,
        )
        return first, second

    def set_step_screenshot(self, step_id: int, screenshot_id: int | None) -> None:
        with self._transaction(what="set_step_screenshot"):
            self._conn.execute(
                "UPDATE draft_steps SET screenshot_id = ? WHERE id = ?",
                (screenshot_id, step_id),
            )

    # ----------------------------------------------------------- manifest

    def _write_manifest(self) -> None:
        with self._lock:
            event_row = self._conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()
            step_row = self._conn.execute(
                "SELECT COUNT(*) FROM draft_steps WHERE suppressed = 0"
            ).fetchone()
            info = self._load_info(self._conn)
        manifest = SessionManifest(
            name=info.name,
            started_at=info.started_at,
            ended_at=info.ended_at,
            event_count=event_row[0],
            step_count=step_row[0],
        )
        write_manifest(self._session.manifest_path, manifest)

    def flush_manifest(self) -> None:
        """Re-derive and write the manifest. Call after bulk mutations."""
        self._write_manifest()

    # Row → dataclass mapping moved to inscription.storage._rowmap;
    # the read methods above import them directly.


# --------------------------------------------------------------- free funcs


def _join_text(left: str, right: str) -> str:
    """Concatenate two step texts, dropping empties."""
    parts = [s.strip() for s in (left, right) if s and s.strip()]
    return " ".join(parts)


def list_sessions(workspace_root: Path) -> list[tuple[str, SessionManifest]]:
    """Return ``(slug, manifest)`` pairs for every session in ``workspace_root``.

    Directories without a manifest are silently skipped.
    """
    if not workspace_root.exists():
        return []
    out: list[tuple[str, SessionManifest]] = []
    for child in sorted(workspace_root.iterdir()):
        if not child.is_dir():
            continue
        manifest_path = child / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            out.append((child.name, read_manifest(manifest_path)))
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Skipping malformed manifest at %s: %s", manifest_path, exc)
    return out
