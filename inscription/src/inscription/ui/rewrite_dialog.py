"""Modal "Rewriting with LLM…" progress dialog.

The LLM call is synchronous and can take seconds to minutes on a
local model, so the controller runs :class:`StepRewriter` on a
:class:`QThread` and shows this dialog in the meantime. The dialog is
deliberately thin: a label, an indeterminate progress bar, and a cancel
button. It closes itself when the worker emits ``finished_ok`` or
``failed``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from suite_common.ui import worker_registry

if TYPE_CHECKING:
    from inscription.llm import StepRewriter
    from inscription.model import DraftStep

logger = logging.getLogger(__name__)


class RewriteWorker(QThread):
    """Runs :meth:`StepRewriter.rewrite` on a background thread."""

    #: Emitted with the rewritten steps on success.
    finished_ok = Signal(list)
    #: Emitted with a human-readable message on any failure.
    failed = Signal(str)

    def __init__(self, rewriter: StepRewriter, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rewriter = rewriter
        worker_registry.track(self)

    def run(self) -> None:
        try:
            steps = self._rewriter.rewrite()
        except Exception as exc:
            logger.exception("LLM rewrite failed")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(steps)


class RewriteProgressDialog(QDialog):
    """Indeterminate progress dialog with a cancel button."""

    #: Emitted on success; payload is the ``list[DraftStep]`` from the
    #: rewriter. Consumers listen to this rather than querying the dialog.
    succeeded = Signal(list)
    #: Emitted on failure with a short human-readable message.
    failed = Signal(str)

    def __init__(
        self,
        worker: RewriteWorker,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rewriting with LLM")
        self.setModal(True)
        self.setMinimumWidth(360)
        # Disable the close button — the cancel path is the worker thread's
        # one clean exit, and Qt's X button would just hide the dialog
        # while leaving the request in flight.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, on=False)

        label = QLabel(
            "Asking the model to rewrite the session's steps.\n"
            "This can take a minute on a local model.",
            self,
        )
        label.setWordWrap(True)

        progress = QProgressBar(self)
        progress.setRange(0, 0)  # indeterminate

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        buttons.rejected.connect(self._on_cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(progress)
        layout.addWidget(buttons)

        self._worker = worker
        self._worker.finished_ok.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)

    def start(self) -> None:
        self._worker.start()

    # ------------------------------------------------------------ slots

    def _on_success(self, steps: list[DraftStep]) -> None:
        self.succeeded.emit(steps)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self.failed.emit(message)
        self.reject()

    def _on_cancel(self) -> None:
        # QThread can't be interrupted cleanly mid-HTTP-call, but the
        # request will finish on its own and the connected slots will
        # no-op after reject() closes the dialog. Hide immediately so
        # the user gets a responsive UI back.
        logger.info("User cancelled LLM rewrite (request may still complete in background)")
        self.reject()
