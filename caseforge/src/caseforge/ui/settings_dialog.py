"""CaseForge Settings dialog.

Two sections: examiner identity defaults (auto-fill new cases) and
launcher / storage paths. Saves through to :class:`Config.sync`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from caseforge.paths import WORKSPACE_DIR

if TYPE_CHECKING:
    from caseforge.config import Config


class SettingsDialog(QDialog):
    """Edit examiner-identity defaults, workspace root, Inscription path."""

    def __init__(self, config: Config, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = config
        self.setWindowTitle("Settings")
        self.resize(580, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(self._build_examiner_group())
        layout.addWidget(self._build_paths_group())
        layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        save_btn = buttons.button(QDialogButtonBox.StandardButton.Save)
        save_btn.setProperty("role", "primary")
        # Enter saves; explicit setDefault rather than relying on
        # creation-order heuristics inside QDialogButtonBox.
        save_btn.setDefault(True)
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------ examiner

    def _build_examiner_group(self) -> QGroupBox:
        box = QGroupBox("Examiner identity (defaults for new cases)", self)

        self._name_edit = QLineEdit(self._config.examiner_name, box)
        self._name_edit.setPlaceholderText("e.g. Alex Smith")
        self._org_edit = QLineEdit(self._config.examiner_org, box)
        self._org_edit.setPlaceholderText("e.g. Cyber Crimes Unit")
        self._badge_edit = QLineEdit(self._config.examiner_badge, box)
        self._badge_edit.setPlaceholderText("e.g. CCU-0421")

        hint = QLabel(
            "These pre-fill the Examiner tab whenever you create a new case. "
            "Each case keeps its own independent examiner block once saved.",
            box,
        )
        hint.setProperty("muted", "true")
        hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Name", self._name_edit)
        form.addRow("Organisation", self._org_edit)
        form.addRow("Badge / ID", self._badge_edit)

        outer = QVBoxLayout(box)
        outer.addLayout(form)
        outer.addWidget(hint)
        return box

    # --------------------------------------------------------- paths

    def _build_paths_group(self) -> QGroupBox:
        box = QGroupBox("Storage and launcher", self)

        self._workspace_edit = QLineEdit(str(self._config.workspace_root or WORKSPACE_DIR), box)
        workspace_btn = QPushButton("Browse…", box)
        workspace_btn.clicked.connect(self._pick_workspace)
        ws_row = QHBoxLayout()
        ws_row.addWidget(self._workspace_edit, 1)
        ws_row.addWidget(workspace_btn)

        self._inscription_edit = QLineEdit(self._config.inscription_path, box)
        self._inscription_edit.setPlaceholderText(
            "Leave blank to fall back to PATH or 'python -m inscription'"
        )
        inscription_btn = QPushButton("Browse…", box)
        inscription_btn.clicked.connect(self._pick_inscription)
        ins_row = QHBoxLayout()
        ins_row.addWidget(self._inscription_edit, 1)
        ins_row.addWidget(inscription_btn)

        self._caseguide_edit = QLineEdit(self._config.caseguide_path, box)
        self._caseguide_edit.setPlaceholderText(
            "Leave blank to fall back to PATH or 'python -m caseguide'"
        )
        caseguide_btn = QPushButton("Browse…", box)
        caseguide_btn.clicked.connect(self._pick_caseguide)
        cg_row = QHBoxLayout()
        cg_row.addWidget(self._caseguide_edit, 1)
        cg_row.addWidget(caseguide_btn)

        self._whispr_edit = QLineEdit(self._config.whispr_path, box)
        self._whispr_edit.setPlaceholderText(
            "Leave blank to fall back to PATH or 'python -m whispr'"
        )
        whispr_btn = QPushButton("Browse…", box)
        whispr_btn.clicked.connect(self._pick_whispr)
        wh_row = QHBoxLayout()
        wh_row.addWidget(self._whispr_edit, 1)
        wh_row.addWidget(whispr_btn)

        form = QFormLayout()
        form.addRow("Case workspace", ws_row)
        form.addRow("Inscription executable", ins_row)
        form.addRow("CaseGuide executable", cg_row)
        form.addRow("Whispr executable", wh_row)

        outer = QVBoxLayout(box)
        outer.addLayout(form)
        return box

    # -------------------------------------------------------- internals

    def _pick_workspace(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Select case workspace", self._workspace_edit.text()
        )
        if directory:
            self._workspace_edit.setText(directory)

    def _pick_inscription(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Inscription executable",
            self._inscription_edit.text() or "",
            "Executables (*.exe);;All files (*)",
        )
        if path:
            self._inscription_edit.setText(path)

    def _pick_caseguide(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select CaseGuide executable",
            self._caseguide_edit.text() or "",
            "Executables (*.exe);;All files (*)",
        )
        if path:
            self._caseguide_edit.setText(path)

    def _pick_whispr(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Whispr executable",
            self._whispr_edit.text() or "",
            "Executables (*.exe);;All files (*)",
        )
        if path:
            self._whispr_edit.setText(path)

    def _on_save(self) -> None:
        self._config.examiner_name = self._name_edit.text().strip()
        self._config.examiner_org = self._org_edit.text().strip()
        self._config.examiner_badge = self._badge_edit.text().strip()
        ws = self._workspace_edit.text().strip()
        if ws:
            self._config.workspace_root = Path(ws)
        self._config.inscription_path = self._inscription_edit.text().strip()
        self._config.caseguide_path = self._caseguide_edit.text().strip()
        self._config.whispr_path = self._whispr_edit.text().strip()
        self._config.sync()
        self.accept()
