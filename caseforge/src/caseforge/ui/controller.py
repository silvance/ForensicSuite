"""Application controller for CaseForge.

Owns the open :class:`Case`, the per-screen widgets, and the bridge to
disk via :mod:`caseforge.storage`. UI widgets emit Qt signals; the
controller translates them into ``case.json`` mutations or the
Inscription launch.
"""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QMessageBox
from suite_common import show_error

from caseforge import __version__
from caseforge.config import Config
from caseforge.inscription_sessions import InscriptionSession, list_inscription_sessions
from caseforge.launcher import launch_tool
from caseforge.model import Case, CaseSummary, ExaminerIdentity, ExamScope, utcnow
from caseforge.paths import WORKSPACE_DIR
from caseforge.storage import (
    CaseAlreadyExistsError,
    StorageError,
    archive_case,
    case_summary_at,
    create_case,
    delete_case,
    list_cases,
    read_case,
    touch_updated_at,
    write_case,
)

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

logger = logging.getLogger(__name__)


class CaseController(QObject):
    """Coordinates the open case and the views that show it."""

    case_opened = Signal(object)  # Case
    case_closed = Signal()
    cases_changed = Signal()  # browser should re-list

    def __init__(self, *, parent_widget: QWidget | None = None) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._config = Config()
        self._case: Case | None = None
        self._case_dir: Path | None = None

    # ------------------------------------------------------------ access

    @property
    def config(self) -> Config:
        return self._config

    def workspace_root(self) -> Path:
        return self._config.workspace_root or WORKSPACE_DIR

    def list_summaries(self) -> list[CaseSummary]:
        """Browser feed: workspace cases + remembered out-of-workspace recents."""
        in_workspace = list_cases(self.workspace_root())
        seen = {summary.path for summary in in_workspace}
        extras: list[CaseSummary] = []
        for raw_path in self._config.recent_case_paths:
            path = Path(raw_path)
            if str(path.resolve()) in seen:
                continue
            try:
                extras.append(case_summary_at(path))
            except StorageError:
                continue
        return [*in_workspace, *extras]

    def current_case(self) -> Case | None:
        return self._case

    def current_case_dir(self) -> Path | None:
        return self._case_dir

    def current_sessions(self) -> list[InscriptionSession]:
        """List Inscription sessions in the open case directory."""
        if self._case_dir is None:
            return []
        return list_inscription_sessions(self._case_dir)

    # ------------------------------------------------------ create / open

    def create(self, *, draft: Case) -> Path | None:
        """Persist a brand-new case under the workspace root."""
        case = dataclasses.replace(
            draft,
            created_at=utcnow(),
            updated_at=utcnow(),
            caseforge_version=__version__,
        )
        try:
            path = create_case(workspace_root=self.workspace_root(), case=case)
        except CaseAlreadyExistsError as exc:
            show_error(
                self._parent_widget,
                title="Case exists",
                what="create the case",
                exc=exc,
            )
            return None
        except StorageError as exc:
            logger.exception("Failed to create case")
            show_error(
                self._parent_widget,
                title="Create failed",
                what="create the case",
                exc=exc,
                critical=True,
            )
            return None
        self._open(case=case, case_dir=path)
        self.cases_changed.emit()
        return path

    def open_existing(self, case_dir: Path) -> bool:
        try:
            case = read_case(case_dir)
        except StorageError as exc:
            show_error(
                self._parent_widget,
                title="Open failed",
                what="open the case",
                exc=exc,
            )
            return False
        self._open(case=case, case_dir=case_dir)
        self.cases_changed.emit()
        return True

    def open_from_picker(self) -> bool:
        """Browse for a case directory anywhere on disk."""
        directory = QFileDialog.getExistingDirectory(
            self._parent_widget,
            "Open case folder",
            str(self.workspace_root()),
        )
        if not directory:
            return False
        return self.open_existing(Path(directory))

    def close_current(self) -> None:
        if self._case is None:
            return
        self._case = None
        self._case_dir = None
        self.case_closed.emit()

    # ------------------------------------------------------ archive / delete

    def archive(self, case_dir: Path) -> bool:
        """Move a case into the workspace's reserved ``_archive/`` folder."""
        if self._case_dir == case_dir:
            self.close_current()
        try:
            archive_case(case_dir)
        except StorageError as exc:
            logger.exception("Archive failed")
            show_error(
                self._parent_widget,
                title="Archive failed",
                what="archive the case",
                exc=exc,
            )
            return False
        self._forget_case(case_dir)
        self.cases_changed.emit()
        return True

    def delete(self, case_dir: Path) -> bool:
        """Recursively remove a case directory after a UI confirmation."""
        if self._case_dir == case_dir:
            self.close_current()
        try:
            delete_case(case_dir)
        except StorageError as exc:
            logger.exception("Delete failed")
            show_error(
                self._parent_widget,
                title="Delete failed",
                what="delete the case",
                exc=exc,
            )
            return False
        self._forget_case(case_dir)
        self.cases_changed.emit()
        return True

    def _forget_case(self, case_dir: Path) -> None:
        """Drop a path from the recents list (post archive / delete)."""
        target = str(case_dir.resolve())
        remaining = [p for p in self._config.recent_case_paths if p != target]
        if remaining != self._config.recent_case_paths:
            self._config.recent_case_paths = remaining
            self._config.sync()

    # ------------------------------------------------------ updates / save

    def save(self, updated: Case) -> bool:
        """Persist an edited case to disk and refresh state."""
        if self._case_dir is None:
            return False
        bumped = touch_updated_at(updated)
        try:
            write_case(self._case_dir, bumped)
        except StorageError as exc:
            logger.exception("Failed to save case")
            show_error(
                self._parent_widget,
                title="Save failed",
                what="save the case",
                exc=exc,
                critical=True,
            )
            return False
        self._case = bumped
        self.case_opened.emit(bumped)
        self.cases_changed.emit()
        return True

    # ----------------------------------------------------------- launching

    def _launch(
        self,
        *,
        tool_label: str,
        module_name: str,
        executable_path: str,
        pass_case_dir: bool = True,
    ) -> None:
        if self._case_dir is None:
            return
        result = launch_tool(
            tool_label=tool_label,
            module_name=module_name,
            executable_path=executable_path,
            case_dir=self._case_dir,
            pass_case_dir=pass_case_dir,
        )
        if not result.ok:
            QMessageBox.warning(self._parent_widget, "Launch failed", result.message)
            return
        QMessageBox.information(
            self._parent_widget, f"{tool_label} launched", result.message
        )

    def launch_inscription(self) -> None:
        self._launch(
            tool_label="Inscription",
            module_name="inscription",
            executable_path=self._config.inscription_path,
        )

    def launch_caseguide(self) -> None:
        self._launch(
            tool_label="CaseGuide",
            module_name="caseguide",
            executable_path=self._config.caseguide_path,
        )

    def launch_whispr(self) -> None:
        """Spawn the Whispr audio-transcription GUI.

        Whispr takes no case argument (the operator picks media files
        inside its own UI); launching it from here keeps the exam's
        tool-set one click away and lets the air-gapped bundle ship it
        as a sibling app like Inscription / CaseGuide.
        """
        self._launch(
            tool_label="Whispr",
            module_name="whispr",
            executable_path=self._config.whispr_path,
            pass_case_dir=False,
        )

    # ----------------------------------------------------- helpers

    def default_examiner(self) -> ExaminerIdentity:
        """Pull the saved examiner-identity defaults for new-case forms."""
        return ExaminerIdentity(
            name=self._config.examiner_name,
            organisation=self._config.examiner_org,
            badge_id=self._config.examiner_badge,
        )

    def default_scope(self) -> ExamScope:
        return ExamScope()

    # --------------------------------------------------------- internals

    def _open(self, *, case: Case, case_dir: Path) -> None:
        self._case = case
        self._case_dir = case_dir
        self._config.remember_case(str(case_dir.resolve()))
        self._config.sync()
        self.case_opened.emit(case)
