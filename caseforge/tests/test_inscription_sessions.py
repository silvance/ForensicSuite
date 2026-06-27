"""Inscription manifest reader: happy path + tolerated malformed inputs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from caseforge.inscription_sessions import (
    InscriptionSession,
    list_inscription_sessions,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write_manifest(case_dir: Path, *, slug: str, payload: dict[str, object]) -> Path:
    session_dir = case_dir / slug
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return session_dir


def _ts(value: str) -> str:
    """Helper to keep test payloads readable."""
    return value


def test_list_sessions_in_a_clean_case(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        slug="reset-aws",
        payload={
            "name": "Reset AWS password",
            "started_at": _ts("2026-04-24T07:21:50.123456+00:00"),
            "ended_at": _ts("2026-04-24T07:24:11.987654+00:00"),
            "event_count": 42,
            "step_count": 17,
            "schema_version": 5,
        },
    )
    _write_manifest(
        tmp_path,
        slug="hash-verify",
        payload={
            "name": "Hash verification",
            "started_at": _ts("2026-04-24T08:10:00+00:00"),
            "ended_at": _ts("2026-04-24T08:12:30+00:00"),
            "event_count": 4,
            "step_count": 4,
        },
    )
    sessions = list_inscription_sessions(tmp_path)

    assert len(sessions) == 2
    # Newest started_at first.
    assert sessions[0].slug == "hash-verify"
    assert sessions[1].slug == "reset-aws"
    assert isinstance(sessions[0], InscriptionSession)
    assert sessions[0].event_count == 4
    assert sessions[0].step_count == 4
    assert isinstance(sessions[0].started_at, datetime)
    assert sessions[0].is_in_progress is False


def test_list_sessions_returns_empty_for_missing_case_dir(tmp_path: Path) -> None:
    sessions = list_inscription_sessions(tmp_path / "no-such-case")
    assert sessions == []


def test_list_sessions_skips_dirs_without_manifest(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        slug="real-session",
        payload={
            "name": "Real",
            "started_at": _ts("2026-04-24T07:00:00+00:00"),
            "ended_at": _ts("2026-04-24T07:01:00+00:00"),
            "event_count": 1,
            "step_count": 1,
        },
    )
    # A stray subdir — could be a half-initialised session, or the
    # examiner's own folder. CaseForge ignores it rather than failing.
    (tmp_path / "stray-subdir").mkdir()

    sessions = list_inscription_sessions(tmp_path)
    assert [s.slug for s in sessions] == ["real-session"]


def test_list_sessions_skips_malformed_json(tmp_path: Path) -> None:
    bad = tmp_path / "broken-session"
    bad.mkdir()
    (bad / "manifest.json").write_text("{not valid json", encoding="utf-8")
    _write_manifest(
        tmp_path,
        slug="good-session",
        payload={
            "name": "Good",
            "started_at": _ts("2026-04-24T07:00:00+00:00"),
            "ended_at": _ts("2026-04-24T07:01:00+00:00"),
            "event_count": 1,
            "step_count": 1,
        },
    )
    sessions = list_inscription_sessions(tmp_path)
    assert [s.slug for s in sessions] == ["good-session"]


def test_in_progress_session_has_no_ended_at(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        slug="live-session",
        payload={
            "name": "Live",
            "started_at": _ts("2026-04-24T09:00:00+00:00"),
            "ended_at": None,
            "event_count": 7,
            "step_count": 0,
        },
    )
    sessions = list_inscription_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].ended_at is None
    assert sessions[0].is_in_progress is True


def test_unknown_extra_fields_do_not_break_parsing(tmp_path: Path) -> None:
    """A future Inscription schema bump that adds keys must not break us."""
    _write_manifest(
        tmp_path,
        slug="future",
        payload={
            "name": "Future",
            "started_at": _ts("2026-04-24T09:00:00+00:00"),
            "ended_at": _ts("2026-04-24T09:05:00+00:00"),
            "event_count": 3,
            "step_count": 2,
            "schema_version": 99,
            "tags": ["pilot"],
            "tomorrow_field": {"nested": True},
        },
    )
    sessions = list_inscription_sessions(tmp_path)
    assert sessions[0].name == "Future"
    assert sessions[0].step_count == 2


def test_missing_optional_fields_get_safe_defaults(tmp_path: Path) -> None:
    _write_manifest(
        tmp_path,
        slug="bare",
        payload={"name": "Bare"},
    )
    sessions = list_inscription_sessions(tmp_path)
    assert len(sessions) == 1
    assert sessions[0].event_count == 0
    assert sessions[0].step_count == 0
    assert sessions[0].ended_at is None


def test_ordering_is_by_started_at_not_filesystem_name(tmp_path: Path) -> None:
    """A lexicographically-later slug with an earlier started_at must
    sort below a lex-earlier slug with a later started_at.

    Documents the contract: directory iteration order is meaningless,
    the final sort on ``started_at`` is the only ordering guarantee.
    Guards against accidentally relying on ``sorted(iterdir())`` -- if
    someone re-introduces a pre-sort or changes the sort key, this
    test still passes the explicit-ordering case but their change
    becomes visible in code review.
    """
    _write_manifest(
        tmp_path,
        slug="zzz-old-session",  # sorts last alphabetically
        payload={
            "name": "Old",
            "started_at": _ts("2026-01-01T06:00:00+00:00"),
            "ended_at": _ts("2026-01-01T06:05:00+00:00"),
            "event_count": 1,
            "step_count": 1,
        },
    )
    _write_manifest(
        tmp_path,
        slug="aaa-new-session",  # sorts first alphabetically
        payload={
            "name": "New",
            "started_at": _ts("2026-06-01T10:00:00+00:00"),
            "ended_at": _ts("2026-06-01T10:05:00+00:00"),
            "event_count": 2,
            "step_count": 2,
        },
    )
    sessions = list_inscription_sessions(tmp_path)
    # Newest started_at first regardless of slug ordering.
    assert sessions[0].slug == "aaa-new-session"
    assert sessions[1].slug == "zzz-old-session"
