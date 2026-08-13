"""Track live worker QThreads so app exit can't leave a zombie process.

Cancelled AI rewrites, transcriptions, verifies, and regenerates
deliberately let their worker thread run to completion in the
background (the subprocess / HTTP call can't be interrupted cleanly
mid-flight). Field failure that motivated this module: an operator
closed the main window while such a worker was still going, the Qt
event loop exited -- and the leftover thread kept a HEADLESS
Inscription.exe alive for up to an hour. Launched from the air-gapped
launcher that process is also elevated, so it is invisible to
path-based process listing and unkillable without UAC, and its loaded
DLLs blocked the suite installer's upgrade swap with "file in use"
errors that pointed at nothing.

Workers register themselves at construction (a ``WeakSet`` -- the
registry must never extend a worker's lifetime). After ``app.exec()``
returns, :func:`shutdown_lingering_workers` grants a short grace
period, then hard-exits the process. ``os._exit`` skips interpreter
finalisation by design: the alternative is the zombie.
"""

from __future__ import annotations

import logging
import os
import weakref
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from PySide6.QtCore import QThread

logger = logging.getLogger(__name__)

_live_workers: weakref.WeakSet = weakref.WeakSet()

#: How long exit waits for stragglers before pulling the plug. Long
#: enough for an in-flight local HTTP call to notice its socket close
#: or a fast subprocess to finish; short enough that closing the app
#: always actually closes the app.
GRACE_PERIOD_MS = 3000


def track(worker: QThread) -> None:
    """Register a worker QThread. Call once from the worker's ``__init__``."""
    _live_workers.add(worker)


def _running_workers() -> list[QThread]:
    return [w for w in _live_workers if w.isRunning()]


def shutdown_lingering_workers(exit_code: int) -> None:
    """Ensure no worker QThread outlives the event loop. May not return.

    Called after ``app.exec()`` has returned, i.e. every window is gone
    and ``exit_code`` is final. If workers are still running after the
    grace period, flushes logging and terminates the process via
    ``os._exit`` -- a headless zombie holding global input hooks and
    every loaded DLL is strictly worse than skipping Python cleanup on
    the way out.
    """
    running = _running_workers()
    if not running:
        return
    logger.warning(
        "%d worker thread(s) still running at exit; waiting up to %dms",
        len(running),
        GRACE_PERIOD_MS,
    )
    per_worker_ms = max(1, GRACE_PERIOD_MS // len(running))
    for worker in running:
        worker.wait(per_worker_ms)
    running = _running_workers()
    if not running:
        return
    logger.warning(
        "Hard-exiting with %d worker thread(s) still alive (%s)",
        len(running),
        ", ".join(type(w).__name__ for w in running),
    )
    logging.shutdown()
    os._exit(exit_code)
