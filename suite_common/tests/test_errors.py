"""Tests for the operator-friendly error helpers."""

from __future__ import annotations

import errno
import sqlite3


from suite_common.errors import friendly_storage_error


def test_returns_raw_text_for_unknown_exception() -> None:
    """Falls through to the original message when no guidance applies.

    Guards against the helper accidentally hiding useful detail just
    because the exception type wasn't recognised.
    """
    exc = RuntimeError("something weird happened")
    assert friendly_storage_error(exc, what="do the thing") == "something weird happened"


def test_disk_full_says_disk_full() -> None:
    exc = OSError(errno.ENOSPC, "No space left on device")
    body = friendly_storage_error(exc, what="save the case")
    assert "disk is full" in body.lower()
    assert "save the case" in body


def test_permission_denied_suggests_check_perms() -> None:
    exc = OSError(errno.EACCES, "Permission denied")
    body = friendly_storage_error(exc, what="save the case")
    assert "access denied" in body.lower()
    assert "save the case" in body
    assert "Original error" in body  # raw exception preserved


def test_already_exists_picks_kind_from_what() -> None:
    """The ``what`` blurb supplies the noun for the guided message."""

    class _CaseExists(Exception):
        pass

    exc = _CaseExists("Workspace 'foo' already exists")
    body = friendly_storage_error(exc, what="create the case")
    assert "case" in body.lower()
    assert "already exists" in body.lower()


def test_already_exists_uses_session_kind_for_session_what() -> None:
    """Same exception text, different ``what`` -> different noun."""
    exc = RuntimeError("Session 'foo' already exists in workspace")
    body = friendly_storage_error(exc, what="open the session")
    assert "session" in body.lower()


def test_schema_version_passes_through_to_raw() -> None:
    """SchemaVersionError messages already explain the recovery path;
    re-wrapping them would just duplicate the guidance."""

    class _SchemaVersionError(Exception):
        pass

    exc = _SchemaVersionError(
        "schema_version 7 is newer than this Inscription build supports (max 6). "
        "Update Inscription or use a compatible build."
    )
    assert friendly_storage_error(exc, what="open the session") == str(exc)


def test_sqlite_database_locked_suggests_close_other_tools() -> None:
    exc = sqlite3.OperationalError("database is locked")
    body = friendly_storage_error(exc, what="save the case")
    assert "locked" in body.lower()
    assert "close" in body.lower()


def test_locked_string_in_generic_exception() -> None:
    """A custom LockedError surfaces the same guidance."""

    class _SessionLockedError(Exception):
        pass

    exc = _SessionLockedError("Session is locked by another process")
    body = friendly_storage_error(exc, what="open the session")
    assert "locked" in body.lower()


def test_not_found_says_moved_or_deleted() -> None:
    exc = OSError(errno.ENOENT, "No such file or directory")
    body = friendly_storage_error(exc, what="open the case")
    assert "missing" in body.lower() or "moved" in body.lower()


def test_raw_text_appended_unless_already_in_guided_text() -> None:
    """If the guided text already contains the raw message verbatim,
    don't duplicate it. Lets a custom exception with a long message
    be the only thing shown when guidance can't improve on it."""
    exc = OSError(errno.ENOSPC, "No space left on device")
    body = friendly_storage_error(exc, what="export")
    # The raw text is appended as "Original error: ..."
    assert body.count("No space left on device") == 1


def test_show_error_lazy_imports_qt() -> None:
    """The convenience wrapper imports PySide6 lazily so the rest of
    the module is usable from headless contexts."""
    # We don't import Qt at module import time; show_error pulls it
    # in when called. Just verify importing the helper doesn't blow up
    # without a Qt platform plugin.
    from suite_common.errors import show_error  # noqa: F401, PLC0415
