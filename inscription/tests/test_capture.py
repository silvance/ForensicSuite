"""Capture engine: end-to-end with a fake foreground/resolver."""

from __future__ import annotations

import queue
import sys
import threading
import time
import types
from typing import TYPE_CHECKING

from inscription.capture import (
    CaptureEngine,
    EnrichedEvent,
    RawCaptureEvent,
)
from inscription.capture import engine as engine_module
from inscription.model import EventKind, ResolvedElement
from inscription.platform import (
    ForegroundInfo,
    ForegroundInspector,
)
from inscription.resolve import ElementResolver

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[EnrichedEvent] = []
        self._lock = threading.Lock()

    def handle(self, event: EnrichedEvent) -> None:
        with self._lock:
            self.events.append(event)


class _FakeForeground(ForegroundInspector):
    def inspect(self) -> ForegroundInfo:
        return ForegroundInfo(
            window_title="FakeApp",
            process_name="fake.exe",
            process_id=999,
        )


class _FakeResolver(ElementResolver):
    def resolve_at(self, x: int, y: int) -> ResolvedElement:
        return ResolvedElement(
            id=None,
            name=f"control-at-{x}-{y}",
            control_type="Button",
            confidence=0.9,
            method="uia",
        )


def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("predicate never became true")


def _fake_resolver(_inspector: ForegroundInspector) -> ElementResolver:
    return _FakeResolver()


def test_engine_enriches_click_with_screenshot_and_resolver() -> None:
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    sink = _CollectingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(
            RawCaptureEvent(
                kind=EventKind.CLICK,
                x=42,
                y=13,
                button="left",
                png_bytes=b"\x89PNG-fake",
                png_width=8,
                png_height=8,
            )
        )
        _wait_for(lambda: len(sink.events) == 1)
    finally:
        engine.stop()

    assert len(sink.events) == 1
    enriched = sink.events[0]
    assert enriched.raw.kind is EventKind.CLICK
    assert enriched.raw.png_bytes == b"\x89PNG-fake"
    assert enriched.image_sha256
    assert enriched.resolved is not None
    assert enriched.resolved.name == "control-at-42-13"
    assert enriched.foreground.window_title == "FakeApp"


def test_engine_skips_resolver_for_non_click_events() -> None:
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    sink = _CollectingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="enter"))
        _wait_for(lambda: len(sink.events) == 1)
    finally:
        engine.stop()

    enriched = sink.events[0]
    assert enriched.resolved is None
    assert enriched.raw.png_bytes is None
    assert enriched.image_sha256 == ""


def test_engine_stops_cleanly_without_events() -> None:
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    engine.start()
    engine.stop()


def test_engine_drops_events_from_own_process() -> None:
    """Examiners frequently click into Inscription mid-recording. The
    engine should silently drop those clicks so they don't pollute the
    captured workflow."""
    own_pid = 999  # _FakeForeground reports this as the foreground pid
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
        own_pid=own_pid,
    )
    sink = _CollectingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.CLICK, x=1, y=1, button="left"))
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="enter"))
        # Markers are user-intent so they should still get through even
        # when the foreground belongs to us.
        engine.submit(RawCaptureEvent(kind=EventKind.MARKER, text="kept"))
        _wait_for(lambda: len(sink.events) >= 1, timeout=1.0)
    finally:
        engine.stop()

    kinds = [e.raw.kind for e in sink.events]
    assert EventKind.CLICK not in kinds
    assert EventKind.KEY_PRESS not in kinds
    assert EventKind.MARKER in kinds


# ------------------------------------------------------- COM apartment


def test_com_apartment_is_noop_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    """On non-Windows the helper must not try to import comtypes.

    The whole pywinauto / comtypes / UIA chain is Windows-only. On
    Linux and macOS the capture worker still runs (the fallback
    resolver path), so the context manager must yield cleanly without
    touching any Windows-only imports.
    """
    monkeypatch.setattr(engine_module.sys, "platform", "linux")
    with engine_module._com_apartment():
        pass  # no exception is the test


def test_com_apartment_initialises_and_uninitialises_on_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows the helper must pair CoInitializeEx with CoUninitialize.

    UIA on a worker thread without an apartment is undefined behaviour.
    We verify the pairing by injecting a fake ``comtypes`` module and
    checking the call sequence. We don't actually go through Windows
    COM here -- that's covered by the manual smoke test path.
    """
    calls: list[str] = []

    fake = types.SimpleNamespace(
        COINIT_APARTMENTTHREADED=0x2,
        CoInitializeEx=lambda flags: calls.append(f"init:{flags:#x}"),
        CoUninitialize=lambda: calls.append("uninit"),
    )
    monkeypatch.setattr(engine_module.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "comtypes", fake)
    with engine_module._com_apartment():
        assert calls == ["init:0x2"]
    assert calls == ["init:0x2", "uninit"]


def test_com_apartment_skips_uninit_when_already_initialised_other_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RPC_E_CHANGED_MODE means COM was already initialised elsewhere.

    Per the CoInitializeEx contract we must not call CoUninitialize in
    that case -- the original initialiser owns the apartment lifetime.
    """
    calls: list[str] = []

    def _changed_mode(_flags: int) -> None:
        exc = OSError("already initialised differently")
        exc.winerror = -2147417850  # RPC_E_CHANGED_MODE
        raise exc

    fake = types.SimpleNamespace(
        COINIT_APARTMENTTHREADED=0x2,
        CoInitializeEx=_changed_mode,
        CoUninitialize=lambda: calls.append("uninit"),
    )
    monkeypatch.setattr(engine_module.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "comtypes", fake)
    with engine_module._com_apartment():
        pass
    assert calls == []  # no uninit


def test_engine_run_uses_com_apartment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture worker's _run() must wrap its lifetime in _com_apartment.

    Regression for the missing-CoInitialize bug: the resolver factory
    runs inside the worker thread and may construct COM objects
    eagerly, so the apartment has to be live before the factories
    fire.
    """
    entered = threading.Event()
    exited = threading.Event()

    class _Tracker:
        def __enter__(self) -> None:
            entered.set()

        def __exit__(self, *_exc: object) -> None:
            exited.set()

    monkeypatch.setattr(engine_module, "_com_apartment", _Tracker)
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    engine.start()
    assert entered.wait(timeout=1.0)
    engine.stop()
    assert exited.wait(timeout=1.0)


# ---------------------------------------------------------- stop races


def test_stop_drops_late_source_events_via_stopping_flag() -> None:
    """submit()'s _stopping guard must reject events from any source
    that's still mid-iteration when stop() returns from stop_sources().

    Regression: stop() used to set _stopping AFTER stop_sources(),
    so a source whose .stop() returned could still race a final event
    into the queue because _stopping was still False during the
    teardown window. Flipping the flag first closes that window.
    """
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    engine.start()
    try:
        # Simulate stop() having been called: just set the flag and
        # confirm submit() refuses. The whole-shutdown ordering is
        # exercised in test_engine_stops_cleanly_without_events; here
        # we just lock the contract that submit() honours _stopping.
        engine._stopping.set()
        accepted = engine.submit(
            RawCaptureEvent(kind=EventKind.CLICK, x=1, y=1, button="left")
        )
        assert accepted is False
    finally:
        engine._stopping.clear()  # let stop() run its normal path
        engine.stop()


def test_stop_does_not_block_when_queue_is_full() -> None:
    """stop() must not deadlock if the queue is full and the worker
    is hung on a sink.

    Regression: the old stop() did a blocking ``_queue.put(_STOP_SENTINEL)``
    which would deadlock if the worker couldn't drain (e.g. a sink
    that raised + hung). Now the sentinel goes through put_nowait and
    the worker's get() has a timeout so it also checks _stopping.
    """
    # Tiny queue so we can fill it in one shot.
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
        queue_maxsize=2,
    )
    # Don't start a worker -- we want to simulate the "worker hung"
    # case where nothing is draining the queue.
    engine._stopping.clear()
    # Fill the queue past its capacity.
    for i in range(4):
        try:
            engine._queue.put_nowait(
                RawCaptureEvent(kind=EventKind.CLICK, x=i, y=i, button="left")
            )
        except queue.Full:
            break
    # stop() must return within a few seconds even with a full queue
    # and no worker. The worker join is a no-op when _worker is None.
    start = time.monotonic()
    engine.stop(timeout=1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"stop() blocked for {elapsed:.2f}s on a full queue"


def test_worker_exits_when_stopping_set_even_without_sentinel() -> None:
    """The worker's queue.get() uses a poll timeout so it can exit
    via _stopping even when the sentinel never lands.

    Regression: stop() now does put_nowait for the sentinel, so a
    full queue means the sentinel is silently dropped. The worker
    must still terminate because get() times out, checks _stopping,
    and breaks.
    """
# ----------------------------------------------------- sink failure surfacing


class _ExplodingSink:
    """Raises on every event so we can observe the engine's reaction."""

    def __init__(self) -> None:
        self.calls = 0

    def handle(self, _event: EnrichedEvent) -> None:
        self.calls += 1
        msg = "simulated sink failure"
        raise RuntimeError(msg)


def test_engine_counts_sink_failures() -> None:
    """The engine exposes a monotonic counter of sink.handle() raises.

    Previously a failing sink was logged and forgotten -- a critical
    sink wedging silently (e.g. SessionSink on a disk-full) looked
    identical to "no events yet" from the outside. The counter lets
    the controller assert "recording is healthy".
    """
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    sink = _ExplodingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="enter"))
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="a"))
        _wait_for(lambda: sink.calls >= 2, timeout=1.0)
    finally:
        engine.stop()
    assert engine.sink_failure_count == 2


def test_engine_invokes_on_sink_error_callback() -> None:
    """A registered ``on_sink_error`` callback fires for each failure.

    The callback is the engine's hook for surfacing "recording is
    broken" up to the UI without baking Qt awareness into the
    engine itself.
    """
    errors: list[tuple[object, str]] = []

    def _record(sink: object, exc: BaseException) -> None:
        errors.append((sink, str(exc)))

    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
        on_sink_error=_record,
    )
    sink = _ExplodingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="enter"))
        _wait_for(lambda: len(errors) >= 1, timeout=1.0)
    finally:
        engine.stop()
    assert len(errors) == 1
    assert errors[0][0] is sink
    assert "simulated sink failure" in errors[0][1]


def test_engine_survives_buggy_on_sink_error_callback() -> None:
    """A callback that raises must not crash the capture worker.

    The callback exists for observability; it's not allowed to
    introduce new failure modes. The engine wraps the call so a
    misbehaving handler is logged and the loop continues.
    """
    def _bad_callback(_sink: object, _exc: BaseException) -> None:
        msg = "callback itself raised"
        raise ValueError(msg)

    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
        on_sink_error=_bad_callback,
    )
    sink = _ExplodingSink()
    engine.add_sink(sink)
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="a"))
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="b"))
        _wait_for(lambda: sink.calls >= 2, timeout=1.0)
    finally:
        engine.stop()
    # Both events processed despite the buggy callback; counter still
    # advanced; worker didn't die.
    assert engine.sink_failure_count == 2


def test_engine_one_failing_sink_does_not_block_others() -> None:
    """A sink that raises must not prevent other sinks from receiving
    the same event. Preserves the existing best-effort fan-out so a
    non-critical sink (LiveStepGenerator) failing doesn't drop the
    SessionSink write that immediately follows it.
    """
    failing = _ExplodingSink()
    healthy = _CollectingSink()
    engine = CaptureEngine(
        foreground_factory=_FakeForeground,
        resolver_factory=_fake_resolver,
    )
    engine.start()
    try:
        # Set _stopping directly without going through stop() so the
        # sentinel is never put. The worker's get() must time out
        # within _STOP_POLL_INTERVAL_S and notice the flag.
        engine._stopping.set()
        assert engine._worker is not None
        engine._worker.join(timeout=engine_module._STOP_POLL_INTERVAL_S * 4)
        assert not engine._worker.is_alive()
    finally:
        engine._worker = None  # avoid stop() trying to re-join
    engine.add_sink(failing)  # registered first; raises
    engine.add_sink(healthy)  # registered second; must still get the event
    engine.start()
    try:
        engine.submit(RawCaptureEvent(kind=EventKind.KEY_PRESS, key="enter"))
        _wait_for(lambda: len(healthy.events) >= 1, timeout=1.0)
    finally:
        engine.stop()
    assert len(healthy.events) == 1
    assert engine.sink_failure_count == 1
