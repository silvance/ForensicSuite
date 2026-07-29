"""Main application window for CaseForge.

Two-state UI: a welcome card (no case open) and a case view (case
open). Stack-swapped via the controller's signals.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QCloseEvent, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QStackedWidget,
)
from suite_common import read_version_info

from caseforge import __version__
from caseforge.config import Config
from caseforge.model import Case
from caseforge.paths import LOG_DIR, ensure_dirs
from caseforge.ui.case_view import CaseView
from caseforge.ui.controller import CaseController
from caseforge.ui.new_case_dialog import NewCaseDialog
from caseforge.ui.onboarding_dialog import OnboardingDialog
from caseforge.ui.settings_dialog import SettingsDialog
from caseforge.ui.welcome import WelcomePage

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level CaseForge window."""

    def __init__(
        self,
        *,
        auto_show: bool = True,
        case_dir: Path | None = None,
        config: Config | None = None,
    ) -> None:
        super().__init__()
        self._config = config if config is not None else Config()
        self.setWindowTitle(f"CaseForge {__version__}")
        self.resize(1000, 720)

        self._controller = CaseController(parent_widget=self)
        self._controller.case_opened.connect(self._on_case_opened)
        self._controller.case_closed.connect(self._on_case_closed)
        self._controller.cases_changed.connect(self._refresh_welcome)

        self._welcome = WelcomePage(self)
        self._welcome.new_case_requested.connect(self._on_new_case)
        self._welcome.open_case_requested.connect(self._on_open_recent)
        self._welcome.open_anywhere_requested.connect(self._on_open_anywhere)
        self._welcome.archive_case_requested.connect(self._on_archive_case)
        self._welcome.delete_case_requested.connect(self._on_delete_case)

        self._case_view = CaseView(self)
        self._case_view.save_requested.connect(self._controller.save)
        self._case_view.launch_inscription_requested.connect(self._on_launch_inscription)
        self._case_view.launch_caseguide_requested.connect(self._controller.launch_caseguide)
        self._case_view.close_requested.connect(self._controller.close_current)
        self._case_view.refresh_sessions_requested.connect(self._refresh_sessions)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._welcome)
        self._stack.addWidget(self._case_view)
        self.setCentralWidget(self._stack)

        self._build_menus()
        self.statusBar().showMessage("Ready")
        self._restore_geometry()
        self._refresh_welcome()

        if auto_show:
            self._maybe_show_onboarding()
            self.show()

        # --case-dir support: when a case directory is passed (typically
        # from start-suite.ps1's app picker, or manually via the CLI),
        # open it straight away rather than landing on the welcome list.
        # The controller routes invalid paths through the existing
        # error-handling that already surfaces a friendly message.
        if case_dir is not None:
            self._controller.open_existing(case_dir)

    # ------------------------------------------------------------ menus

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        self._build_file_menu(menubar)
        self._build_edit_menu(menubar)
        self._build_help_menu(menubar)

    def _build_file_menu(self, menubar: QMenuBar) -> None:
        menu = menubar.addMenu("&File")
        self._add_action(
            menu, "&New case…", self._on_new_case,
            shortcut="Ctrl+N", tip="Create a new case directory",
        )
        self._add_action(
            menu, "&Open case folder…", self._on_open_anywhere,
            shortcut="Ctrl+O", tip="Open an existing case from anywhere on disk",
        )
        menu.addSeparator()
        self._add_action(
            menu, "&Launch Inscription", self._controller.launch_inscription,
            tip="Spawn Inscription pointed at the open case",
        )
        self._add_action(
            menu, "Launch &Whispr (transcription)", self._controller.launch_whispr,
            tip="Open the Whispr audio-transcription tool (local Whisper "
                "engine with speaker diarization) for evidence audio",
        )
        menu.addSeparator()
        self._add_action(menu, "Close case", self._controller.close_current)
        menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        menu.addAction(exit_action)

    def _build_edit_menu(self, menubar: QMenuBar) -> None:
        menu = menubar.addMenu("&Edit")
        self._add_action(
            menu, "&Settings…", self._open_settings,
            shortcut="Ctrl+,", tip="Examiner identity, workspace, Inscription path",
        )

    def _build_help_menu(self, menubar: QMenuBar) -> None:
        menu = menubar.addMenu("&Help")
        self._add_action(
            menu, "Show &logs folder", self._open_logs_folder,
            tip="Open the CaseForge log directory in the file manager",
        )
        menu.addSeparator()
        self._add_action(menu, "&About CaseForge", self._show_about)
        self._add_action(menu, "About &Qt", lambda: QMessageBox.aboutQt(self, "About Qt"))

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

    # -------------------------------------------------------- geometry

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

    # ----------------------------------------------------- intents

    def _on_new_case(self) -> None:
        dialog = NewCaseDialog(
            examiner_defaults=self._controller.default_examiner(),
            scope_defaults=self._controller.default_scope(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        draft = dialog.draft()
        if draft is None:
            return
        self._controller.create(draft=draft)

    def _on_open_anywhere(self) -> None:
        self._controller.open_from_picker()

    def _on_open_recent(self, path: str) -> None:
        self._controller.open_existing(Path(path))

    def _on_archive_case(self, path: str) -> None:
        # Use a custom-button QMessageBox so the action button reads
        # "Archive case" instead of the generic "Yes" -- destructive
        # confirmations should name the action so the title-bar context
        # isn't the only thing distinguishing them.
        mb = QMessageBox(
            QMessageBox.Icon.Question,
            "Archive case",
            (
                f"Move this case into <code>_archive/</code> inside the workspace?\n\n"
                f"Path: {path}\n\n"
                "Archived cases stop appearing in the browser but stay on "
                "disk so you can recover them by moving them back."
            ),
            parent=self,
        )
        archive_btn = mb.addButton("Archive case", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        mb.setDefaultButton(cancel_btn)
        mb.exec()
        if mb.clickedButton() is archive_btn:
            self._controller.archive(Path(path))

    def _on_delete_case(self, path: str) -> None:
        mb = QMessageBox(
            QMessageBox.Icon.Warning,
            "Delete case permanently",
            (
                f"Permanently delete this case directory and everything in it?\n\n"
                f"Path: {path}\n\n"
                "This cannot be undone. Recordings, screenshots, and exports "
                "in the case folder will all be removed."
            ),
            parent=self,
        )
        # Named explicitly so a hurried Yes-click can't be misread as
        # "yes I want to keep it" -- the verb is right there on the
        # button. Cancel stays default so Enter is the safe choice.
        delete_btn = mb.addButton("Delete permanently", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = mb.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        mb.setDefaultButton(cancel_btn)
        mb.exec()
        if mb.clickedButton() is delete_btn:
            self._controller.delete(Path(path))

    def _open_settings(self) -> None:
        SettingsDialog(self._config, parent=self).exec()
        # Workspace path may have changed; refresh the welcome list.
        self._refresh_welcome()

    def _maybe_show_onboarding(self) -> None:
        """Pop the first-run dialog when the operator hasn't been through it.

        Called from the ``auto_show`` block in ``__init__`` so it never
        fires under tests (which pass ``auto_show=False``). The dialog
        sets ``onboarding_completed`` whether the operator saves or skips,
        so this is a one-shot per profile.
        """
        if self._config.onboarding_completed:
            return
        OnboardingDialog(self._config, parent=self).exec()
        # Workspace path may have changed; refresh the welcome list so the
        # operator sees the right cases (or empty state) on first paint.
        self._refresh_welcome()

    def _show_about(self) -> None:
        # Surface the air-gapped bundle's version.json stamp when we're
        # running from a bundled .exe; None when running from a source
        # checkout, in which case we just show the package version.
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
            "About CaseForge",
            (
                f"<h3>CaseForge {__version__}</h3>"
                "<p>Case intake and scope tool for the Inscription forensic-exam suite.</p>"
                f"{bundle_html}"
            ),
        )

    def _open_logs_folder(self) -> None:
        ensure_dirs()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    # ------------------------------------------------------- slots

    def _on_case_opened(self, case: object) -> None:
        case_dir = self._controller.current_case_dir()
        if case_dir is None or not isinstance(case, Case):
            return
        self._case_view.show_case(case, case_dir=case_dir)
        self._refresh_sessions()
        self.setWindowTitle(f"CaseForge {__version__} — {case.name}")
        self.statusBar().showMessage(f"Case {case.name!r} open")
        self._stack.setCurrentIndex(1)

    def _refresh_sessions(self) -> None:
        self._case_view.show_sessions(self._controller.current_sessions())

    def _on_launch_inscription(self) -> None:
        self._controller.launch_inscription()
        # Inscription is async — sessions will materialise once the
        # examiner records something. Refresh the view so the empty
        # state still reads sensibly while they work, and the user can
        # hit Refresh on the panel after the recording wraps.
        self._refresh_sessions()

    def _on_case_closed(self) -> None:
        self.setWindowTitle(f"CaseForge {__version__}")
        self.statusBar().showMessage("No case open")
        self._refresh_welcome()
        self._stack.setCurrentIndex(0)

    def _refresh_welcome(self) -> None:
        self._welcome.refresh(self._controller.list_summaries())

    # -------------------------------------------------------- events

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        self._save_geometry()
        super().closeEvent(event)
