"""Modal "Refining suggestions…" progress dialog.

Mirrors Inscription's :class:`inscription.ui.rewrite_dialog`. The LLM
call is synchronous and can take seconds-to-minutes on a local model,
so the controller runs :class:`SuggestionsRefiner` on a
:class:`QThread` and shows this dialog meanwhile. The dialog is
deliberately thin: a label, an indeterminate progress bar, and a
cancel button. It closes itself when the worker emits ``finished_ok``
or ``failed``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)
from suite_common.llm import LLMError
from suite_common.ui import worker_registry

if TYPE_CHECKING:
    from caseguide.case_reader import CaseScope
    from caseguide.llm.augment import SuggestionsRefiner
    from caseguide.model import Suggestion

logger = logging.getLogger(__name__)


class RefineWorker(QThread):
    """Runs :meth:`SuggestionsRefiner.refine` on a background thread."""

    finished_ok = Signal(list)  # list[Suggestion]
    failed = Signal(str)

    def __init__(
        self,
        *,
        refiner: SuggestionsRefiner,
        scope: CaseScope,
        drafts: list[Suggestion],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._refiner = refiner
        self._scope = scope
        self._drafts = drafts
        worker_registry.track(self)

    def run(self) -> None:
        try:
            refined = self._refiner.refine(scope=self._scope, drafts=self._drafts)
        except LLMError as exc:
            logger.warning("LLM refinement failed: %s", exc)
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        except Exception as exc:
            # Anything that isn't an LLMError is a programmer bug
            # (parser regression, model construction issue) — log the
            # full traceback so we have the stack to debug from, and
            # still report something the UI can show.
            logger.exception("Unexpected error during LLM refinement")
            self.failed.emit(f"Unexpected: {type(exc).__name__}: {exc}")
            return
        self.finished_ok.emit(refined)


class RefineProgressDialog(QDialog):
    """Indeterminate progress dialog with a cancel button."""

    succeeded = Signal(list)  # list[Suggestion]
    failed = Signal(str)

    def __init__(self, worker: RefineWorker, *, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Refining with LLM")
        self.setModal(True)
        self.setMinimumWidth(460)
        # The X button would just hide the dialog while the worker
        # keeps running; force the user through Cancel so the path
        # is explicit.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, on=False)

        label = QLabel(
            "Asking the model to tailor the suggestions to this case.\n"
            "This can take a minute on a local model.",
            self,
        )
        label.setWordWrap(True)

        progress = QProgressBar(self)
        progress.setRange(0, 0)  # indeterminate
        progress.setTextVisible(False)
        progress.setMinimumHeight(8)

        # Elapsed-time readout — confirms the dialog is alive while
        # the model thinks. Tabular numerals so the seconds digit
        # doesn't reflow the layout every tick.
        self._elapsed_label = QLabel("Elapsed: 0:00", self)
        self._elapsed_label.setProperty("muted", "true")
        self._elapsed_label.setStyleSheet("font-variant-numeric: tabular-nums;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel, parent=self)
        buttons.rejected.connect(self._on_cancel)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)
        layout.addWidget(label)
        layout.addWidget(progress)
        layout.addWidget(self._elapsed_label)
        layout.addWidget(buttons)

        self._worker = worker
        self._worker.finished_ok.connect(self._on_success)
        self._worker.failed.connect(self._on_failure)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(500)  # 2 Hz; cheap and responsive
        self._tick_timer.timeout.connect(self._update_elapsed)
        self._started_at = 0.0

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._tick_timer.start()
        self._worker.start()

    def _update_elapsed(self) -> None:
        if self._started_at == 0.0:
            return
        elapsed = int(time.monotonic() - self._started_at)
        minutes, seconds = divmod(elapsed, 60)
        self._elapsed_label.setText(f"Elapsed: {minutes}:{seconds:02d}")

    # ------------------------------------------------------------ slots

    def _on_success(self, refined: list[Suggestion]) -> None:
        self._tick_timer.stop()
        self.succeeded.emit(refined)
        self.accept()

    def _on_failure(self, message: str) -> None:
        self._tick_timer.stop()
        self.failed.emit(message)
        self.reject()

    def _on_cancel(self) -> None:
        # QThread can't be killed mid-HTTP, but the request will finish
        # on its own. Disconnect the worker's signals before we reject
        # so a late-arriving success doesn't fire ``_on_success`` on a
        # closed dialog and re-emit ``succeeded`` after the controller
        # has moved on.
        self._tick_timer.stop()
        try:
            self._worker.finished_ok.disconnect(self._on_success)
            self._worker.failed.disconnect(self._on_failure)
        except (RuntimeError, TypeError):
            # Already disconnected (success/failure raced us); fine.
            pass
        logger.info("User cancelled LLM refinement (request may still complete in background)")
        self.reject()
