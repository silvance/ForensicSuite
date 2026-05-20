"""Application controller.

Owns the :class:`SessionRepository`, the :class:`CaptureEngine`, and the
session lifecycle. Dialogs and widgets emit Qt signals; the controller
translates those into repository mutations and UI updates.

Global hotkeys also live here:

- Ctrl+Shift+R — toggle recording. Registered while a session is open so
  the user can stop recording without touching the Inscription window
  (which would otherwise land in the final screenshot).
- Ctrl+Shift+M — drop a marker during recording. Registered only while
  recording is active.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QUndoStack
from PySide6.QtWidgets import (
    QDialog,
    QInputDialog,
    QMessageBox,
    QWidget,
)

from inscription import __version__
from inscription.capture import (
    CaptureEngine,
    ClickSource,
    EnrichedEvent,
    KeyboardMilestoneSource,
    RawCaptureEvent,
    ScrollSource,
    SessionSink,
    WindowFocusSource,
)
from inscription.config import Config
from inscription.export import (
    export_forensic_notes,
    export_html,
    export_markdown,
    export_pdf,
)
from inscription.llm import LLMClient, LLMError, StepRewriter
from inscription.model import DraftStep, EventKind, utcnow
from inscription.paths import WORKSPACE_DIR
from inscription.platform import (
    CAPTURE_FULLY_SUPPORTED,
    HotkeyBinding,
    HotkeyManager,
    create_foreground_inspector,
    create_hotkey_manager,
    create_screen_capturer,
    safe_close,
)
from inscription.resolve import create_element_resolver
from inscription.steps import LiveStepGenerator, generate_steps
from inscription.storage import (
    SessionAlreadyExistsError,
    SessionLockedError,
    SessionRepository,
    SubmittedMarker,
    list_sessions,
)
from inscription.storage import (
    submitted as submitted_marker,
)
from inscription.ui.controller_errors import friendly_llm_error as _friendly_llm_error
from inscription.ui.controller_exports import run_export
from inscription.ui.qt_capture_bridge import QtCaptureBridge
from inscription.ui.rewrite_dialog import RewriteProgressDialog, RewriteWorker
from inscription.ui.session_dialogs import SessionListDialog
from inscription.ui.settings_dialog import SettingsDialog
from inscription.ui.undo_commands import (
    EditStepFieldsCommand,
    ReorderStepsCommand,
    SetStepEvidentiaryCommand,
    SetStepSuppressedCommand,
    SnapshotAndReplaceCommand,
)
from inscription.ui.verify_dialog import IntegrityResultDialog
from inscription.ui.verify_progress_dialog import VerifyProgressDialog, VerifyWorker

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from inscription.caseguide_link import CaseguideSuggestion
    from inscription.model import ExportDocument
    from inscription.ui.recorder_bar import RecorderBar
    from inscription.ui.workspace import SessionWorkspaceWidget
    from inscription.verify import IntegrityResult

logger = logging.getLogger(__name__)

TOGGLE_RECORD_HOTKEY = "<ctrl>+<shift>+r"
MARKER_HOTKEY = "<ctrl>+<shift>+m"
SNAPSHOT_HOTKEY = "<ctrl>+<shift>+p"


class SessionController(QObject):
    """Coordinates the open session, the capture engine, and the UI."""

    session_opened = Signal(str)  # session name
    session_closed = Signal()
    event_counted = Signal(int)  # total events in current session
    recording_state_changed = Signal(bool)  # True when recording started, False when stopped
    #: Emitted after every live-generator update with (latest_step, started_at).
    #: ``latest_step`` is a :class:`DraftStep` or None; ``started_at`` is its
    #: first-source-event timestamp (or None when nothing's been captured).
    latest_step_changed = Signal(object, object)

    #: Queued across threads so the pynput hotkey callback can flip state
    #: on the Qt main thread without race conditions.
    _toggle_requested = Signal()
    _marker_requested = Signal()
    _snapshot_requested = Signal()
    #: Fired by the live step generator (capture worker thread) whenever
    #: a step is appended or extended; the queued connection bounces
    #: workspace.reload() onto the Qt main thread.
    _live_steps_changed = Signal()

    def __init__(
        self,
        *,
        workspace: SessionWorkspaceWidget,
        recorder_bar: RecorderBar,
        parent_widget: QWidget | None = None,
        parent: QObject | None = None,
        case_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace = workspace
        self._recorder_bar = recorder_bar
        self._parent_widget = parent_widget
        self._config = Config()
        # Per-run override; doesn't persist to config. Used by CaseForge
        # integration: when CaseForge launches Inscription with --case-dir,
        # all sessions for this run land inside that directory.
        self._case_dir = case_dir

        self._repository: SessionRepository | None = None
        self._engine: CaptureEngine | None = None
        self._bridge: QtCaptureBridge | None = None
        self._sink: SessionSink | None = None
        self._click_source: ClickSource | None = None
        self._window_source: WindowFocusSource | None = None
        self._event_count = 0
        self._hotkeys: HotkeyManager = create_hotkey_manager()
        # One undo stack per controller instance (one per window). Cleared
        # on session open / close so commands never reach across sessions.
        # Workspace mutations push commands here instead of calling the
        # repository directly; Edit -> Undo / Redo (Ctrl+Z / Ctrl+Y) walks
        # the stack via the actions exposed by ``undo_action`` /
        # ``redo_action``.
        self._undo_stack: QUndoStack = QUndoStack(self)
        # Bounded so a long editing session of SnapshotAndReplaceCommands
        # (each holding two full step lists) doesn't accumulate
        # unboundedly. 100 is generous for an examiner workflow.
        self._undo_stack.setUndoLimit(100)

        # Cache of the open session's submitted marker. Populated in
        # _activate, invalidated in _close_session, and updated through
        # mark_session_submitted / reopen_session_for_editing. See
        # is_session_submitted for the rationale.
        self._submitted_marker: SubmittedMarker | None = None

        self._workspace.step_fields_edited.connect(self._on_step_fields_edited)
        self._workspace.step_suppressed.connect(self._on_step_suppressed)
        self._workspace.step_evidentiary_toggled.connect(self._on_step_evidentiary_toggled)
        self._workspace.steps_reordered.connect(self._on_steps_reordered)
        self._workspace.merge_requested.connect(self._on_merge_requested)
        self._workspace.split_requested.connect(self._on_split_requested)
        self._workspace.draft_step_requested.connect(self._on_draft_step_requested)
        self._workspace.reopen_requested.connect(self._on_reopen_requested)
        self._recorder_bar.record_toggled.connect(self._on_record_toggled)
        self._recorder_bar.marker_requested.connect(self._on_marker_requested)
        self._toggle_requested.connect(self._on_toggle_hotkey)
        self._marker_requested.connect(self._on_marker_hotkey)
        self._snapshot_requested.connect(self._on_snapshot_hotkey)
        self._live_steps_changed.connect(self._on_live_steps_changed)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Show the session picker. Called from the main window on launch."""
        self._show_session_picker()

    def open_session_by_slug(self, slug: str) -> None:
        """Open the session identified by ``slug`` from the welcome page."""
        self._open_session(slug)

    def workspace_root(self) -> Path:
        """Return the directory the welcome page should enumerate."""
        return self._workspace_root()

    def open_settings(self) -> None:
        """Show the Settings dialog (examiner identity + LLM endpoint)."""
        SettingsDialog(self._config, parent=self._parent_widget).exec()

    def verify_integrity(self) -> None:
        """Re-hash every screenshot in the open session and report drift.

        Disabled when no session is open; the menu wiring already
        guards against that, but we double-check here to keep this
        method safe for callers (hotkeys, scripts) that might not.
        """
        if self._repository is None:
            QMessageBox.information(
                self._parent_widget,
                "No session",
                "Open a session before running an integrity check.",
            )
            return
        # The hash pass is CPU-bound and can take seconds on a large
        # case; previously this ran synchronously with a wait cursor
        # and froze the event loop. Lift onto a QThread with a
        # progress dialog (mirrors the rewrite-with-AI flow).
        worker = VerifyWorker(self._repository)
        dialog = VerifyProgressDialog(worker, parent=self._parent_widget)
        dialog.succeeded.connect(self._show_integrity_result)
        dialog.failed.connect(self._show_integrity_failure)
        dialog.start()
        dialog.exec()

    def _show_integrity_result(self, result: IntegrityResult) -> None:
        IntegrityResultDialog(result, parent=self._parent_widget).exec()

    def _show_integrity_failure(self, message: str) -> None:
        QMessageBox.critical(
            self._parent_widget,
            "Integrity check failed",
            f"Could not run the check.\n\n{message}\n\nSee logs for details.",
        )

    def shutdown(self) -> None:
        self._workspace.flush_pending()
        self._stop_recording()
        self._close_session()
        self._hotkeys.unregister_all()

    # ---------------------------------------------------- submitted state

    def is_session_submitted(self) -> bool:
        """True when the open session has a submitted marker on disk.

        Reads from the cached marker populated at session activation
        and updated through ``mark_session_submitted`` /
        ``reopen_session_for_editing``. The cache exists because every
        mutation slot calls this gate -- on a debounced text edit
        that's per-keystroke, and a per-keystroke ``stat()`` +
        ``read_text()`` + ``json.loads()`` is wasteful, especially on
        a network-mounted case directory.
        """
        return self._submitted_marker is not None

    def mark_session_submitted(self, *, export_format: str | None = None) -> None:
        """Mark the open session as submitted; show the banner.

        ``export_format`` records what produced the submission
        ("Forensic notes", "PDF") so the banner can display it. The
        examiner string is pulled from Config so the banner shows
        "by Alex Smith" without per-call wiring.

        Clears the undo stack so undoing past the submission isn't
        possible -- the operator should see the locked state as a
        firm checkpoint, not a pending change.
        """
        if self._repository is None:
            return
        examiner = self._config.examiner_name.strip() or None
        marker = submitted_marker.mark(
            self._repository.session,
            examiner=examiner,
            export_format=export_format,
        )
        self._submitted_marker = marker
        self._workspace.set_submitted_marker(marker)
        self._undo_stack.clear()

    def reopen_session_for_editing(self) -> None:
        """Clear the submitted marker; hide the banner."""
        if self._repository is None:
            return
        submitted_marker.clear(self._repository.session)
        self._submitted_marker = None
        self._workspace.set_submitted_marker(None)

    @Slot()
    def _on_reopen_requested(self) -> None:
        """Banner's "Reopen for editing" button → confirm and clear."""
        if self._repository is None:
            return
        confirm = QMessageBox.warning(
            self._parent_widget,
            "Reopen submitted session?",
            (
                "This session was marked submitted as evidence. Reopening "
                "lets you edit it, but any changes will diverge from what's "
                "in the discovery package you already handed over.\n\n"
                "If you make changes, re-export and re-mark the session as "
                "submitted before sharing again."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            self.reopen_session_for_editing()

    def _blocked_by_submitted(self) -> bool:
        """Defensive gate for every workspace mutation slot.

        The workspace's banner already directs the operator to
        "Reopen for editing", so this guard is silent -- a stray
        signal that reaches a slot when the session is submitted
        just no-ops. Returns True when the caller should skip the
        mutation.
        """
        return self.is_session_submitted()

    # --------------------------------------------------------- session picker

    def _show_session_picker(self) -> None:
        workspace_root = self._workspace_root()
        workspace_root.mkdir(parents=True, exist_ok=True)
        sessions = list_sessions(workspace_root)
        dialog = SessionListDialog(sessions=sessions, parent=self._parent_widget)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        choice = dialog.choice()
        if choice.open_slug is not None:
            self._open_session(choice.open_slug)
        elif choice.new_name is not None:
            self._create_session(choice.new_name)

    def _workspace_root(self) -> Path:
        if self._case_dir is not None:
            return self._case_dir
        return self._config.workspace_root or WORKSPACE_DIR

    def _open_session(self, slug: str) -> None:
        self._close_session()
        try:
            repo = SessionRepository.open_existing(workspace_root=self._workspace_root(), slug=slug)
        except SessionLockedError as exc:
            QMessageBox.warning(self._parent_widget, "Session locked", str(exc))
            return
        except Exception:
            logger.exception("Failed to open session %s", slug)
            QMessageBox.critical(
                self._parent_widget,
                "Open failed",
                f"Could not open session {slug!r}. See logs for details.",
            )
            return
        self._activate(repo)

    def _create_session(self, name: str) -> None:
        self._close_session()
        try:
            repo = SessionRepository.create(
                workspace_root=self._workspace_root(),
                name=name,
                recorder_version=__version__,
            )
        except SessionAlreadyExistsError as exc:
            QMessageBox.warning(self._parent_widget, "Session exists", str(exc))
            return
        except Exception:
            logger.exception("Failed to create session %s", name)
            QMessageBox.critical(
                self._parent_widget,
                "Create failed",
                "Could not create the session. See logs for details.",
            )
            return
        self._activate(repo)

    def _activate(self, repo: SessionRepository) -> None:
        self._repository = repo
        self._workspace.set_repository(repo)
        # Clear the undo stack so commands recorded against an earlier
        # session can't accidentally apply to this one.
        self._undo_stack.clear()
        # Show / hide the read-only banner based on the on-disk marker.
        # Cache so per-keystroke is_session_submitted() doesn't hit disk.
        self._submitted_marker = submitted_marker.read(repo.session)
        self._workspace.set_submitted_marker(self._submitted_marker)
        self._event_count = len(repo.list_events())
        self._recorder_bar.set_session_name(repo.session.info.name)
        self._recorder_bar.set_event_count(self._event_count)
        self._recorder_bar.set_recording(False)
        self._register_toggle_hotkey()
        self.session_opened.emit(repo.session.info.name)

    def _register_toggle_hotkey(self) -> None:
        # On non-Windows hosts capture would produce zero-confidence
        # placeholders (no UIA, no foreground inspector). MainWindow
        # already greys out the Record button + tray entry; mirror
        # that for the global hotkey so Ctrl+Shift+R doesn't kick off
        # a useless session from outside the app.
        if not CAPTURE_FULLY_SUPPORTED:
            return
        self._hotkeys.register(
            HotkeyBinding(sequence=TOGGLE_RECORD_HOTKEY, name="toggle-record"),
            self._toggle_requested.emit,
        )

    def _close_session(self) -> None:
        if self._repository is None:
            return
        self._stop_recording()
        self._hotkeys.unregister_all()
        try:
            self._repository.end_session()
            self._repository.close()
        except Exception:
            logger.exception("Failed to close session cleanly")
        self._repository = None
        self._submitted_marker = None
        self._workspace.clear_repository()
        self._undo_stack.clear()
        self._recorder_bar.set_session_name(None)
        self._recorder_bar.set_event_count(0)
        self._recorder_bar.set_recording(False)
        self.session_closed.emit()

    @property
    def undo_stack(self) -> QUndoStack:
        """Expose the undo stack so the main window can wire its actions.

        ``MainWindow`` calls
        ``stack.createUndoAction(parent, "Undo")`` /
        ``createRedoAction(...)`` to get auto-updated menu entries with
        Ctrl+Z / Ctrl+Y shortcuts and dynamic text ("Undo Edit step 3").
        """
        return self._undo_stack

    # ------------------------------------------------------------ recording

    def _on_record_toggled(self, recording: bool) -> None:
        if recording:
            self._start_recording()
        else:
            self._stop_recording()
            if self._repository is not None:
                self._regenerate_steps()

    def _start_recording(self) -> None:
        if self._repository is None:
            QMessageBox.information(
                self._parent_widget,
                "No session",
                "Open or create a session before starting a recording.",
            )
            self._recorder_bar.set_recording(False)
            return
        if self._engine is not None:
            return

        engine = CaptureEngine(
            foreground_factory=create_foreground_inspector,
            resolver_factory=create_element_resolver,
        )

        bridge = QtCaptureBridge(parent=self)
        bridge.event_ready.connect(self._on_engine_event)
        engine.add_sink(bridge)

        sink = SessionSink(self._repository)
        engine.add_sink(sink)

        # LiveStepGenerator must run after SessionSink — it reads the
        # persisted_event_id that the sink stamps onto the event.
        live = LiveStepGenerator(
            self._repository,
            on_changed=self._live_steps_changed.emit,
        )
        engine.add_sink(live)

        engine.start()

        auto = self._config.auto_screenshot
        self._click_source = ClickSource(auto_screenshot=auto)
        engine.add_source(self._click_source)
        engine.add_source(KeyboardMilestoneSource())
        engine.add_source(ScrollSource())
        self._window_source = WindowFocusSource(
            inspector=create_foreground_inspector(),
            auto_screenshot=auto,
        )
        engine.add_source(self._window_source)

        self._hotkeys.register(
            HotkeyBinding(sequence=MARKER_HOTKEY, name="marker"),
            self._marker_requested.emit,
        )
        self._hotkeys.register(
            HotkeyBinding(sequence=SNAPSHOT_HOTKEY, name="snapshot"),
            self._snapshot_requested.emit,
        )

        self._engine = engine
        self._bridge = bridge
        self._sink = sink
        self._recorder_bar.set_recording(True)
        self.recording_state_changed.emit(True)
        logger.info("Recording started for session %r", self._repository.session.info.name)

    def _stop_recording(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:
            logger.exception("Error stopping capture engine")
        self._engine = None
        self._bridge = None
        self._sink = None
        self._click_source = None
        self._window_source = None
        # Re-register just the toggle binding so Ctrl+Shift+R still works.
        self._hotkeys.unregister_all()
        if self._repository is not None:
            self._register_toggle_hotkey()
        self._recorder_bar.set_recording(False)
        self.recording_state_changed.emit(False)

    @Slot()
    def _on_toggle_hotkey(self) -> None:
        # Queued connection from the pynput listener thread. Flip the
        # button, which fires ``record_toggled`` and runs the same path
        # as a click on the bar.
        self._recorder_bar.toggle_record()

    @Slot()
    def _on_live_steps_changed(self) -> None:
        # Capture-thread → main-thread bounce. Reload the workspace so
        # the live notes panel grows as the examiner works. Cheap enough
        # for demo-scale workflows; we'll switch to incremental updates
        # if a session ever gets big enough to feel sluggish.
        if self._repository is None:
            return
        self._workspace.reload()
        self._broadcast_latest_step()

    def _broadcast_latest_step(self) -> None:
        """Notify the compact dock (and any other listener) of the newest step."""
        if self._repository is None:
            self.latest_step_changed.emit(None, None)
            return
        steps = self._repository.list_steps()
        if not steps:
            self.latest_step_changed.emit(None, None)
            return
        latest = steps[-1]
        events_by_id = {e.id: e for e in self._repository.list_events() if e.id is not None}
        started_at = next(
            (
                events_by_id[eid].occurred_at
                for eid in latest.source_event_ids
                if eid in events_by_id
            ),
            None,
        )
        self.latest_step_changed.emit(latest, started_at)

    @Slot()
    def _on_marker_hotkey(self) -> None:
        self._submit_marker(note="")

    def _on_marker_requested(self) -> None:
        if self._engine is None:
            return
        text, ok = QInputDialog.getText(self._parent_widget, "Marker", "Note (optional):")
        if ok:
            self._submit_marker(note=text.strip())

    def _submit_marker(self, *, note: str) -> None:
        engine = self._engine
        if engine is None:
            return
        engine.submit(
            RawCaptureEvent(
                kind=EventKind.MARKER,
                occurred_at=utcnow(),
                text=note or None,
            )
        )

    @Slot()
    def _on_snapshot_hotkey(self) -> None:
        """Capture the current screen and submit it as a marker event.

        Works in either auto- or manual-screenshot mode — handy when the
        examiner wants to mark *this* exact moment with a frozen frame
        even if the surrounding events would normally produce screenshots
        anyway.
        """
        engine = self._engine
        if engine is None:
            return
        # mss must be owned by the calling thread; the queued signal puts
        # this on the Qt main thread, where a fresh capturer is fine.
        capturer = create_screen_capturer()
        try:
            image = capturer.capture()
        except Exception:
            logger.exception("Snapshot hotkey failed to capture screen")
            return
        finally:
            safe_close(capturer)
        # Recording could have stopped between the queued signal arriving
        # and the capture finishing; re-check before submit so we don't
        # silently lose the snapshot to an engine that's no longer
        # accepting events.
        engine = self._engine
        if engine is None:
            logger.info("Snapshot hotkey: recording stopped before submit")
            return
        if not engine.submit(
            RawCaptureEvent(
                kind=EventKind.MARKER,
                occurred_at=utcnow(),
                text="Manual snapshot.",
                png_bytes=image.png_bytes,
                png_width=image.width,
                png_height=image.height,
            )
        ):
            logger.warning("Snapshot hotkey: capture queue refused the event")

    # ---------------------------------------------------------- step actions

    def _regenerate_steps(self) -> None:
        if self._repository is None:
            return
        try:
            generate_steps(self._repository)
        except Exception:
            logger.exception("Step generation failed")
            QMessageBox.warning(
                self._parent_widget,
                "Step generation failed",
                "Inscription could not rebuild draft steps. See logs for details.",
            )
            return
        self._workspace.reload()

    def regenerate_steps(self) -> None:
        """Public entry point for the File > Regenerate menu item."""
        self._regenerate_steps()

    def auto_screenshot_enabled(self) -> bool:
        return self._config.auto_screenshot

    def set_auto_screenshot(self, enabled: bool) -> None:
        """Persist the auto-screenshot preference and apply it immediately.

        Updates any running capture sources so toggling during a recording
        takes effect on the very next event, not just at the next recording.
        """
        self._config.auto_screenshot = enabled
        self._config.sync()
        if self._click_source is not None:
            self._click_source.set_auto_screenshot(enabled)
        if self._window_source is not None:
            self._window_source.set_auto_screenshot(enabled)

    def rewrite_with_llm(self) -> None:
        """Send the session to the configured LLM and replace draft_steps
        with the model's rewritten version. Preserves manual edits.

        Shows a modal progress dialog and runs the request on a worker
        thread so the UI stays responsive. Any failure — connection,
        timeout, malformed response — leaves the existing steps in place
        and shows the error to the user.
        """
        if self._repository is None:
            return
        self._workspace.flush_pending()
        try:
            client = LLMClient(
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout_s=self._config.llm_timeout_s,
                api_key=self._config.llm_api_key,
            )
        except LLMError as exc:
            QMessageBox.warning(self._parent_widget, "LLM not configured", str(exc))
            return

        rewriter = StepRewriter(repository=self._repository, client=client)
        worker = RewriteWorker(rewriter)
        dialog = RewriteProgressDialog(worker, parent=self._parent_widget)
        dialog.succeeded.connect(lambda _steps: self._workspace.reload())
        dialog.failed.connect(self._show_llm_error)
        dialog.start()
        dialog.exec()

    def _show_llm_error(self, raw_message: str) -> None:
        """Surface an LLM failure with a hint pointing at Settings."""
        friendly = _friendly_llm_error(raw_message, base_url=self._config.llm_base_url)
        QMessageBox.warning(self._parent_widget, "LLM rewrite failed", friendly)

    def export_html(self) -> None:
        run_export(
            self._repository,
            parent=self._parent_widget,
            kind="HTML",
            extension="html",
            file_filter="HTML (*.html)",
            renderer=export_html,
        )

    def export_markdown(self) -> None:
        run_export(
            self._repository,
            parent=self._parent_widget,
            kind="Markdown",
            extension="md",
            file_filter="Markdown (*.md)",
            renderer=export_markdown,
        )

    def export_forensic_notes(self) -> None:
        examiner = self._format_examiner()
        case_ref = self._case_dir.name if self._case_dir is not None else None

        def render(repository: SessionRepository, *, destination: Path) -> ExportDocument:
            return export_forensic_notes(
                repository,
                destination=destination,
                examiner=examiner,
                case_reference=case_ref,
            )

        self._run_deliverable_export(
            kind="Forensic notes",
            extension="html",
            file_filter="HTML (*.html)",
            renderer=render,
            suggested_suffix="-notes",
        )

    def export_pdf(self) -> None:
        """Render forensic notes to a self-contained PDF.

        Uses the same case header / examiner block as the HTML
        forensic-notes export, but adds per-page header (case +
        examiner) + footer (page X of Y, generated-at timestamp)
        and inlines all screenshots into a single .pdf file --
        nothing for the operator to zip up before handing over.
        """
        examiner = self._format_examiner()
        case_ref = self._case_dir.name if self._case_dir is not None else None

        def render(repository: SessionRepository, *, destination: Path) -> ExportDocument:
            return export_pdf(
                repository,
                destination=destination,
                examiner=examiner,
                case_reference=case_ref,
            )

        self._run_deliverable_export(
            kind="PDF",
            extension="pdf",
            file_filter="PDF (*.pdf)",
            renderer=render,
            suggested_suffix="-notes",
        )

    def _run_deliverable_export(
        self,
        *,
        kind: str,
        extension: str,
        file_filter: str,
        renderer: Callable[[SessionRepository, Path], ExportDocument],
        suggested_suffix: str = "",
    ) -> None:
        """Drive a deliverable-class export through ``run_export`` and
        offer to lock the session afterwards.

        ``kind`` doubles as the ``export_format`` recorded in the
        submitted marker so the lock prompt and the marker stay
        consistent. Used by export_forensic_notes / export_pdf.
        """

        def _maybe_offer_lock() -> None:
            if not self.is_session_submitted():
                self._offer_mark_submitted(export_format=kind)

        run_export(
            self._repository,
            parent=self._parent_widget,
            kind=kind,
            extension=extension,
            file_filter=file_filter,
            renderer=renderer,  # type: ignore[arg-type]
            suggested_suffix=suggested_suffix,
            on_complete=_maybe_offer_lock,
        )

    def _format_examiner(self) -> str | None:
        """Build the "Name (Org) · ID" string used in export headers.

        Each fragment is included only when present, so a config with
        only a name produces just the name; with name + org but no
        id, "Name (Org)"; etc. Shared by the HTML and PDF forensic
        exports so the rendered header stays consistent across formats.
        """
        examiner = self._config.examiner_name.strip() or None
        if self._config.examiner_org.strip():
            examiner = (
                f"{examiner} ({self._config.examiner_org.strip()})"
                if examiner
                else self._config.examiner_org.strip()
            )
        if self._config.examiner_id.strip():
            examiner = (
                f"{examiner} · {self._config.examiner_id.strip()}"
                if examiner
                else self._config.examiner_id.strip()
            )
        return examiner

    def _offer_mark_submitted(self, *, export_format: str) -> None:
        """Prompt: 'Mark this session as submitted? It will become read-only.'

        Default is Yes -- if the operator just hand-rolled an evidence
        export, locking is the right next move. Cancel keeps the session
        editable and they can mark it later via Edit -> Mark as
        submitted.
        """
        choice = QMessageBox.question(
            self._parent_widget,
            "Mark session as submitted?",
            (
                f"You just exported {export_format} for this session. "
                "Mark the session as submitted so further edits don't "
                "diverge from what's now in the discovery package?\n\n"
                "You can reopen for editing any time from the banner that "
                "appears at the top of the workspace."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if choice == QMessageBox.StandardButton.Yes:
            self.mark_session_submitted(export_format=export_format)

    # ----------------------------------------------------------- slots

    @Slot(object)
    def _on_engine_event(self, event: object) -> None:
        if not isinstance(event, EnrichedEvent):
            return
        if self._repository is None:
            return
        self._event_count += 1
        self._recorder_bar.set_event_count(self._event_count)
        self.event_counted.emit(self._event_count)

    @Slot(int, str, str)
    def _on_step_fields_edited(self, step_id: int, action: str, result: str) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        # Look up the pre-edit state so the undo command can restore the
        # exact prior text + manual_edit flag. Skip pushing a command
        # when nothing actually changed (the editor fires this slot on
        # focus-out regardless of whether the text was edited).
        before = self._repository.get_step(step_id)
        if before is None:
            return
        if before.action == action and before.result == result and before.manual_edit:
            return
        try:
            self._undo_stack.push(
                EditStepFieldsCommand(
                    repository=self._repository,
                    workspace=self._workspace,
                    step_id=step_id,
                    before_action=before.action,
                    before_result=before.result,
                    before_manual_edit=before.manual_edit,
                    after_action=action,
                    after_result=result,
                )
            )
        except Exception:
            logger.exception("Failed to persist step edit (step_id=%d)", step_id)

    @Slot(int, bool)
    def _on_step_suppressed(self, step_id: int, suppressed: bool) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        before = self._repository.get_step(step_id)
        if before is None or before.suppressed == suppressed:
            return
        try:
            self._undo_stack.push(
                SetStepSuppressedCommand(
                    repository=self._repository,
                    workspace=self._workspace,
                    step_id=step_id,
                    before=before.suppressed,
                    after=suppressed,
                )
            )
        except Exception:
            logger.exception("Failed to persist step suppression (step_id=%d)", step_id)

    @Slot(int, bool)
    def _on_step_evidentiary_toggled(self, step_id: int, evidentiary: bool) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        before = self._repository.get_step(step_id)
        if before is None or before.evidentiary == evidentiary:
            return
        try:
            self._undo_stack.push(
                SetStepEvidentiaryCommand(
                    repository=self._repository,
                    workspace=self._workspace,
                    step_id=step_id,
                    before=before.evidentiary,
                    after=evidentiary,
                )
            )
        except Exception:
            logger.exception("Failed to persist evidentiary flag (step_id=%d)", step_id)

    @Slot(list)
    def _on_steps_reordered(self, ordered_ids: list[int]) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            self._workspace.reload()  # snap UI back to truth: drag is rejected
            return
        # Snapshot the previous ordering so undo can restore it. We pull
        # ids from the live step list rather than the workspace so the
        # before-state is taken from the canonical source.
        before_order = [
            s.id
            for s in self._repository.list_steps(include_suppressed=True)
            if s.id is not None
        ]
        if before_order == ordered_ids:
            return
        try:
            self._undo_stack.push(
                ReorderStepsCommand(
                    repository=self._repository,
                    workspace=self._workspace,
                    before_order=before_order,
                    after_order=ordered_ids,
                )
            )
        except Exception:
            logger.exception("Failed to persist reorder")
            self._workspace.reload()  # snap UI back to truth on failure

    @Slot(int, int)
    def _on_merge_requested(self, primary_id: int, other_id: int) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        repo = self._repository

        def mutate() -> None:
            repo.merge_steps(primary_id=primary_id, other_id=other_id)

        try:
            self._undo_stack.push(
                SnapshotAndReplaceCommand(
                    repository=repo,
                    workspace=self._workspace,
                    text=f"Merge steps {primary_id} and {other_id}",
                    mutate=mutate,
                )
            )
        except Exception:
            logger.exception("Failed to merge steps %d and %d", primary_id, other_id)
            QMessageBox.warning(
                self._parent_widget,
                "Merge failed",
                "Could not merge those two steps. See logs for details.",
            )

    @Slot(int)
    def _on_split_requested(self, step_id: int) -> None:
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        repo = self._repository

        def mutate() -> None:
            repo.split_step(step_id)

        try:
            self._undo_stack.push(
                SnapshotAndReplaceCommand(
                    repository=repo,
                    workspace=self._workspace,
                    text=f"Split step {step_id}",
                    mutate=mutate,
                )
            )
        except Exception:
            logger.exception("Failed to split step %d", step_id)
            QMessageBox.warning(
                self._parent_widget,
                "Split failed",
                "Could not split that step. See logs for details.",
            )

    @Slot(object)
    def _on_draft_step_requested(self, suggestion: CaseguideSuggestion) -> None:
        """Append a draft step seeded from a CaseGuide suggestion.

        ``manual_edit=True`` so the next Regenerate-Steps pass leaves
        the row untouched — the examiner deliberately chose this
        suggestion, it shouldn't be clobbered by event-driven
        regeneration.

        Routed through ``SnapshotAndReplaceCommand`` so a Ctrl+Z
        pulls the drafted step back out instead of silently undoing
        the previous unrelated edit.
        """
        if self._repository is None:
            return
        if self._blocked_by_submitted():
            return
        action = suggestion.action.strip()
        if not action:
            logger.info("Draft-as-step ignored: suggestion %s has no action", suggestion.id)
            return
        repo = self._repository

        def mutate() -> None:
            repo.append_step(
                DraftStep(
                    id=None,
                    sequence=0,  # repository auto-assigns from MAX(sequence) + 1
                    action=action,
                    result=suggestion.expected_result,
                    manual_edit=True,
                )
            )

        try:
            self._undo_stack.push(
                SnapshotAndReplaceCommand(
                    repository=repo,
                    workspace=self._workspace,
                    text=f"Draft step from suggestion {suggestion.id}",
                    mutate=mutate,
                )
            )
        except Exception:
            logger.exception("Failed to draft suggestion %s as a step", suggestion.id)
            QMessageBox.warning(
                self._parent_widget,
                "Draft failed",
                "Could not add that suggestion as a step. See logs for details.",
            )


# _friendly_llm_error moved to inscription.ui.controller_errors;
# imported at the top of this file and exposed under its old name
# for any external caller / test that imported it from controller.
