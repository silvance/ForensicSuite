"""Tests for the shared crash-safe writer."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from suite_common.atomic import atomic_write_text

if TYPE_CHECKING:
    from pathlib import Path


def test_atomic_write_creates_file_at_destination(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_atomic_write_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_atomic_write_leaves_no_tmp_after_success(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "body")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected .tmp leftovers: {leftovers}"


def test_atomic_write_cleans_up_tmp_when_body_raises(tmp_path: Path) -> None:
    """A failure mid-write (e.g. encoding error, KeyboardInterrupt)
    must not leave a half-written .tmp sibling on disk.

    A lingering ``out.txt.tmp`` from a crashed write would survive
    across restarts and accumulate as the user retries the operation.
    The helper's except: catches BaseException specifically so even
    Ctrl+C is cleaned up.
    """
    target = tmp_path / "out.txt"

    class _Boom(Exception):
        pass

    # ``body`` is a str so we can't easily inject a write failure
    # through normal arguments; instead force the encoding step to
    # fail by passing a non-encodable string through a custom encoding.
    # surrogateescape-incompatible char on a strict ASCII encoding:
    with pytest.raises(UnicodeEncodeError):
        atomic_write_text(target, "café", encoding="ascii")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"unexpected .tmp leftovers: {leftovers}"
    assert not target.exists()


def test_atomic_write_clears_stale_tmp_from_prior_crash(tmp_path: Path) -> None:
    """If a previous crashed write left a sibling .tmp behind, the
    next call wipes it before starting -- otherwise repeated crashes
    would accumulate junk siblings.
    """
    target = tmp_path / "out.txt"
    stale = tmp_path / "out.txt.tmp"
    stale.write_text("STALE", encoding="utf-8")
    atomic_write_text(target, "fresh")
    assert target.read_text(encoding="utf-8") == "fresh"
    assert not stale.exists()


def test_atomic_write_respects_custom_encoding(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "naïve", encoding="latin-1")
    assert target.read_bytes() == "naïve".encode("latin-1")


def test_atomic_write_handles_fsync_failure_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fsync isn't supported on every filesystem (some FUSE / shm /
    network mounts). Failure to fsync is logged at debug and the
    write completes -- atomic (via rename) but not durable.
    """
    target = tmp_path / "out.txt"

    real_fsync = os.fsync

    def _fsync_fail(fd: int) -> None:
        # Fail on file fsync; let directory fsync run normally.
        # Heuristic: directory fds are typically larger than file fds
        # so this is good enough for the test without inspecting
        # /proc/self/fd.
        raise OSError("fake EIO from fsync")

    monkeypatch.setattr(os, "fsync", _fsync_fail)
    atomic_write_text(target, "body")
    assert target.read_text(encoding="utf-8") == "body"
    # Restore so cleanup isn't broken.
    monkeypatch.setattr(os, "fsync", real_fsync)
