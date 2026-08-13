"""Non-blocking step regeneration with a late-showing progress dialog.

``generate_steps`` ran synchronously on the GUI thread -- and it runs
automatically on EVERY recording stop, so a forensic-case-sized
session (thousands of events, per-event resolved-element lookups)
froze the whole window for seconds with no feedback.

This module mirrors the rewrite/verify worker pattern with one
refinement: the modal dialog only appears if the regeneration is
still running after ``_SHOW_DELAY_MS``. Small sessions regenerate in
tens of milliseconds; flashing a modal for those on every stop would
be worse than the freeze it replaces.

Repository writes are safe off the GUI thread: the repository guards
every write with a lock and its connection is created with
``check_same_thread=False`` (the LLM rewrite worker already relies on
this).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout
from suite_common.ui import worker_registry

from inscription.steps import generate_steps

if TYPE_CHECKING:
    from collections.abc import Callable

    from PySide6.QtWidgets import QWidget

    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)

#: The dialog only appears if regeneration is still running after this
#: long. Fast regens (the overwhelmingly common case) never flash UI.
_SHOW_DELAY_MS = 300


class RegenerateWorker(QThread):
    """Runs :func:`generate_steps` on a background thread."""

    finished_ok = Signal()
    failed = Signal(str)

    def __init__(
        self, repository: SessionRepository, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        worker_registry.track(self)

    def run(self) -> None:
        try:
            generate_steps(self._repository)
        except Exception as exc:
            logger.exception("Step generation failed")
            self.failed.emit(str(exc))
            return
        self.finished_ok.emit()


class _RegenerateProgressDialog(QDialog):
    """Indeterminate 'Rebuilding draft steps…' modal. No cancel button:
    regeneration completes in bounded time and interrupting a
    replace_steps mid-write risks a half-replaced step table."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Rebuilding draft steps")
        self.setModal(True)
        self.setMinimumWidth(340)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, on=False)
        label = QLabel(
            "Rebuilding the draft step list from the recorded events…", self
        )
        label.setWordWrap(True)
        bar = QProgressBar(self)
        bar.setRange(0, 0)
        layout = QVBoxLayout(self)
        layout.addWidget(label)
        layout.addWidget(bar)


def run_regenerate(
    repository: SessionRepository,
    *,
    parent: QWidget | None,
    on_success: Callable[[], None],
    on_failure: Callable[[str], None],
) -> RegenerateWorker:
    """Regenerate steps on a worker thread; show progress only if slow.

    ``on_success`` / ``on_failure`` fire on the GUI thread (queued
    signal connections). The returned worker is parented to ``parent``
    is not -- the caller must keep a reference until completion; the
    controller stores it on an attribute for exactly that reason.
    """
    worker = RegenerateWorker(repository)
    dialog = _RegenerateProgressDialog(parent)

    def _close_dialog() -> None:
        if dialog.isVisible():
            dialog.accept()

    def _maybe_show() -> None:
        if worker.isRunning():
            dialog.show()

    worker.finished_ok.connect(_close_dialog)
    worker.failed.connect(lambda _msg: _close_dialog())
    worker.finished_ok.connect(on_success)
    worker.failed.connect(on_failure)
    # Late-show: no dialog at all unless the regen outlives the delay.
    QTimer.singleShot(_SHOW_DELAY_MS, _maybe_show)
    worker.start()
    return worker
