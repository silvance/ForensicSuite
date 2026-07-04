"""UI for the integrity-verification command.

Shows a summary up top — pass / fail / warning — and an expandable
list of every problem row when the check finds anything. The dialog
is read-only; remediation (re-hash, restore from backup, mark
unhashed) is the operator's job and lives outside Inscription.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

if TYPE_CHECKING:
    from inscription.verify import IntegrityResult

logger = logging.getLogger(__name__)


class IntegrityResultDialog(QDialog):
    """Modal report for a single :func:`verify_session_integrity` run."""

    def __init__(self, result: IntegrityResult, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session integrity check")
        self.setMinimumWidth(560)

        headline = QLabel(_summary_line(result), self)
        headline_font = headline.font()
        headline_font.setBold(True)
        headline_font.setPointSize(headline_font.pointSize() + 1)
        headline.setFont(headline_font)
        headline.setStyleSheet(
            "color: #2c7a2c;" if result.is_clean else "color: #c0392b;"
        )

        counts = QLabel(_counts_line(result), self)
        counts.setProperty("muted", True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        layout.addWidget(headline)
        layout.addWidget(counts)

        detail = _detail_text(result)
        if detail:
            box = QPlainTextEdit(detail, self)
            box.setReadOnly(True)
            box.setMinimumHeight(220)
            layout.addWidget(box, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _summary_line(result: IntegrityResult) -> str:
    if result.is_clean and not result.has_warnings:
        return "✓ All screenshots verified."
    if result.is_clean and result.has_warnings:
        return "✓ All hashed screenshots verified (some rows lack hashes — see below)."
    return "✗ Integrity check failed — see details below."


def _counts_line(result: IntegrityResult) -> str:
    return (
        f"{result.total_checked} screenshot(s) checked · "
        f"{result.ok} OK · {len(result.mismatched)} mismatched · "
        f"{len(result.missing)} missing · "
        f"{len(result.unreadable)} unreadable · "
        f"{len(result.unhashed)} unhashed"
    )


def _detail_text(result: IntegrityResult) -> str:
    sections: list[str] = []
    if result.mismatched:
        lines = ["MISMATCHED (file content has changed since capture):"]
        for item in result.mismatched:
            lines.append(f"  {item.relative_path}")
            lines.append(f"    expected: {item.expected_sha256}")
            lines.append(f"    actual:   {item.actual_sha256}")
        sections.append("\n".join(lines))
    if result.missing:
        lines = ["MISSING (file no longer on disk):"]
        lines.extend(f"  {path}" for path in result.missing)
        sections.append("\n".join(lines))
    if result.unreadable:
        lines = [
            "UNREADABLE (on disk but could not be read — permissions, "
            "another program holding the file, or a disk error; "
            "integrity NOT verified):"
        ]
        lines.extend(f"  {path}" for path in result.unreadable)
        sections.append("\n".join(lines))
    if result.unhashed:
        lines = ["UNHASHED (no SHA-256 was recorded — likely a legacy session):"]
        lines.extend(f"  {path}" for path in result.unhashed)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
