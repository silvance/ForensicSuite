"""Worker + progress dialog for evidence-audio transcription.

Whisper on CPU is minutes-per-hour-of-audio; the transcription runs
on a :class:`QThread` behind an indeterminate progress dialog,
mirroring the LLM rewrite pattern. Cancel hides the dialog and
discards the result when it eventually arrives -- the subprocess
can't be interrupted mid-model-load cleanly, and an orphaned
transcription burning CPU for a few minutes is preferable to a
half-written deliverable.
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
)
from suite_common.transcribe import TranscriptionError, transcribe_file
from suite_common.ui import worker_registry

if TYPE_CHECKING:
    from pathlib import Path

    from PySide6.QtWidgets import QWidget
    from suite_common.transcribe import Transcription

logger = logging.getLogger(__name__)


class TranscribeWorker(QThread):
    """Runs :func:`transcribe_file` on a background thread."""

    finished_ok = Signal(object)  # Transcription
    failed = Signal(str)

    def __init__(
        self,
        media_path: Path,
        *,
        model: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._media_path = media_path
        self._model = model
        worker_registry.track(self)

    def run(self) -> None:
        try:
            result = transcribe_file(self._media_path, model=self._model)
        except TranscriptionError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # unexpected -- keep the thread from dying silently
            logger.exception("Transcription failed unexpectedly")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit(result)


class TranscribeProgressDialog(QDialog):
    """Indeterminate progress dialog wrapping a :class:`TranscribeWorker`."""

    succeeded = Signal(object)  # Transcription
    failed = Signal(str)

    def __init__(self, worker: TranscribeWorker, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Transcribing audio")
        self.setModal(True)
        self.setMinimumWidth(380)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, on=False)

        label = QLabel(
            "Transcribing with the local Whisper engine.\n"
            "Long recordings can take several minutes on CPU -- roughly "
            "real time with the default model.",
            self,
        )
        label.setWordWrap(True)
        progress = QProgressBar(self)
        progress.setRange(0, 0)
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

    def _on_success(self, result: Transcription) -> None:
        self.succeeded.emit(result)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self.failed.emit(message)
        self.reject()

    def _on_cancel(self) -> None:
        logger.info(
            "User cancelled transcription (whisper may still finish in background; "
            "its result will be discarded)"
        )
        self.reject()
