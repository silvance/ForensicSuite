"""Application controller for CaseGuide.

Owns the open case + the playbook library; bridges the panels to the
storage / generator / LLM modules. Panels emit Qt signals; the
controller translates them into ``case.json`` / ``suggestions.json``
reads and writes. The LLM refinement path runs on a worker thread
via :class:`caseguide.ui.refine_dialog.RefineWorker`.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox
from suite_common import show_error

from caseguide import __version__
from caseguide.case_reader import CaseReadError, read_case
from caseguide.config import Config
from caseguide.generator import generate_suggestions
from caseguide.llm import LLMClient, LLMError, SuggestionsRefiner
from caseguide.markdown_export import render_markdown
from caseguide.model import SuggestionsDocument, utcnow
from caseguide.playbooks import PlaybookMatcher, load_playbooks
from caseguide.storage import StorageError, read_suggestions, write_suggestions

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

    from caseguide.case_reader import CaseHandle

logger = logging.getLogger(__name__)


class CaseGuideController(QObject):
    """Coordinates the open case, the playbook library, and the panels."""

    case_opened = Signal(object, str)  # CaseHandle, case_dir str
    case_closed = Signal()
    suggestions_loaded = Signal(object)  # SuggestionsDocument

    def __init__(self, *, parent_widget: QWidget | None = None) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._config = Config()
        self._case_dir: Path | None = None
        self._case: CaseHandle | None = None
        self._playbooks = load_playbooks()
        self._matcher = PlaybookMatcher(self._playbooks)
        logger.info("Loaded %d playbooks", len(self._playbooks))

    # ------------------------------------------------------- access

    @property
    def config(self) -> Config:
        return self._config

    @property
    def matcher(self) -> PlaybookMatcher:
        return self._matcher

    def current_case_dir(self) -> Path | None:
        return self._case_dir

    def current_case(self) -> CaseHandle | None:
        return self._case

    # ------------------------------------------------------- open / close

    def open_from_picker(self) -> bool:
        directory = QFileDialog.getExistingDirectory(
            self._parent_widget,
            "Open case folder",
        )
        if not directory:
            return False
        return self.open_existing(Path(directory))

    def open_existing(self, case_dir: Path) -> bool:
        try:
            handle = read_case(case_dir)
        except CaseReadError as exc:
            show_error(
                self._parent_widget,
                title="Open failed",
                what="open the case",
                exc=exc,
            )
            return False
        self._case = handle
        self._case_dir = case_dir
        self._config.remember_case(str(case_dir.resolve()))
        self._config.sync()
        self.case_opened.emit(handle, str(case_dir))
        # Auto-load saved suggestions if they exist; otherwise emit None
        # so the panel shows its empty state until Generate runs.
        try:
            doc = read_suggestions(case_dir)
        except StorageError as exc:
            show_error(
                self._parent_widget,
                title="Suggestions read failed",
                what="read the suggestions",
                exc=exc,
            )
            doc = None
        self.suggestions_loaded.emit(doc)
        return True

    def close_current(self) -> None:
        if self._case is None:
            return
        self._case = None
        self._case_dir = None
        self.case_closed.emit()

    # ------------------------------------------------------- generate

    def generate(self) -> SuggestionsDocument | None:
        """Run the deterministic playbook matcher; LLM augmentation arrives later."""
        if self._case is None or self._case_dir is None:
            QMessageBox.information(
                self._parent_widget,
                "No case open",
                "Open a case directory first.",
            )
            return None
        return generate_suggestions(scope=self._case.scope, matcher=self._matcher)

    # ------------------------------------------------------- refine

    def build_refiner(self) -> SuggestionsRefiner | None:
        """Construct a refiner from the current LLM config.

        Returns None and shows a friendly error dialog when the LLM
        config is incomplete; the caller falls back to keeping the
        deterministic suggestions in place.
        """
        try:
            client = LLMClient(
                base_url=self._config.llm_base_url,
                model=self._config.llm_model,
                timeout_s=self._config.llm_timeout_s,
                api_key=self._config.llm_api_key,
            )
        except LLMError as exc:
            QMessageBox.warning(self._parent_widget, "LLM not configured", str(exc))
            return None
        return SuggestionsRefiner(client=client)

    # ------------------------------------------------------- save

    def save(self, doc: SuggestionsDocument) -> bool:
        if self._case_dir is None:
            return False
        # Refresh metadata on the in-memory copy so the saved file's
        # generated_at reflects this save, not whichever value the UI
        # was last handed.
        bumped = dataclasses.replace(
            doc,
            generated_at=utcnow(),
            caseguide_version=__version__,
        )
        try:
            write_suggestions(self._case_dir, bumped)
        except StorageError as exc:
            logger.exception("Save failed")
            show_error(
                self._parent_widget,
                title="Save failed",
                what="save the suggestions",
                exc=exc,
                critical=True,
            )
            return False
        return True

    # ------------------------------------------------------- export

    def copy_markdown_to_clipboard(self, doc: SuggestionsDocument) -> bool:
        """Render the suggestions as markdown and stash on the clipboard.

        Returns False (and shows nothing) when there's no clipboard
        available, which only happens in headless test environments;
        callers can ignore the return value in normal flow.
        """
        text = render_markdown(doc, case=self._case)
        clipboard = QApplication.clipboard()
        if clipboard is None:
            return False
        clipboard.setText(text)
        return True

    def export_markdown(self, doc: SuggestionsDocument) -> Path | None:
        """Show a save-file dialog and write the markdown checklist.

        Default filename derives from the case slug + ``-suggestions.md``
        so the saved file lines up with the case folder it came from.
        """
        suggested_dir = (
            str(self._case_dir.resolve()) if self._case_dir is not None else ""
        )
        case_name = (
            self._case.name
            if self._case is not None and self._case.name
            else "suggestions"
        )
        suggested_name = (
            f"{_filename_safe(case_name)}-suggestions.md" if case_name else "suggestions.md"
        )
        suggested_path = (
            f"{suggested_dir}/{suggested_name}" if suggested_dir else suggested_name
        )
        target, _ = QFileDialog.getSaveFileName(
            self._parent_widget,
            "Export checklist as Markdown",
            suggested_path,
            "Markdown (*.md);;All files (*)",
        )
        if not target:
            return None
        path = Path(target)
        text = render_markdown(doc, case=self._case)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.exception("Markdown export failed")
            show_error(
                self._parent_widget,
                title="Export failed",
                what=f"export the checklist to {path.name}",
                exc=exc,
                critical=True,
            )
            return None
        return path


def _filename_safe(name: str) -> str:
    """Strip / replace characters most filesystems object to.

    Same vocabulary as CaseForge's slugify, lifted inline rather than
    imported so CaseGuide stays decoupled from the sibling package.
    """
    out = []
    for ch in name:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        elif ch == " ":
            out.append("-")
    cleaned = "".join(out).strip("-._")
    return cleaned or "suggestions"
