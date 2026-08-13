"""Modal "Verifying integrity…" progress dialog.

The SHA-256 verify pass is CPU-bound and can take many seconds on a
forensic-case-sized session (hundreds of multi-MB PNGs). Without a
worker thread, the call from ``File → Verify integrity…`` would
freeze the Qt event loop. This module mirrors the
:mod:`inscription.ui.rewrite_dialog` pattern: a thin
indeterminate-then-determinate progress dialog wrapping a
:class:`QThread` that runs :func:`verify_session_integrity`.
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

from inscription.verify import verify_session_integrity

if TYPE_CHECKING:
    from inscription.storage import SessionRepository
    from inscription.verify import IntegrityResult

logger = logging.getLogger(__name__)


class VerifyWorker(QThread):
    """Runs :func:`verify_session_integrity` on a background thread."""

    #: Emitted with the IntegrityResult on success.
    finished_ok = Signal(object)
    #: Emitted with a human-readable message on any failure.
    failed = Signal(str)
    #: Emitted as ``(done, total)`` between rows so the dialog can
    #: switch from indeterminate to determinate progress.
    progress = Signal(int, int)

    def __init__(
        self,
        repository: SessionRepository,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        worker_registry.track(self)

    def run(self) -> None:
        try:
            # The lambda passes the (done, total) ints through to a
            # Qt signal; emitting from a background thread is safe
            # because Signal connections default to QueuedConnection
            # across thread boundaries.
            result = verify_session_integrity(
                self._repository,
                progress_callback=self.progress.emit,
            )
        except Exception as exc:
            logger.exception("Integrity check failed")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class VerifyProgressDialog(QDialog):
    """Indeterminate-then-determinate progress dialog with cancel."""

    #: Emitted on success; payload is the :class:`IntegrityResult`.
    succeeded = Signal(object)
    #: Emitted on failure with a short human-readable message.
    failed = Signal(str)

    def __init__(
        self,
        worker: VerifyWorker,
        *,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Verifying integrity")
        self.setModal(True)
        self.setMinimumWidth(360)
        # Disable the close button — cancel is the worker's clean exit.
        # Qt's X would just hide the dialog while leaving the hash pass
        # running in the background.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, on=False)

        self._label = QLabel(
            "Re-hashing every screenshot and comparing to the\n"
            "SHA-256 recorded at capture time.",
            self,
        )
        self._label.setWordWrap(True)

        self._progress = QProgressBar(self)
        self._progress.setRange(0, 0)  # indeterminate until first signal

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        buttons.rejected.connect(self._on_cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        layout.addWidget(self._progress)
        layout.addWidget(buttons)

        self._worker = worker
        self._worker.finished_ok.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)
        self._worker.progress.connect(self._on_progress)

    def start(self) -> None:
        self._worker.start()

    # ------------------------------------------------------------ slots

    def _on_progress(self, done: int, total: int) -> None:
        if total <= 0:
            return
        self._progress.setRange(0, total)
        self._progress.setValue(done)
        self._label.setText(
            f"Re-hashing screenshot {done} of {total}\n"
            "and comparing to the SHA-256 recorded at capture time."
        )

    def _on_success(self, result: IntegrityResult) -> None:
        self.succeeded.emit(result)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self.failed.emit(message)
        self.reject()

    def _on_cancel(self) -> None:
        # QThread can't be interrupted cleanly mid-hash, but the worker
        # will finish on its own and the connected slots will no-op
        # after reject() closes the dialog. Hide immediately so the
        # operator gets a responsive UI back.
        logger.info(
            "User cancelled integrity check (hash pass may still complete in background)"
        )
        self.reject()
