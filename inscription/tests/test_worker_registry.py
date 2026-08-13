"""Zombie-process backstop: workers must not outlive the event loop.

Field failure: closing the main window while a cancelled transcription
was still running left a headless, elevated Inscription.exe alive --
invisible to path-based process listing, holding the global input
hooks, and blocking the installer's upgrade swap with "file in use".
These tests drive :func:`shutdown_lingering_workers` with fake workers
(duck-typed ``isRunning``/``wait``) so the exit logic is covered
without Qt or real threads.
"""

from __future__ import annotations

import os
import weakref

from suite_common.ui import worker_registry


def _forbid_exit(code: int) -> None:
    msg = "must not exit"
    raise AssertionError(msg)


class _FakeWorker:
    """Duck-types the QThread surface the registry touches."""

    def __init__(self, *, running: bool, stops_on_wait: bool = False) -> None:
        self._running = running
        self._stops_on_wait = stops_on_wait
        self.wait_calls: list[int] = []

    def isRunning(self) -> bool:  # noqa: N802 - QThread API casing
        return self._running

    def wait(self, ms: int) -> bool:
        self.wait_calls.append(ms)
        if self._stops_on_wait:
            self._running = False
        return not self._running


def _fresh_registry(monkeypatch) -> None:
    monkeypatch.setattr(worker_registry, "_live_workers", weakref.WeakSet())


def test_no_workers_returns_without_exiting(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    monkeypatch.setattr(os, "_exit", _forbid_exit)
    worker_registry.shutdown_lingering_workers(0)


def test_finished_workers_do_not_trigger_exit(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    done = _FakeWorker(running=False)
    worker_registry.track(done)
    monkeypatch.setattr(os, "_exit", _forbid_exit)
    worker_registry.shutdown_lingering_workers(0)
    assert done.wait_calls == []


def test_worker_finishing_within_grace_avoids_hard_exit(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    slow = _FakeWorker(running=True, stops_on_wait=True)
    worker_registry.track(slow)
    monkeypatch.setattr(os, "_exit", _forbid_exit)
    worker_registry.shutdown_lingering_workers(0)
    assert slow.wait_calls  # the grace period was actually granted


def test_stuck_worker_forces_process_exit_with_the_event_loop_code(monkeypatch) -> None:
    _fresh_registry(monkeypatch)
    stuck = _FakeWorker(running=True, stops_on_wait=False)
    worker_registry.track(stuck)
    exited: list[int] = []
    monkeypatch.setattr(os, "_exit", exited.append)
    worker_registry.shutdown_lingering_workers(7)
    assert exited == [7]


def test_grace_period_is_split_across_workers(monkeypatch) -> None:
    """Total worst-case wait stays ~GRACE_PERIOD_MS regardless of how
    many workers linger -- exit latency must not scale with count."""
    _fresh_registry(monkeypatch)
    workers = [_FakeWorker(running=True, stops_on_wait=True) for _ in range(3)]
    for w in workers:
        worker_registry.track(w)
    monkeypatch.setattr(os, "_exit", _forbid_exit)
    worker_registry.shutdown_lingering_workers(0)
    total_granted = sum(sum(w.wait_calls) for w in workers)
    assert total_granted <= worker_registry.GRACE_PERIOD_MS
