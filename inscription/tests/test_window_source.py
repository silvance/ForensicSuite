"""WindowFocusSource identity keying.

The source keys "did the window change?" on the native handle when
available, not on the title. This stops Notepad-style title updates
(``"*h - Notepad"`` → ``"*he - Notepad"``) from producing one spurious
switch event per keystroke.
"""

from __future__ import annotations

from inscription.capture.window_source import _identity
from inscription.platform import ForegroundInfo


def _info(*, title: str, hwnd: int | None, process: str = "notepad.exe") -> ForegroundInfo:
    return ForegroundInfo(
        window_title=title,
        process_name=process,
        process_id=1,
        hwnd=hwnd,
    )


def test_same_hwnd_different_title_is_the_same_window() -> None:
    a = _info(title="*h - Notepad", hwnd=12345)
    b = _info(title="*he - Notepad", hwnd=12345)
    assert _identity(a) == _identity(b)


def test_different_hwnd_same_title_is_a_different_window() -> None:
    a = _info(title="Untitled - Notepad", hwnd=111)
    b = _info(title="Untitled - Notepad", hwnd=222)
    assert _identity(a) != _identity(b)


def test_falls_back_to_title_when_no_hwnd() -> None:
    a = _info(title="Foo", hwnd=None)
    b = _info(title="Bar", hwnd=None)
    assert _identity(a) != _identity(b)
    c = _info(title="Foo", hwnd=None)
    assert _identity(a) == _identity(c)


def test_process_name_distinguishes_windows_with_same_hwnd_value() -> None:
    # Pathological but cheap to guard: a zero hwnd from a stub inspector
    # shouldn't let two unrelated apps collapse to the same identity.
    a = _info(title="A", hwnd=0, process="a.exe")
    b = _info(title="B", hwnd=0, process="b.exe")
    assert _identity(a) != _identity(b)


# --------------- multi-monitor capture routing ----------------


from inscription.capture.window_source import WindowFocusSource  # noqa: E402
from inscription.platform import (  # noqa: E402
    CapturedImage,
    ForegroundInspector,
    MonitorInfo,
    ScreenCapturer,
)


class _StubInspector(ForegroundInspector):
    def __init__(self, info: ForegroundInfo) -> None:
        self._info = info

    def inspect(self) -> ForegroundInfo:
        return self._info


class _RecordingCapturer(ScreenCapturer):
    """Records which monitor ``capture()`` / ``capture_at()`` chose."""

    def __init__(self) -> None:
        self.last_index: int | None = None

    def list_monitors(self) -> list[MonitorInfo]:
        # Left monitor is "secondary" (mss often enumerates it as index 1
        # on Windows when the user's primary lives on the right).
        return [
            MonitorInfo(index=0, left=0, top=0, width=3840, height=1080),
            MonitorInfo(index=1, left=0, top=0, width=1920, height=1080),
            MonitorInfo(index=2, left=1920, top=0, width=1920, height=1080),
        ]

    def capture(self, monitor_index: int | None = None) -> CapturedImage:
        idx = monitor_index if monitor_index is not None else 1
        self.last_index = idx
        return CapturedImage(png_bytes=b"x", width=1, height=1, monitor_index=idx)


def _window_source_with(capturer: _RecordingCapturer) -> WindowFocusSource:
    src = WindowFocusSource(inspector=_StubInspector(_info(title="", hwnd=None)))
    # Inject the capturer directly — skip the thread to keep the unit
    # test deterministic.
    src._screen = capturer  # type: ignore[attr-defined]
    return src


def _window_info(rect: tuple[int, int, int, int]) -> ForegroundInfo:
    return ForegroundInfo(
        window_title="T",
        process_name="p.exe",
        process_id=1,
        hwnd=123,
        window_rect=rect,
    )


def test_capture_picks_monitor_under_the_window_center() -> None:
    cap = _RecordingCapturer()
    src = _window_source_with(cap)
    # Window on the "right" monitor (index 2: 1920..3840 x 0..1080).
    png, _, _, _, _ = src._capture(_window_info((2000, 100, 3000, 800)))  # type: ignore[attr-defined]
    assert cap.last_index == 2
    assert png == b"x"


def test_capture_picks_left_monitor_for_left_window() -> None:
    cap = _RecordingCapturer()
    src = _window_source_with(cap)
    png, _, _, _, _ = src._capture(_window_info((100, 100, 900, 800)))  # type: ignore[attr-defined]
    assert cap.last_index == 1
    assert png == b"x"


def test_capture_falls_back_when_rect_is_missing() -> None:
    cap = _RecordingCapturer()
    src = _window_source_with(cap)
    png, _, _, _, _ = src._capture(_info(title="x", hwnd=5))  # type: ignore[attr-defined]
    # No rect → primary fallback path.
    assert cap.last_index == 1
    assert png == b"x"


# --------------- announce process on first focus ----------------


from inscription.capture.events import RawCaptureEvent  # noqa: E402, TC001
from inscription.model import EventKind  # noqa: E402


class _RecordingEngine:
    """Catches every ``submit`` call so the source's emissions are inspectable."""

    def __init__(self) -> None:
        self.submitted: list[RawCaptureEvent] = []

    def submit(self, event: RawCaptureEvent) -> None:
        self.submitted.append(event)


def _info_with_path(*, path: str | None, name: str = "axiom.exe") -> ForegroundInfo:
    return ForegroundInfo(
        window_title="AXIOM Examine",
        process_name=name,
        process_id=4242,
        process_path=path,
        hwnd=99,
    )


def test_first_focus_emits_marker_with_version() -> None:
    src = WindowFocusSource(
        inspector=_StubInspector(_info_with_path(path=r"C:\Magnet\axiom.exe")),
        version_reader=lambda _path: "8.6.0.42301",
    )
    engine = _RecordingEngine()

    src._announce_process_if_new(engine, _info_with_path(path=r"C:\Magnet\axiom.exe"))  # type: ignore[arg-type]

    assert len(engine.submitted) == 1
    marker = engine.submitted[0]
    assert marker.kind is EventKind.MARKER
    assert marker.text == "Foreground app: axiom.exe v8.6.0.42301"


def test_repeat_focus_for_same_process_does_not_re_announce() -> None:
    src = WindowFocusSource(
        inspector=_StubInspector(_info_with_path(path=r"C:\Magnet\axiom.exe")),
        version_reader=lambda _p: "8.6.0.42301",
    )
    engine = _RecordingEngine()

    info = _info_with_path(path=r"C:\Magnet\axiom.exe")
    src._announce_process_if_new(engine, info)  # type: ignore[arg-type]
    src._announce_process_if_new(engine, info)  # type: ignore[arg-type]
    src._announce_process_if_new(engine, info)  # type: ignore[arg-type]

    assert len(engine.submitted) == 1


def test_focus_with_unreadable_version_still_announces_name() -> None:
    src = WindowFocusSource(
        inspector=_StubInspector(_info_with_path(path=r"C:\foo.exe")),
        version_reader=lambda _p: None,
    )
    engine = _RecordingEngine()

    src._announce_process_if_new(engine, _info_with_path(path=r"C:\foo.exe", name="foo.exe"))  # type: ignore[arg-type]

    assert engine.submitted[0].text == "Foreground app: foo.exe"


def test_focus_without_process_path_emits_nothing() -> None:
    """The non-Windows fallback inspector returns ``process_path=None`` --
    we shouldn't fabricate a marker out of an unknown binary."""
    src = WindowFocusSource(
        inspector=_StubInspector(_info_with_path(path=None)),
        version_reader=lambda _p: "x",  # would still return something
    )
    engine = _RecordingEngine()

    src._announce_process_if_new(engine, _info_with_path(path=None))  # type: ignore[arg-type]

    assert engine.submitted == []


def test_focus_announces_each_distinct_process_once() -> None:
    src = WindowFocusSource(
        inspector=_StubInspector(_info_with_path(path=r"C:\a.exe")),
        version_reader=lambda p: "1.0.0.0" if "a" in p else "2.0.0.0",
    )
    engine = _RecordingEngine()

    src._announce_process_if_new(engine, _info_with_path(path=r"C:\a.exe", name="a.exe"))  # type: ignore[arg-type]
    src._announce_process_if_new(engine, _info_with_path(path=r"C:\b.exe", name="b.exe"))  # type: ignore[arg-type]
    src._announce_process_if_new(engine, _info_with_path(path=r"C:\a.exe", name="a.exe"))  # type: ignore[arg-type]

    assert [e.text for e in engine.submitted] == [
        "Foreground app: a.exe v1.0.0.0",
        "Foreground app: b.exe v2.0.0.0",
    ]
