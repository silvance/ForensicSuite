"""Export-pipeline helpers for :class:`SessionController`.

The controller's ``export_html`` / ``export_markdown`` / ``export_forensic_notes``
methods all share the same shape: ask the user for a target path,
invoke a renderer, surface the result. The actual file-dialog +
render + result-dialog plumbing lives here so ``controller.py``
doesn't grow ~80 lines of per-format boilerplate. The controller
keeps just the per-format renderer + destination-naming choice.

Free functions (vs. a class) keep the module trivially testable
without a controller instance. The argument list is small — the
controller hands over the open repository, the parent widget for
the dialogs, and the per-format renderer callable. Examiner-string
formatting for forensic notes stays in the controller because it
reads ``Config``; the renderer the caller hands in here closes over
those formatted values.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PySide6.QtWidgets import QFileDialog, QMessageBox
from suite_common.ui.export_success import show_export_complete

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QWidget

    from inscription.model import ExportDocument
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


class _Renderer(Protocol):
    """A renderer takes the open repo + a destination path and writes."""

    def __call__(
        self, repository: SessionRepository, *, destination: Path
    ) -> ExportDocument: ...


def run_export(
    repository: SessionRepository | None,
    *,
    parent: QWidget | None,
    kind: str,
    extension: str,
    file_filter: str,
    renderer: _Renderer,
    suggested_suffix: str = "",
    on_complete: Callable[[], None] | None = None,
) -> None:
    """File-dialog → render → result-dialog loop.

    ``kind`` is the operator-facing label ("HTML", "Markdown",
    "Forensic notes") and is woven into every dialog title and error
    message so the user knows which export they're configuring.
    ``extension`` and ``file_filter`` configure the save-as dialog;
    ``suggested_suffix`` lets formats that share an extension
    distinguish themselves in the default filename (forensic notes
    use ``-notes`` so they don't collide with a plain HTML export of
    the same session).

    ``on_complete`` runs after the success dialog dismisses. The
    forensic-notes path uses it to offer "mark this session as
    submitted?" -- keeps the deliverable-class lock-down flow at the
    controller layer (where it can read state) without dragging the
    submitted-marker concept into this generic helper.

    Silently returns when the controller has no session open --
    matches the menu-disabled guard the controller already has, but
    keeps the helper safe to call from any code path.
    """
    if repository is None:
        return
    suggested = str(
        repository.session.exports_dir
        / f"{repository.session.root.name}{suggested_suffix}.{extension}"
    )
    target, _ = QFileDialog.getSaveFileName(
        parent,
        f"Export as {kind}",
        suggested,
        file_filter,
    )
    if not target:
        return
    target_path = Path(target)
    try:
        doc = renderer(repository, destination=target_path)
    except Exception:
        logger.exception("%s export failed", kind)
        # Best-effort: drop a partial file the renderer may have
        # written before failing. Otherwise the operator finds an
        # orphan in exports_dir/ that looks like a successful export.
        if target_path.exists():
            try:
                target_path.unlink()
            except OSError:
                logger.warning("Could not remove partial export file at %s", target_path)
        QMessageBox.critical(
            parent,
            "Export failed",
            f"Inscription could not export the guide as {kind}. See logs for details.",
        )
        return
    show_export_complete(
        parent,
        label=f"Exported as {kind}.",
        path=doc.path,
    )
    if on_complete is not None:
        on_complete()
