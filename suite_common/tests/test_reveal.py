"""Tests for the platform-specific reveal-in-file-manager helper."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from suite_common.ui.reveal import reveal_in_file_manager


def test_reveal_returns_false_when_path_missing(tmp_path: Path) -> None:
    """A path that doesn't exist returns False without raising.

    Defensive: a controller calling this from a "show in folder"
    button shouldn't have to wrap it in try/except.
    """
    missing = tmp_path / "does-not-exist"
    assert reveal_in_file_manager(missing) is False


def test_reveal_windows_launches_explorer_with_select(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows we shell out to explorer.exe /select,<path>."""
    target = tmp_path / "report.html"
    target.write_text("ok", encoding="utf-8")

    calls: list[list[str]] = []

    def _record_popen(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        return object()

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "Popen", _record_popen)
    assert reveal_in_file_manager(target) is True
    assert calls == [["explorer.exe", "/select,", str(target)]]


def test_reveal_macos_launches_open_with_dash_R(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS we shell out to `open -R <path>` to reveal in Finder."""
    target = tmp_path / "report.docx"
    target.write_text("ok", encoding="utf-8")

    calls: list[list[str]] = []

    def _record_popen(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        return object()

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _record_popen)
    assert reveal_in_file_manager(target) is True
    assert calls == [["open", "-R", str(target)]]


def test_reveal_linux_opens_parent_via_xdg_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linux xdg-open doesn't support per-file selection; open the parent."""
    target = tmp_path / "checklist.md"
    target.write_text("ok", encoding="utf-8")

    calls: list[list[str]] = []

    def _record_popen(args: list[str], **_kwargs: object) -> object:
        calls.append(args)
        return object()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/xdg-open")
    monkeypatch.setattr(subprocess, "Popen", _record_popen)
    assert reveal_in_file_manager(target) is True
    assert calls == [["xdg-open", str(target.parent)]]


def test_reveal_linux_returns_false_when_xdg_open_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A headless box without xdg-open shouldn't crash, just decline."""
    target = tmp_path / "report.html"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert reveal_in_file_manager(target) is False


def test_reveal_swallows_oserror_from_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If Popen itself raises (e.g. binary missing on PATH despite
    shutil.which finding it earlier), we log and return False, not
    propagate. UX nicety, never critical path."""
    target = tmp_path / "report.html"
    target.write_text("ok", encoding="utf-8")

    def _explode(*_args: object, **_kwargs: object) -> object:
        msg = "no such binary"
        raise OSError(msg)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(subprocess, "Popen", _explode)
    assert reveal_in_file_manager(target) is False
