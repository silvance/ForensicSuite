"""Capture engine: end-to-end with a fake foreground/resolver."""

from __future__ import annotations

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
