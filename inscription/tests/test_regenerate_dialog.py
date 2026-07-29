"""Regenerate worker: off-thread step generation with late-show dialog."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

pytest.importorskip("pytestqt")

from inscription.model import EventKind
from inscription.storage import SessionRepository
from inscription.ui.regenerate_dialog import run_regenerate

if TYPE_CHECKING:
    from pathlib import Path


def test_run_regenerate_completes_and_reports_success(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The worker rebuilds steps off-thread and fires on_success on the
    GUI thread; the progress dialog never appears for a fast regen
    (regenerate fires on EVERY recording stop -- flashing a modal for
    a 50 ms rebuild would be worse than the freeze it replaces)."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="RegenFast")
    try:
        repo.append_event(kind=EventKind.CLICK, x=1, y=1, button="left",
                          window_title="App")
        results: list[str] = []
        worker = run_regenerate(
            repo,
            parent=None,
            on_success=lambda: results.append("ok"),
            on_failure=lambda msg: results.append(f"fail:{msg}"),
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not results:
            qtbot.wait(20)
        worker.wait(2000)
        assert results == ["ok"]
        assert len(repo.list_steps()) == 1
    finally:
        repo.close()


def test_run_regenerate_reports_failure(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A repository error surfaces via on_failure, not an unhandled
    exception on the worker thread."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="RegenFail")
    try:
        results: list[str] = []
        repo.close()  # closed repo -> generate_steps raises
        worker = run_regenerate(
            repo,
            parent=None,
            on_success=lambda: results.append("ok"),
            on_failure=lambda msg: results.append("fail"),
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not results:
            qtbot.wait(20)
        worker.wait(2000)
        assert results == ["fail"]
    finally:
        pass
