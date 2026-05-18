"""Tests for the per-session submitted marker.

Two layers covered:

- The marker file itself (read / mark / clear; defensive against
  missing or corrupt files).
- The controller wiring: mutation slots no-op when submitted, and
  the export flow's "mark submitted?" prompt is offered exactly
  when the controller flags it.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QAbstractItemView

from inscription.model import DraftStep, EventKind, ResolvedElement
from inscription.storage import SessionRepository, SubmittedMarker, submitted
from inscription.ui import controller_exports
from inscription.ui.step_editor import StepEditorPanel
from inscription.ui.step_list import StepListWidget
from inscription.ui.workspace import SessionWorkspaceWidget

if TYPE_CHECKING:
    from pathlib import Path


# ----------------------------------------------------- marker file API


def test_read_returns_none_when_no_marker(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="NoMarker")
    try:
        assert submitted.read(repo.session) is None
    finally:
        repo.close()


def test_mark_then_read_round_trips(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="MarkRead")
    try:
        marker = submitted.mark(
            repo.session,
            examiner="Alex Smith",
            export_format="Forensic notes",
        )
        assert marker.examiner == "Alex Smith"
        assert marker.export_format == "Forensic notes"

        loaded = submitted.read(repo.session)
        assert loaded is not None
        assert loaded.examiner == "Alex Smith"
        assert loaded.export_format == "Forensic notes"
        # submitted_at is per-call; just sanity-check it's recent.
        now = datetime.now(UTC)
        assert (now - loaded.submitted_at) < timedelta(minutes=5)
    finally:
        repo.close()


def test_clear_removes_marker(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Clear")
    try:
        submitted.mark(repo.session)
        assert submitted.read(repo.session) is not None
        submitted.clear(repo.session)
        assert submitted.read(repo.session) is None
        # Clearing twice is a no-op (no exception).
        submitted.clear(repo.session)
    finally:
        repo.close()


def test_read_handles_corrupt_marker(tmp_path: Path) -> None:
    """A truncated / corrupt marker file shouldn't lock the operator
    out forever -- treat as 'not submitted' and let them mark it
    again if they want."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="Corrupt")
    try:
        # Write garbage to the marker path.
        marker_path = repo.session.internal_dir / "submitted.json"
        marker_path.write_text("{not valid json", encoding="utf-8")
        assert submitted.read(repo.session) is None
    finally:
        repo.close()


def test_read_handles_missing_submitted_at_field(tmp_path: Path) -> None:
    """A marker file with a missing submitted_at field is not a
    valid marker -- treat as 'not submitted'."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="MissingField")
    try:
        marker_path = repo.session.internal_dir / "submitted.json"
        marker_path.write_text('{"examiner": "Alex"}', encoding="utf-8")
        assert submitted.read(repo.session) is None
    finally:
        repo.close()


def test_read_rejects_naive_submitted_at(tmp_path: Path) -> None:
    """Naive datetimes are corrupt for forensic timestamping -- the
    marker is rejected so a banner rendered on another machine in
    another timezone can't misread the time."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="NaiveTs")
    try:
        marker_path = repo.session.internal_dir / "submitted.json"
        # Naive ISO8601: no offset, no Z.
        marker_path.write_text(
            '{"submitted_at": "2026-05-08T12:00:00"}', encoding="utf-8"
        )
        assert submitted.read(repo.session) is None
    finally:
        repo.close()


def test_read_rejects_oversized_marker(tmp_path: Path) -> None:
    """A marker file larger than the cap is treated as corrupt
    (defensive against a tampered or runaway file silently filling
    RAM during read_text)."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="HugeMarker")
    try:
        marker_path = repo.session.internal_dir / "submitted.json"
        # 128 KB of valid-looking JSON (well past the 64 KB cap).
        body = '{"submitted_at": "2026-05-08T12:00:00+00:00", "padding": "'
        body += "x" * (128 * 1024)
        body += '"}'
        marker_path.write_text(body, encoding="utf-8")
        assert submitted.read(repo.session) is None
    finally:
        repo.close()


def test_mark_writes_atomically(tmp_path: Path) -> None:
    """mark() goes through a .tmp file + os.replace so a crash
    mid-write can't leave a half-written marker the reader treats
    as 'not submitted', silently dropping the evidentiary lock."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="AtomicMark")
    try:
        submitted.mark(repo.session)
        # Final file is in place; no .tmp sibling left behind.
        marker_path = repo.session.internal_dir / "submitted.json"
        tmp_path_stale = repo.session.internal_dir / "submitted.json.tmp"
        assert marker_path.exists()
        assert not tmp_path_stale.exists()
        # Sanity: the file is fully readable JSON, not a partial.
        marker = submitted.read(repo.session)
        assert marker is not None
        assert marker.submitted_at.tzinfo is not None
    finally:
        repo.close()


def test_mark_creates_internal_dir(tmp_path: Path) -> None:
    """Defensive: mark() should create .inscription/ if it's missing."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="MakeDir")
    try:
        # Wipe the internal dir to simulate a session in an unusual state.
        shutil.rmtree(repo.session.internal_dir)
        submitted.mark(repo.session)
        assert (repo.session.internal_dir / "submitted.json").exists()
    finally:
        repo.close()


# ----------------------------------------------------- controller gating


pytest.importorskip("pytestqt")

from inscription.ui.controller import SessionController  # noqa: E402


class _FakeWorkspace:
    """Minimal stand-in for SessionWorkspaceWidget. Records reload calls
    and stub-implements every signal the controller connects to."""

    def __init__(self) -> None:
        # pyqtSignal-like stubs: tracked attribute that the controller
        # calls .connect(slot) on. Tests don't fire signals through
        # these; they just need to exist for SessionController.__init__.
        class _Stub(QObject):
            step_fields_edited = Signal(int, str, str)
            step_suppressed = Signal(int, bool)
            step_evidentiary_toggled = Signal(int, bool)
            steps_reordered = Signal(list)
            merge_requested = Signal(int, int)
            split_requested = Signal(int)
            draft_step_requested = Signal(object)
            reopen_requested = Signal()

        self._stub = _Stub()
        self.set_repository_calls = 0
        self.set_submitted_marker_calls: list[object] = []
        self.reload_calls = 0

        # Forward signal attributes for controller introspection.
        self.step_fields_edited = self._stub.step_fields_edited
        self.step_suppressed = self._stub.step_suppressed
        self.step_evidentiary_toggled = self._stub.step_evidentiary_toggled
        self.steps_reordered = self._stub.steps_reordered
        self.merge_requested = self._stub.merge_requested
        self.split_requested = self._stub.split_requested
        self.draft_step_requested = self._stub.draft_step_requested
        self.reopen_requested = self._stub.reopen_requested

    def set_repository(self, repo: object) -> None:
        self.set_repository_calls += 1

    def clear_repository(self) -> None: ...
    def set_case_dir(self, _case_dir: object) -> None: ...
    def reload(self) -> None:
        self.reload_calls += 1
    def flush_pending(self) -> None: ...

    def set_submitted_marker(self, marker: object) -> None:
        self.set_submitted_marker_calls.append(marker)


class _FakeRecorderBar:
    """Minimum recorder bar interface the controller wires to."""

    def __init__(self) -> None:
        class _Stub(QObject):
            record_toggled = Signal(bool)
            marker_requested = Signal()

        self._stub = _Stub()
        self.record_toggled = self._stub.record_toggled
        self.marker_requested = self._stub.marker_requested

    def set_session_name(self, _name: object) -> None: ...
    def set_event_count(self, _count: int) -> None: ...
    def set_recording(self, _recording: bool) -> None: ...


def _seed_repo_with_steps(repo: SessionRepository) -> int:
    """Seed one click event + one step. Returns the step id."""
    resolved = repo.add_resolved_element(
        ResolvedElement(id=None, name="Save", control_type="Button", confidence=0.9, method="uia")
    )
    event = repo.append_event(
        kind=EventKind.CLICK, button="left", x=1, y=1,
        window_title="Notepad", process_name="notepad.exe",
        resolved_element_id=resolved.id,
    )
    assert event.id is not None
    saved = repo.replace_steps([
        DraftStep(
            id=None, sequence=0,
            action="Click Save", source_event_ids=(event.id,),
        ),
    ])
    sid = saved[0].id
    assert sid is not None
    return sid


def test_controller_blocks_step_edit_when_submitted(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """When the marker is set, _on_step_fields_edited returns silently
    instead of pushing an undo command + writing to the repo."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="GatedEdit")
    try:
        sid = _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        # Plug the repo in through the controller's _activate path.
        controller._activate(repo)
        original = repo.get_step(sid)
        assert original is not None

        # Mark the session as submitted; subsequent edits must no-op.
        controller.mark_session_submitted()
        controller._on_step_fields_edited(sid, "Should not persist", "")
        after = repo.get_step(sid)
        assert after is not None
        assert after.action == original.action
        assert controller._undo_stack.count() == 0
    finally:
        repo.close()


def test_controller_allows_edits_when_marker_cleared(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """After clearing the marker, edits go through normally."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="UngatedEdit")
    try:
        sid = _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        controller._activate(repo)

        controller.mark_session_submitted()
        controller.reopen_session_for_editing()

        controller._on_step_fields_edited(sid, "Now this persists", "")
        after = repo.get_step(sid)
        assert after is not None
        assert after.action == "Now this persists"
        assert controller._undo_stack.count() == 1
    finally:
        repo.close()


def test_mark_session_submitted_sets_banner_and_clears_undo(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """mark_session_submitted pushes the marker to the workspace and
    drops any pending undo entries (so undoing past the submission
    isn't possible)."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="MarkAPI")
    try:
        sid = _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        controller._activate(repo)
        # Push one editable command so we can verify the stack clears.
        controller._on_step_fields_edited(sid, "Edited", "")
        assert controller._undo_stack.count() == 1

        controller.mark_session_submitted(export_format="Forensic notes")

        assert controller.is_session_submitted() is True
        assert controller._undo_stack.count() == 0
        # The most-recent set_submitted_marker call carried a non-None
        # marker (initial _activate call passed None for not-yet-submitted).
        last = ws.set_submitted_marker_calls[-1]
        assert last is not None
    finally:
        repo.close()


def test_reopen_session_clears_marker(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    repo = SessionRepository.create(workspace_root=tmp_path, name="Reopen")
    try:
        _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        controller._activate(repo)
        controller.mark_session_submitted(export_format="Forensic notes")
        assert controller.is_session_submitted() is True

        controller.reopen_session_for_editing()
        assert controller.is_session_submitted() is False
        # Last set_submitted_marker call carries None (banner hidden).
        assert ws.set_submitted_marker_calls[-1] is None
    finally:
        repo.close()


def test_export_methods_pass_valid_kwargs_to_run_export(  # type: ignore[no-untyped-def]
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the merge artefact where export_forensic_notes /
    export_pdf were passing offer_submit=True to run_export, which
    only accepts on_complete. The call shape used to TypeError at
    runtime; mock the file dialog to cancel and confirm both methods
    return cleanly.
    """
    monkeypatch.setattr(
        controller_exports.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **kw: ("", "")),
    )
    repo = SessionRepository.create(workspace_root=tmp_path, name="ExportShape")
    try:
        _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        controller._activate(repo)
        # Both methods cancel cleanly because the dialog returned "".
        # Before the fix, export_forensic_notes raised TypeError on
        # the run_export call.
        controller.export_forensic_notes()
        controller.export_pdf()
    finally:
        repo.close()


# ----------------------------------------------------- read-only widget propagation


def test_workspace_set_submitted_marker_propagates_to_children(qtbot, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """SessionWorkspaceWidget should forward read-only state to its
    list / editor / suggestions children when the marker is set,
    and re-enable them when cleared."""
    workspace = SessionWorkspaceWidget()
    qtbot.addWidget(workspace)

    # Sanity: brand-new workspace -> children all editable.
    assert workspace._list._read_only is False
    assert workspace._editor._read_only is False
    assert workspace._suggestions._read_only is False

    workspace.set_submitted_marker(
        SubmittedMarker(submitted_at=datetime.now(UTC), examiner="Alex")
    )
    assert workspace._list._read_only is True
    assert workspace._editor._read_only is True
    assert workspace._suggestions._read_only is True

    workspace.set_submitted_marker(None)
    assert workspace._list._read_only is False
    assert workspace._editor._read_only is False
    assert workspace._suggestions._read_only is False


def test_step_editor_read_only_disables_text_edits(qtbot) -> None:  # type: ignore[no-untyped-def]
    """The action / result text edits become QTextEdit.readOnly under
    set_read_only(True), so a user typing into them produces no change."""
    editor = StepEditorPanel()
    qtbot.addWidget(editor)
    editor.set_read_only(True)
    assert editor._action.isReadOnly() is True
    assert editor._result.isReadOnly() is True

    editor.set_read_only(False)
    assert editor._action.isReadOnly() is False
    assert editor._result.isReadOnly() is False


def test_step_list_read_only_disables_drag_drop(qtbot) -> None:  # type: ignore[no-untyped-def]
    """DragDropMode flips to NoDragDrop when read-only -- prevents the
    operator from dragging a row to a new position."""
    lst = StepListWidget()
    qtbot.addWidget(lst)
    # Default: InternalMove (drag-drop allowed).
    assert lst.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove

    lst.set_read_only(True)
    assert lst.dragDropMode() == QAbstractItemView.DragDropMode.NoDragDrop

    lst.set_read_only(False)
    assert lst.dragDropMode() == QAbstractItemView.DragDropMode.InternalMove


# ----------------------------------------------------- export kwargs regression


def test_export_controller_methods_call_run_export_with_valid_kwargs(  # type: ignore[no-untyped-def]
    qtbot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both deliverable-class export controller methods must call
    ``run_export`` with kwargs the helper actually accepts.

    Regression for the bug where ``export_forensic_notes`` passed
    ``offer_submit=True`` -- a kwarg ``run_export`` doesn't accept --
    crashing the menu action with TypeError on every click. Cancel
    via getSaveFileName so we don't actually render anything; we just
    want to confirm the kwargs the controller passes are valid.
    """
    from PySide6.QtWidgets import QFileDialog

    repo = SessionRepository.create(workspace_root=tmp_path, name="ExportKwargs")
    try:
        _seed_repo_with_steps(repo)
        ws = _FakeWorkspace()
        rb = _FakeRecorderBar()
        controller = SessionController(
            workspace=ws,  # type: ignore[arg-type]
            recorder_bar=rb,  # type: ignore[arg-type]
        )
        controller._activate(repo)

        monkeypatch.setattr(
            QFileDialog,
            "getSaveFileName",
            staticmethod(lambda *a, **k: ("", "")),  # user cancels
        )
        # Each call would raise TypeError before reaching the cancel
        # branch if the controller passed an unknown kwarg.
        controller.export_forensic_notes()
        controller.export_pdf()
        controller.export_html()
        controller.export_markdown()
    finally:
        repo.close()
