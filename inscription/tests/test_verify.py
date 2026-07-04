"""Integrity verification: clean / mismatched / missing / unreadable / unhashed."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from inscription import verify as verify_module
from inscription.model import utcnow
from inscription.storage import SessionRepository
from inscription.verify import verify_session_integrity

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _stage_screenshot(repo: SessionRepository, *, name: str, body: bytes) -> str:
    """Write a PNG into the session's screenshots/ and register it.

    Returns the relative path so the test can mutate the file on disk.
    """
    rel = f"screenshots/{name}"
    target = repo.session.root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    repo.add_screenshot(
        relative_path=rel,
        captured_at=utcnow(),
        width=1,
        height=1,
        sha256=hashlib.sha256(body).hexdigest(),
    )
    return rel


def test_verify_clean_session(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Clean")
    try:
        _stage_screenshot(repo, name="a.png", body=b"first-png-bytes")
        _stage_screenshot(repo, name="b.png", body=b"second-png-bytes")
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    assert result.total_checked == 2
    assert result.ok == 2
    assert result.is_clean is True
    assert not result.mismatched
    assert not result.missing
    assert not result.unhashed


def test_verify_detects_mismatched_screenshot(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Tampered")
    try:
        rel = _stage_screenshot(repo, name="a.png", body=b"original")
        # Mutate the file on disk after registration.
        (repo.session.root / rel).write_bytes(b"tampered-with-bytes")
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    assert result.total_checked == 1
    assert result.ok == 0
    assert len(result.mismatched) == 1
    assert result.mismatched[0].relative_path == rel
    assert result.is_clean is False


def test_verify_detects_missing_screenshot(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Missing")
    try:
        rel = _stage_screenshot(repo, name="a.png", body=b"will-be-deleted")
        (repo.session.root / rel).unlink()
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    assert result.missing == [rel]
    assert result.is_clean is False


def test_verify_flags_unhashed_rows_as_warnings(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Legacy")
    try:
        rel = "screenshots/legacy.png"
        target = repo.session.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"legacy-without-stored-hash")
        # Register without a sha256 — the very-early-alpha case.
        repo.add_screenshot(
            relative_path=rel,
            captured_at=utcnow(),
            width=1,
            height=1,
            sha256="",
        )
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    assert result.unhashed == [rel]
    # Unhashed rows are warnings, not failures.
    assert result.is_clean is True
    assert result.has_warnings is True


def test_verify_progress_callback_fires_for_every_row(tmp_path: Path) -> None:
    """The progress callback gets (0, total) before the loop and
    (i+1, total) after each row -- the verify_progress_dialog uses
    that to drive the progress bar."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="Progress")
    try:
        for n in range(3):
            png = repo.session.root / "screenshots" / f"shot{n}.png"
            png.parent.mkdir(parents=True, exist_ok=True)
            png.write_bytes(b"fake-png-bytes")
            sha = hashlib.sha256(b"fake-png-bytes").hexdigest()
            repo.add_screenshot(
                relative_path=f"screenshots/shot{n}.png",
                captured_at=utcnow(),
                width=1,
                height=1,
                sha256=sha,
            )

        progress: list[tuple[int, int]] = []
        result = verify_session_integrity(
            repo, progress_callback=lambda d, t: progress.append((d, t))
        )

        # Initial 0/total + one tick per row.
        assert progress[0] == (0, 3)
        assert progress[-1] == (3, 3)
        assert len(progress) == 4
        assert result.ok == 3
    finally:
        repo.close()


def test_verify_no_progress_callback_when_omitted(tmp_path: Path) -> None:
    """CLI / test paths can leave progress_callback at the default."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="NoCb")
    try:
        # No screenshots, no callback -- must not raise.
        result = verify_session_integrity(repo)
        assert result.total_checked == 0
    finally:
        repo.close()


# ------------------------------------------------------- unreadable files


def test_verify_classifies_unreadable_file_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable file must NOT abort the scan.

    Regression: _hash_file's open() raced against the exists() check --
    a file locked by antivirus / Explorer preview (common on Windows),
    or deleted mid-scan, raised OSError and aborted the ENTIRE
    integrity check. The operator saw a scan-wide error dialog instead
    of a verdict on the other N-1 screenshots plus a pointer at the
    one file that couldn't be read.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="Unreadable")
    try:
        _stage_screenshot(repo, name="good.png", body=b"fine")
        locked_rel = _stage_screenshot(repo, name="locked.png", body=b"locked")
        _stage_screenshot(repo, name="also-good.png", body=b"also fine")

        real_hash_file = verify_module._hash_file

        def _hash_or_lock(path: Path) -> str:
            if path.name == "locked.png":
                msg = "The process cannot access the file"
                raise OSError(msg)
            return real_hash_file(path)

        monkeypatch.setattr(verify_module, "_hash_file", _hash_or_lock)
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    # Scan completed: both readable files got verdicts.
    assert result.ok == 2
    assert result.unreadable == [locked_rel]
    assert not result.mismatched
    assert not result.missing


def test_unreadable_file_is_not_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unverified file must never render as a clean report.

    'We couldn't check' and 'we checked and it matches' are different
    forensic statements; is_clean collapsing them would let a locked
    (possibly tampered) file hide behind a green checkmark.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="NotClean")
    try:
        _stage_screenshot(repo, name="locked.png", body=b"x")

        def _always_locked(_path: Path) -> str:
            msg = "Permission denied"
            raise OSError(msg)

        monkeypatch.setattr(verify_module, "_hash_file", _always_locked)
        result = verify_session_integrity(repo)
    finally:
        repo.close()

    assert result.is_clean is False
    assert len(result.unreadable) == 1
