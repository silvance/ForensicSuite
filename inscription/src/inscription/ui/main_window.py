"""Main application window.

Hosts the recorder bar on top, the session workspace below, and the menus
that drive the controller.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from suite_common import read_version_info

from inscription import __version__
from inscription.config import Config
from inscription.paths import LOG_DIR, ensure_dirs
from inscription.platform import CAPTURE_FULLY_SUPPORTED, CAPTURE_UNAVAILABLE_REASON
from inscription.ui.app_icon import build_app_icon
from inscription.ui.controller import SessionController
from inscription.ui.mini_dock import MiniDock
from inscription.ui.recorder_bar import RecorderBar
from inscription.ui.tray import SystemTrayController, quit_application
from inscription.ui.welcome import WelcomePage
from inscription.ui.workspace import SessionWorkspaceWidget

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level application window."""

    def __init__(
        self,
        *,
        auto_start_controller: bool = True,
        case_dir: Path | None = None,
    ) -> None:
        super().__init__()
        self._config = Config()
        self._case_dir = case_dir

        title = f"Inscription {__version__}"
        if case_dir is not None:
            title = f"Inscription {__version__} — Case: {case_dir.name}"
        self.setWindowTitle(title)
        self.resize(1200, 800)

        self._recorder_bar = RecorderBar(self)
        self._workspace = SessionWorkspaceWidget(self)
        self._workspace.set_case_dir(case_dir)
        self._welcome = WelcomePage(self)
        central = self._build_central()
        self.setCentralWidget(central)

        self._controller = SessionController(
            workspace=self._workspace,
            recorder_bar=self._recorder_bar,
            parent_widget=self,
            parent=self,
            case_dir=case_dir,
        )
        self._controller.session_opened.connect(self._on_session_opened)
        self._controller.session_closed.connect(self._on_session_closed)
        self._welcome.open_session_requested.connect(self._controller.start)
        self._welcome.open_existing_requested.connect(self._controller.open_session_by_slug)

        # Compact-overlay + tray bring-up. Both stay hidden until the
        # examiner asks for them (or the close-to-tray path triggers).
        self._force_quit = False
        self._mini_dock = MiniDock()
        self._mini_dock.expand_requested.connect(self._on_dock_expand)
        self._mini_dock.hide_requested.connect(self._mini_dock.hide)
        self._mini_dock.moved.connect(self._on_dock_moved)
        self._restore_dock_position()
        self._controller.latest_step_changed.connect(self._mini_dock.show_step)
        self._controller.recording_state_changed.connect(self._mini_dock.set_recording)
        self._controller.session_opened.connect(self._mini_dock.set_session_name)
        self._controller.session_closed.connect(lambda: self._mini_dock.set_session_name(None))

        self._tray = SystemTrayController(icon=build_app_icon(), parent=self)
        self._tray.show_window_requested.connect(self._on_tray_show_window)
        self._tray.compact_mode_requested.connect(self.enter_compact_mode)
        self._tray.toggle_recording_requested.connect(self._recorder_bar.toggle_record)
        self._tray.quit_requested.connect(self._on_tray_quit)
        self._controller.session_opened.connect(self._tray.set_session)
        self._controller.session_closed.connect(lambda: self._tray.set_session(None))
        if self._tray.is_supported():
            self._tray.show()

        self._build_menus()
        self._build_statusbar()
        self._restore_geometry()
        self._welcome.refresh(self._controller.workspace_root())

        # On non-Windows hosts the UIA resolver isn't available and the
        # foreground inspector is a stub, so a Record session would
        # produce all-generic placeholder steps. Hard-disable the
        # capture controls (Record + Mark) so the operator doesn't end
        # up with a forensic-grade-looking session full of meaningless
        # data. Case management, step rewriting, and exports are
        # untouched.
        if not CAPTURE_FULLY_SUPPORTED:
            self._recorder_bar.set_capture_supported(
                False, reason=CAPTURE_UNAVAILABLE_REASON
            )
            self._tray.set_recording_toggle_enabled(False)

        if auto_start_controller:
            self.show()

    # ------------------------------------------------------------------ UI

    def _build_central(self) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._recorder_bar)

        self._stack = QStackedWidget(container)
        self._stack.addWidget(self._welcome)
        self._stack.addWidget(self._workspace)

        layout.addWidget(self._stack, 1)
        return container

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        self._build_file_menu(menubar)
        self._build_edit_menu(menubar)
        self._build_view_menu(menubar)
        self._build_help_menu(menubar)

    def _build_file_menu(self, menubar: QMenuBar) -> None:
        file_menu = menubar.addMenu("&File")
        self._add_action(
            file_menu, "&Open Session…", self._controller.start,
            shortcut="Ctrl+O", tip="Open or create a session",
        )
        self._add_action(
            file_menu, "&Regenerate Steps", self._controller.regenerate_steps,
            shortcut="Ctrl+R", tip="Rebuild draft steps from raw events",
        )
        # No keyboard shortcut — Ctrl+Shift+R is the global "toggle recording"
        # hotkey registered by SessionController, and this action would
        # collide with it.
        self._add_action(
            file_menu, "Rewrite with &AI…", self._controller.rewrite_with_llm,
            tip="Rewrite draft steps using the configured local LLM",
        )
        file_menu.addSeparator()
        self._add_action(
            file_menu, "Export as &HTML…", self._controller.export_html,
            shortcut="Ctrl+E", tip="Export the current session as HTML",
        )
        self._add_action(
            file_menu, "Export as &Markdown…", self._controller.export_markdown,
            tip="Export the current session as Markdown (paste into tickets, wikis, PRs)",
        )
        self._add_action(
            file_menu, "Export &Forensic notes…", self._controller.export_forensic_notes,
            tip="Export a printable Time/Date · Action · Result notes table",
        )
        self._add_action(
            file_menu, "Export as &PDF…", self._controller.export_pdf,
            tip="Export the forensic notes as a self-contained PDF with per-page headers",
        )
        file_menu.addSeparator()
        self._add_action(
            file_menu, "&Verify integrity…", self._controller.verify_integrity,
            tip="Re-hash every screenshot and compare to the stored SHA-256",
        )
        self._add_action(
            file_menu, "&Transcribe audio evidence…",
            self._controller.transcribe_audio_evidence,
            tip="Transcribe an audio or video file with the local Whisper "
                "engine; the transcript is saved into the session and the "
                "timeline records the act",
        )
        file_menu.addSeparator()
        self._add_action(
            file_menu, "Mark session as &submitted",
            self._on_mark_submitted,
            tip="Lock the session so further edits don't diverge from what's "
                "already in the discovery package. Reopen any time from the banner.",
        )
        self._add_action(
            file_menu, "Reop&en for editing",
            self._on_reopen_submitted,
            tip="Clear the submitted marker and re-enable editing.",
        )
        file_menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

    def _build_edit_menu(self, menubar: QMenuBar) -> None:
        edit_menu = menubar.addMenu("&Edit")

        # Qt's QUndoStack vends QAction instances that auto-update their
        # enabled state and label text ("Undo Edit step 3" / "Redo
        # Reorder steps") as the stack changes. Wiring the Edit menu to
        # those actions rather than rolling our own keeps the menu
        # honest as commands push and pop.
        undo_action = self._controller.undo_stack.createUndoAction(self, "&Undo")
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        undo_action.setStatusTip("Undo the last step edit")
        edit_menu.addAction(undo_action)

        redo_action = self._controller.undo_stack.createRedoAction(self, "&Redo")
        redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        redo_action.setStatusTip("Redo the previously undone edit")
        edit_menu.addAction(redo_action)

        edit_menu.addSeparator()

        self._add_action(
            edit_menu, "&Settings…", self._controller.open_settings,
            shortcut="Ctrl+,", tip="Examiner identity and LLM endpoint",
        )

    def _build_view_menu(self, menubar: QMenuBar) -> None:
        view_menu = menubar.addMenu("&View")
        self._auto_screenshot_action = QAction("Auto-screenshot every action", self)
        self._auto_screenshot_action.setCheckable(True)
        self._auto_screenshot_action.setChecked(self._controller.auto_screenshot_enabled())
        self._auto_screenshot_action.setStatusTip(
            "When off, only the Ctrl+Shift+P snapshot hotkey produces images. "
            "Takes effect on the next recording."
        )
        self._auto_screenshot_action.toggled.connect(self._controller.set_auto_screenshot)
        view_menu.addAction(self._auto_screenshot_action)
        view_menu.addSeparator()
        self._add_action(
            view_menu, "&Compact mode", self.enter_compact_mode,
            shortcut="Ctrl+Shift+D",
            tip="Hide the main window and show a small always-on-top step tracker",
        )
        self._add_action(
            view_menu, "Hide to system tray", self._hide_to_tray,
            tip="Close the main window — Inscription keeps running in the tray",
        )

    def _build_help_menu(self, menubar: QMenuBar) -> None:
        help_menu = menubar.addMenu("&Help")
        self._add_action(
            help_menu, "Show &logs folder", self._open_logs_folder,
            tip="Open the Inscription log directory in the file manager",
        )
        help_menu.addSeparator()
        self._add_action(help_menu, "&About Inscription", self._show_about)
        self._add_action(
            help_menu, "About &Qt", lambda: QMessageBox.aboutQt(self, "About Qt"),
        )

    def _add_action(
        self,
        menu: QMenu,
        title: str,
        slot: object,
        *,
        shortcut: str | None = None,
        tip: str | None = None,
    ) -> QAction:
        action = QAction(title, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        if tip:
            action.setStatusTip(tip)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _build_statusbar(self) -> None:
        if CAPTURE_FULLY_SUPPORTED:
            self.statusBar().showMessage("Ready")
        else:
            # Persistent (timeout=0) so the operator sees the message
            # for the duration of the session, not just for a moment.
            self.statusBar().showMessage(
                "Capture is Windows-only on this build. "
                "Case management, step rewriting, and exports work normally.",
                0,
            )

    # --------------------------------------------------------------- State

    def _restore_geometry(self) -> None:
        geom = self._config.window_geometry
        if geom is not None:
            self.restoreGeometry(geom)
        state = self._config.window_state
        if state is not None:
            self.restoreState(state)

    def _save_geometry(self) -> None:
        self._config.window_geometry = self.saveGeometry()
        self._config.window_state = self.saveState()
        self._config.sync()

    # ------------------------------------------------------------- Actions

    def _show_about(self) -> None:
        # The bundle build stamp lives next to start-suite.ps1; surface
        # it here so an operator can answer "which build did we deploy?"
        # without going to the file system. None when running from a
        # source checkout, in which case we just show the package version.
        bundle_info = read_version_info()
        bundle_html = ""
        if bundle_info is not None:
            sha = str(bundle_info.get("git_sha", ""))[:8] or "unknown"
            built = str(bundle_info.get("build_timestamp", "")) or "unknown"
            models = bundle_info.get("models") or []
            model_str = ", ".join(str(m) for m in models) or "(none)"
            bundle_html = (
                "<hr>"
                "<p><b>Bundle build</b><br>"
                f"git: <code>{sha}</code><br>"
                f"built: <code>{built}</code><br>"
                f"models: <code>{model_str}</code></p>"
            )
        QMessageBox.about(
            self,
            "About Inscription",
            (
                f"<h3>Inscription {__version__}</h3>"
                "<p>Record a workflow, review the draft, export a polished guide.</p>"
                f"{bundle_html}"
            ),
        )

    def _open_logs_folder(self) -> None:
        # ensure_dirs() is idempotent; create LOG_DIR on demand so the
        # first-ever click doesn't open into a missing directory if
        # nothing has logged yet (clean dev install before the logger
        # has touched disk).
        ensure_dirs()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    # ------------------------------------------------------------- Slots

    def _on_session_opened(self, name: str) -> None:
        case_prefix = f"Case: {self._case_dir.name} — " if self._case_dir else ""
        self.setWindowTitle(f"Inscription {__version__} — {case_prefix}{name}")
        self.statusBar().showMessage(f"Session {name!r} open")
        self._stack.setCurrentIndex(1)

    def _on_session_closed(self) -> None:
        title = f"Inscription {__version__}"
        if self._case_dir is not None:
            title = f"{title} — Case: {self._case_dir.name}"
        self.setWindowTitle(title)
        self.statusBar().showMessage("No session open")
        self._welcome.refresh(self._controller.workspace_root())
        self._stack.setCurrentIndex(0)

    # ----------------------------------------------------- Tray / dock

    def enter_compact_mode(self) -> None:
        """Hide the main window and float the dock; expanded by clicking it."""
        self._mini_dock.show()
        self._mini_dock.raise_()
        self.hide()

    def _on_dock_expand(self) -> None:
        self._mini_dock.hide()
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_dock_moved(self, x: int, y: int) -> None:
        self._config.mini_dock_position = (x, y)
        self._config.sync()

    def _restore_dock_position(self) -> None:
        saved = self._config.mini_dock_position
        if saved is None:
            # First-run default: top-right of the primary screen.
            screen = self.screen() or self._mini_dock.screen()
            geometry = screen.availableGeometry() if screen is not None else None
            if geometry is not None:
                self._mini_dock.adjustSize()
                margin = 24
                self._mini_dock.move(
                    geometry.right() - self._mini_dock.width() - margin,
                    geometry.top() + margin,
                )
            return
        self._mini_dock.move(saved[0], saved[1])

    def _on_tray_show_window(self) -> None:
        self._mini_dock.hide()
        self.showNormal()
        self.activateWindow()
        self.raise_()

    def _on_mark_submitted(self) -> None:
        """File menu → Mark session as submitted."""
        if self._controller.is_session_submitted():
            QMessageBox.information(
                self,
                "Already submitted",
                "This session is already marked as submitted. The "
                "banner at the top of the workspace shows when, and "
                "the Reopen button there returns it to editable.",
            )
            return
        # Same prompt the export-flow uses, but with the export_format
        # field set to "manual" because no export just ran.
        self._controller._offer_mark_submitted(export_format="manual")

    def _on_reopen_submitted(self) -> None:
        """File menu → Reopen for editing."""
        if not self._controller.is_session_submitted():
            QMessageBox.information(
                self,
                "Session is editable",
                "This session isn't marked as submitted, so editing "
                "is already enabled.",
            )
            return
        # Same confirm-dialog the banner button uses, so the menu and
        # banner paths produce identical operator-facing UX.
        self._controller._on_reopen_requested()

    def _hide_to_tray(self) -> None:
        """Same path the X button takes when the tray is available."""
        if not self._tray.is_supported():
            return
        self.hide()
        self._maybe_show_first_close_hint()

    def _maybe_show_first_close_hint(self) -> None:
        if self._config.tray_close_hint_shown:
            return
        self._tray.show_message(
            "Inscription is still running",
            "The recorder lives in the system tray. Right-click the tray "
            "icon to bring the window back or quit.",
        )
        self._config.tray_close_hint_shown = True
        self._config.sync()

    def _on_tray_quit(self) -> None:
        self._force_quit = True
        self.close()
        quit_application()

    # -------------------------------------------------------------- Events

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802  (Qt API)
        # X-button on the title bar: hide-to-tray instead of quit, so a
        # mid-recording examiner who reflexively closes the window
        # doesn't lose their session. The tray menu's Quit sets
        # ``_force_quit`` to break out of this path.
        if not self._force_quit and self._tray.is_supported():
            event.ignore()
            self.hide()
            self._maybe_show_first_close_hint()
            return
        self._mini_dock.hide()
        self._tray.hide()
        self._controller.shutdown()
        self._save_geometry()
        logger.info("Main window closing")
        super().closeEvent(event)
