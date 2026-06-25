"""Main application window.

Two-pane layout: scope on the left, suggestions on the right. A header
strip at the top carries the active-case badge and the primary action
buttons (Open, Generate, Save). The window remains usable when no case
is open — the panels show their empty states and Open is the only
enabled action.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)
from suite_common import read_version_info
from suite_common.ui.export_success import show_export_complete

from caseguide import __version__
from caseguide.config import Config
from caseguide.model import Suggestion, SuggestionsDocument, utcnow
from caseguide.paths import LOG_DIR, ensure_dirs
from caseguide.ui.controller import CaseGuideController
from caseguide.ui.refine_dialog import RefineProgressDialog, RefineWorker
from caseguide.ui.scope_panel import ScopePanel
from caseguide.ui.settings_dialog import SettingsDialog
from caseguide.ui.suggestions_panel import SuggestionsPanel
from caseguide.ui.widgets import empty_state

if TYPE_CHECKING:
    from PySide6.QtGui import QCloseEvent

    from caseguide.case_reader import CaseHandle

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Top-level CaseGuide window."""

    def __init__(
        self,
        *,
        case_dir: Path | None = None,
        auto_show: bool = True,
    ) -> None:
        super().__init__()
        self._config = Config()
        self.setWindowTitle(f"CaseGuide {__version__}")
        self.resize(1200, 760)
        self._unsaved = False

        self._controller = CaseGuideController(parent_widget=self)
        self._controller.case_opened.connect(self._on_case_opened)
        self._controller.case_closed.connect(self._on_case_closed)
        self._controller.suggestions_loaded.connect(self._on_suggestions_loaded)

        self._scope = ScopePanel(self)
        self._suggestions = SuggestionsPanel(self)
        self._suggestions.changed.connect(self._on_suggestions_changed)

        self._header_label = self._build_header_label()
        (
            self._open_btn,
            self._generate_btn,
            self._refine_btn,
            self._save_btn,
        ) = self._build_action_buttons()

        self.setCentralWidget(self._build_central())
        self._build_menus()
        self.statusBar().showMessage("Ready")
        self._restore_geometry()

        if auto_show:
            self.show()
        if case_dir is not None:
            self._controller.open_existing(case_dir)

    def _build_header_label(self) -> QLabel:
        label = QLabel("No case open", self)
        font = label.font()
        font.setBold(True)
        label.setFont(font)
        return label

    def _build_action_buttons(
        self,
    ) -> tuple[QPushButton, QPushButton, QPushButton, QPushButton]:
        open_btn = QPushButton("Open case…", self)
        open_btn.clicked.connect(self._controller.open_from_picker)
        generate_btn = QPushButton("Generate", self)
        generate_btn.setToolTip(
            "Run the deterministic playbook matcher to draft suggestions."
        )
        generate_btn.clicked.connect(self._on_generate)
        generate_btn.setEnabled(False)
        refine_btn = QPushButton("Refine with AI", self)
        refine_btn.setProperty("role", "primary")
        refine_btn.setToolTip(
            "Send the current draft to the local LLM to tailor it to this case's scope."
        )
        refine_btn.clicked.connect(self._on_refine)
        refine_btn.setEnabled(False)
        save_btn = QPushButton("Save", self)
        save_btn.clicked.connect(self._on_save)
        save_btn.setEnabled(False)
        return open_btn, generate_btn, refine_btn, save_btn

    def _build_central(self) -> QWidget:
        # Header strip (page-header role for the QSS surface treatment).
        header = QFrame(self)
        header.setProperty("role", "page-header")
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(20, 12, 20, 12)
        header_row.setSpacing(10)
        header_row.addWidget(self._header_label, 1)
        header_row.addWidget(self._open_btn)
        header_row.addWidget(self._generate_btn)
        header_row.addWidget(self._refine_btn)
        header_row.addWidget(self._save_btn)

        # The splitter (active when a case is open).
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.addWidget(self._scope)
        splitter.addWidget(self._suggestions)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)

        # Empty state (shown when no case is open).
        self._empty_state, empty_cta = empty_state(
            title="Open a case to begin",
            body=(
                "CaseGuide reads the scope from CaseForge's case.json, "
                "matches procedural playbooks, and drafts a tailored "
                "exam checklist you can refine with the local LLM."
            ),
            cta_label="Open case folder…",
        )
        if empty_cta is not None:
            empty_cta.clicked.connect(self._controller.open_from_picker)
        empty_outer = QWidget(self)
        empty_layout = QVBoxLayout(empty_outer)
        empty_layout.setContentsMargins(0, 36, 0, 0)
        empty_layout.addWidget(self._empty_state)
        empty_layout.addStretch(1)

        self._stack = QStackedWidget(self)
        self._stack.addWidget(empty_outer)
        self._stack.addWidget(splitter)
        self._stack.setCurrentIndex(0)

        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(self._stack, 1)
        return central

    # ------------------------------------------------------------ menus

    def _build_menus(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("&File")

        open_action = QAction("&Open case…", self)
        open_action.setShortcut(QKeySequence("Ctrl+O"))
        open_action.triggered.connect(self._controller.open_from_picker)
        file_menu.addAction(open_action)

        save_action = QAction("&Save", self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.triggered.connect(self._on_save)
        file_menu.addAction(save_action)

        file_menu.addSeparator()
        regenerate_action = QAction("&Generate suggestions", self)
        regenerate_action.setShortcut(QKeySequence("Ctrl+G"))
        regenerate_action.triggered.connect(self._on_generate)
        file_menu.addAction(regenerate_action)

        file_menu.addSeparator()
        self._copy_md_action = QAction("&Copy checklist to clipboard", self)
        self._copy_md_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        self._copy_md_action.setStatusTip(
            "Copy the suggestions list as a markdown checklist (paste into "
            "the case file, a ticket, or the forensic notes)."
        )
        self._copy_md_action.triggered.connect(self._on_copy_markdown)
        self._copy_md_action.setEnabled(False)
        file_menu.addAction(self._copy_md_action)

        self._export_md_action = QAction("Export checklist as &Markdown…", self)
        self._export_md_action.setStatusTip(
            "Save the suggestions list as a markdown file alongside the case."
        )
        self._export_md_action.triggered.connect(self._on_export_markdown)
        self._export_md_action.setEnabled(False)
        file_menu.addAction(self._export_md_action)

        file_menu.addSeparator()
        exit_action = QAction("E&xit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        edit_menu = menubar.addMenu("&Edit")
        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut(QKeySequence("Ctrl+,"))
        settings_action.setStatusTip("Configure the local LLM endpoint used by Refine.")
        settings_action.triggered.connect(self._open_settings)
        edit_menu.addAction(settings_action)

        help_menu = menubar.addMenu("&Help")
        logs_action = QAction("Show &logs folder", self)
        logs_action.setStatusTip(
            "Open the CaseGuide log directory in the file manager"
        )
        logs_action.triggered.connect(self._open_logs_folder)
        help_menu.addAction(logs_action)
        help_menu.addSeparator()
        about_action = QAction("&About CaseGuide", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    # ------------------------------------------------------------ slots

    def _on_case_opened(self, handle: CaseHandle, case_dir: str) -> None:
        self._scope.show_case(handle, case_dir=case_dir)
        self.setWindowTitle(f"CaseGuide {__version__} — {handle.name}")
        self._header_label.setText(self._header_for(handle))
        self.statusBar().showMessage(f"Opened {case_dir}")
        self._generate_btn.setEnabled(True)
        self._stack.setCurrentIndex(1)

    def _on_case_closed(self) -> None:
        self._scope.clear()
        self._suggestions.set_suggestions([])
        self._header_label.setText("No case open")
        self.setWindowTitle(f"CaseGuide {__version__}")
        self._generate_btn.setEnabled(False)
        self._refine_btn.setEnabled(False)
        self._save_btn.setEnabled(False)
        self._set_export_actions_enabled(have_suggestions=False)
        self._unsaved = False
        self._stack.setCurrentIndex(0)

    def _on_suggestions_loaded(self, doc: SuggestionsDocument | None) -> None:
        if doc is None:
            self._suggestions.set_suggestions([])
            self.statusBar().showMessage(
                "No saved suggestions — click Generate to draft a list."
            )
            self._save_btn.setEnabled(False)
            self._refine_btn.setEnabled(False)
            self._set_export_actions_enabled(have_suggestions=False)
            self._unsaved = False
            return
        self._suggestions.set_suggestions(doc.suggestions)
        when = doc.generated_at.astimezone().strftime("%Y-%m-%d %H:%M")
        self.statusBar().showMessage(
            f"Loaded {len(doc.suggestions)} suggestion(s) · last generated {when}"
        )
        # Loading from disk doesn't dirty the buffer; but enable Save so
        # the user can re-save if they want to bump the timestamp.
        self._save_btn.setEnabled(True)
        self._refine_btn.setEnabled(bool(doc.suggestions))
        self._set_export_actions_enabled(have_suggestions=bool(doc.suggestions))
        self._unsaved = False

    def _on_suggestions_changed(self) -> None:
        self._unsaved = True
        self._save_btn.setEnabled(self._controller.current_case_dir() is not None)
        self._set_export_actions_enabled(
            have_suggestions=bool(self._suggestions.suggestions())
        )

    def _on_generate(self) -> None:
        doc = self._controller.generate()
        if doc is None:
            return
        self._suggestions.set_suggestions(doc.suggestions)
        ids = ", ".join(doc.playbooks) if doc.playbooks else "—"
        self.statusBar().showMessage(
            f"Generated {len(doc.suggestions)} suggestion(s) from playbooks: {ids}"
        )
        self._unsaved = True
        self._save_btn.setEnabled(True)
        self._refine_btn.setEnabled(bool(doc.suggestions))
        self._set_export_actions_enabled(have_suggestions=bool(doc.suggestions))

    def _set_export_actions_enabled(self, *, have_suggestions: bool) -> None:
        self._copy_md_action.setEnabled(have_suggestions)
        self._export_md_action.setEnabled(have_suggestions)

    def _current_document(self) -> SuggestionsDocument:
        """Bundle the panel's current state into a SuggestionsDocument.

        Used by the export paths so they see exactly what's on screen,
        including unsaved edits and completion-state toggles.
        """
        case = self._controller.current_case()
        scope_summary = self._header_for(case) if case is not None else ""
        return SuggestionsDocument(
            generated_at=utcnow(),
            scope_summary=scope_summary,
            playbooks=[],
            suggestions=self._suggestions.suggestions(),
            caseguide_version=__version__,
        )

    def _on_copy_markdown(self) -> None:
        doc = self._current_document()
        if not doc.suggestions:
            return
        if self._controller.copy_markdown_to_clipboard(doc):
            self.statusBar().showMessage(
                f"Copied {len(doc.suggestions)} suggestion(s) to the clipboard as markdown."
            )

    def _on_export_markdown(self) -> None:
        doc = self._current_document()
        if not doc.suggestions:
            return
        target = self._controller.export_markdown(doc)
        if target is not None:
            # Mirror the per-status-bar message in a confirmation
            # dialog with a "Show in folder" button -- the status bar
            # message fades after a few seconds, and operators
            # frequently need to find the file in Explorer right
            # afterwards. Both still fire so the status bar acts as
            # a no-modal-required reminder once dismissed.
            self.statusBar().showMessage(f"Exported markdown checklist to {target}.")
            show_export_complete(
                self,
                label=f"Exported {len(doc.suggestions)} suggestion(s) as Markdown.",
                path=target,
            )

    def _on_refine(self) -> None:
        case = self._controller.current_case()
        if case is None:
            return
        drafts = self._suggestions.suggestions()
        if not drafts:
            self.statusBar().showMessage(
                "Run Generate first — there's nothing for the LLM to refine."
            )
            return
        refiner = self._controller.build_refiner()
        if refiner is None:
            return
        worker = RefineWorker(
            refiner=refiner, scope=case.scope, drafts=drafts, parent=self
        )
        dialog = RefineProgressDialog(worker, parent=self)
        dialog.succeeded.connect(self._on_refine_succeeded)
        dialog.failed.connect(self._on_refine_failed)
        dialog.start()
        dialog.exec()

    def _on_refine_succeeded(self, refined: list[Suggestion]) -> None:
        self._suggestions.set_suggestions(refined)
        self.statusBar().showMessage(
            f"LLM produced {len(refined)} refined suggestion(s)."
        )
        self._unsaved = True
        self._save_btn.setEnabled(True)
        self._refine_btn.setEnabled(bool(refined))

    def _on_refine_failed(self, message: str) -> None:
        QMessageBox.warning(
            self,
            "LLM refinement failed",
            _friendly_llm_error(message, base_url=self._config.llm_base_url),
        )

    def _on_save(self) -> None:
        case_dir = self._controller.current_case_dir()
        if case_dir is None:
            return
        doc = SuggestionsDocument(
            generated_at=utcnow(),
            scope_summary=self._scope_summary_for_save(),
            suggestions=self._suggestions.suggestions(),
        )
        if self._controller.save(doc):
            self.statusBar().showMessage("Saved suggestions.json.")
            self._unsaved = False

    def _scope_summary_for_save(self) -> str:
        case = self._controller.current_case()
        if case is None:
            return ""
        scope = case.scope
        bits: list[str] = []
        if scope.exam_type:
            bits.append(scope.exam_type)
        if scope.primary_tool:
            bits.append(f"tool: {scope.primary_tool}")
        if scope.device_classes:
            bits.append("devices: " + ", ".join(scope.device_classes))
        return " · ".join(bits) if bits else (scope.summary[:200] if scope.summary else "")

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
            "About CaseGuide",
            (
                f"<h3>CaseGuide {__version__}</h3>"
                "<p>LLM-assisted exam coach for the Inscription "
                "forensic-exam suite.</p>"
                f"{bundle_html}"
            ),
        )

    def _open_logs_folder(self) -> None:
        ensure_dirs()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(LOG_DIR)))

    def _open_settings(self) -> None:
        SettingsDialog(self._config, parent=self).exec()

    @staticmethod
    def _header_for(handle: CaseHandle) -> str:
        ref = f" · {handle.case_reference}" if handle.case_reference else ""
        tool = (
            f" · tool: {handle.scope.primary_tool}"
            if handle.scope.primary_tool
            else ""
        )
        return f"{handle.name}{ref}{tool}"

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

    # -------------------------------------------------------- events

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._unsaved:
            reply = QMessageBox.question(
                self,
                "Unsaved suggestions",
                "Save changes to suggestions.json before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                event.ignore()
                return
            if reply == QMessageBox.StandardButton.Save:
                self._on_save()
        self._save_geometry()
        super().closeEvent(event)


def _friendly_llm_error(raw_message: str, *, base_url: str) -> str:
    """Turn a urllib stacktrace string into something the user can act on.

    Mirrors Inscription's helper of the same name — the local-LLM-not-
    running case is the dominant failure mode in the field, so we
    catch the connection-refused / unreachable patterns and tell the
    user what to do instead of dumping a stacktrace.
    """
    lower = raw_message.lower()
    if "connection refused" in lower or "failed to establish" in lower:
        return (
            f"Couldn't reach the local LLM server at {base_url}.\n\n"
            "Start Ollama (or LM Studio / llama.cpp --server) and try "
            "again. If it's running on a different URL or port, open "
            "Edit → Settings to point CaseGuide at it (the Test "
            "connection button verifies before you save).\n\n"
            f"Original error: {raw_message}"
        )
    if "timed out" in lower:
        return (
            "The LLM took too long to respond.\n\n"
            "Local models can be slow on the first request after start-up. "
            "Wait and retry, or raise the timeout / switch to a smaller "
            "model in Edit → Settings.\n\n"
            f"Original error: {raw_message}"
        )
    if "model not found" in lower or "no such model" in lower or "http 404" in lower:
        return (
            "The configured model isn't available on the LLM server.\n\n"
            "Pull it (e.g. `ollama pull gemma4`) or change the model "
            "name in Edit → Settings.\n\n"
            f"Original error: {raw_message}"
        )
    return raw_message
