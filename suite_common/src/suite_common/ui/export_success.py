"""Reusable "export complete" dialog with a Show-in-folder button.

The three apps each have at least one "Export X" code path that needs
to tell the operator where the file landed. The pattern is identical:

  - confirm the export succeeded
  - show the destination path (full, in monospace if possible)
  - offer "Show in folder" so the operator can jump straight to it

This helper wraps that pattern so every export endpoint gets the same
affordances without each app re-implementing the dialog.

Lazy-imports PySide6 so the rest of suite_common stays importable
from headless contexts (CLI tools, non-Qt tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from suite_common.ui.reveal import reveal_in_file_manager

if TYPE_CHECKING:
    from pathlib import Path


def show_export_complete(
    parent: object,
    *,
    title: str = "Export complete",
    label: str,
    path: Path,
) -> None:
    """Show a confirmation dialog with the destination path + a reveal button.

    ``label`` is a short blurb that goes above the path (e.g.
    ``"Exported {N} suggestions to:"``). ``path`` is what we render
    underneath; clicking "Show in folder" opens its parent in the OS
    file manager.

    The dialog is modal -- callers can fire-and-forget; control
    returns when the operator dismisses it.
    """
    from PySide6.QtWidgets import QMessageBox  # noqa: PLC0415 - lazy Qt import

    box = QMessageBox(parent)  # type: ignore[arg-type]
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle(title)
    box.setText(label)
    # Render the path with newlines so a very long path wraps in the
    # dialog instead of stretching it across the screen.
    box.setInformativeText(str(path))
    # Use addButton with the explicit role rather than StandardButton
    # so the reveal button appears as an "action" rather than a
    # default accept/cancel.
    reveal_button = box.addButton("Show in folder", QMessageBox.ButtonRole.ActionRole)
    box.addButton(QMessageBox.StandardButton.Ok)
    box.setDefaultButton(QMessageBox.StandardButton.Ok)
    box.exec()
    if box.clickedButton() is reveal_button:
        reveal_in_file_manager(path)
