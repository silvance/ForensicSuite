"""Translate raw storage / IO exception text into operator guidance.

Operators see the title and body of a QMessageBox; what we put in
``str(exc)`` is what they read. Default raw exception text — sqlite3
constraint failures, ``[Errno 28] No space left on device``,
``[WinError 5] Access is denied`` — leaks implementation detail and
gives no guidance on what to do next.

The function below maps common failure modes to a guided message that
states what went wrong in plain English and what the operator can try.
The raw exception is appended verbatim at the bottom so debugging
information isn't lost.

Each app's controllers wrap their `QMessageBox.warning(...)` /
`QMessageBox.critical(...)` calls through this helper rather than
calling ``str(exc)`` directly.
"""

from __future__ import annotations

import errno
import sqlite3

#: Suffix appended to every guided message so the original exception
#: text is preserved for support / debugging. Operators can ignore it;
#: support engineers can read it.
_RAW_PREFIX = "Original error: "


def friendly_storage_error(exc: BaseException, *, what: str) -> str:
    """Translate ``exc`` into operator-facing text describing what failed.

    ``what`` is a short description of the operation the operator
    was attempting, e.g. ``"save the case"`` or ``"open the session"``.
    It's woven into the guided message so a "permission denied" error
    on a save reads differently from one on an open.

    The raw exception is always appended at the end so a support
    engineer reading a screenshot can still see the underlying cause.
    """
    raw = str(exc).strip()
    guided = _guide(exc, what=what)
    if guided is None:
        return raw
    if not raw or raw in guided:
        return guided
    return f"{guided}\n\n{_RAW_PREFIX}{raw}"


def _guide(exc: BaseException, *, what: str) -> str | None:
    """Pick a guided message for ``exc`` or return None for fallback.

    Order matters: more specific exception types are matched before
    their parents. ``isinstance`` checks would normally catch the
    parent first, so we explicitly check OSError errno codes and
    string patterns before falling through to the generic StorageError
    handler.
    """
    # OSError covers disk-full, permission-denied, file-not-found etc.
    # at the OS layer. errno is the most reliable signal because the
    # Windows-localised text varies by display language.
    if isinstance(exc, OSError):
        guided = _guide_os_error(exc, what=what)
        if guided is not None:
            return guided

    # SQLite "database is locked" -- another tool has the case open.
    if isinstance(exc, sqlite3.OperationalError):
        msg_lower = str(exc).lower()
        if "database is locked" in msg_lower:
            return (
                f"Couldn't {what} -- the case database is currently locked.\n\n"
                "Close any other tool that might have this case open and "
                "try again. If no other tool is open, the lock may be "
                "stale -- restart the app to clear it."
            )

    # String-based heuristics on the exception message. These cover
    # the cases where the storage layer raises a generic StorageError
    # with descriptive text the user could act on.
    lower = str(exc).lower()
    if "already exists" in lower or "alreadyexistserror" in type(exc).__name__.lower():
        kind = _infer_kind(what)
        return (
            f"A {kind} with that name already exists.\n\n"
            "Pick a different name, or open the existing one from the "
            "browser."
        )
    if "is locked" in lower or "lockederror" in type(exc).__name__.lower():
        return (
            f"Couldn't {what} -- another window has this open.\n\n"
            "Close the other window and try again. If you're sure nothing "
            "else is using it, restart the app to clear a stale lock."
        )
    if (
        "schema_version" in lower
        or "schema version" in lower
        or "schemaversionerror" in type(exc).__name__.lower()
    ):
        # SchemaVersionError messages already include the recovery
        # path ("Update Inscription or use a compatible build."),
        # so don't double up -- pass them through verbatim.
        return None
    if "not found" in lower or "notfounderror" in type(exc).__name__.lower():
        kind = _infer_kind(what)
        return (
            f"Couldn't find this {kind} on disk.\n\n"
            "It may have been moved, renamed, or deleted outside the app. "
            "Open the browser and try again."
        )
    return None


def _guide_os_error(exc: OSError, *, what: str) -> str | None:
    """Map OSError errno values to guided messages."""
    code = exc.errno
    if code == errno.ENOSPC:
        return (
            "The disk is full.\n\n"
            f"Free up space on the drive holding your workspace and "
            f"try to {what} again."
        )
    if code in (errno.EACCES, errno.EPERM):
        return (
            f"Access denied while trying to {what}.\n\n"
            "Check that you have permission to write to the workspace "
            "folder, and close any program that might have a file open "
            "there (Explorer preview, antivirus scan, another editor)."
        )
    if code == errno.ENOENT:
        return (
            f"A file we expected to find while trying to {what} is "
            "missing.\n\n"
            "It may have been moved or deleted outside the app. Reopen "
            "the case from the browser to refresh."
        )
    if code in (errno.EROFS,):
        return (
            f"Couldn't {what} -- the destination is read-only.\n\n"
            "Move the workspace to a writable drive, or check folder "
            "properties."
        )
    if code == errno.EBUSY:
        return (
            f"Couldn't {what} -- a file is in use by another program.\n\n"
            "Close any tool that might have it open and try again."
        )
    # Windows-only: file-locked-by-another-process surfaces as
    # WinError 32 (sharing violation) with errno=EACCES sometimes
    # and EBUSY others. Both are covered above. WinError 5 (access
    # denied) also maps to EACCES, which is fine.
    return None


def _infer_kind(what: str) -> str:
    """Pull "case" / "session" / "suggestion" out of the ``what`` blurb.

    Lets the guided message use the noun the operator was acting on
    without each call site having to pass it explicitly.
    """
    lower = what.lower()
    if "session" in lower:
        return "session"
    if "case" in lower:
        return "case"
    if "suggestion" in lower:
        return "suggestion"
    return "item"


def show_error(
    parent: object,
    *,
    title: str,
    what: str,
    exc: BaseException,
    critical: bool = False,
) -> None:
    """Convenience: format the error and show it via QMessageBox.

    Lazy-imports PySide6 so this module stays importable from
    non-UI contexts (e.g. CLI tools, headless tests). ``critical`` picks
    between the warning and critical icon variants; both block until
    dismissed and look identical to operators.
    """
    from PySide6.QtWidgets import QMessageBox

    body = friendly_storage_error(exc, what=what)
    method = QMessageBox.critical if critical else QMessageBox.warning
    method(parent, title, body)
