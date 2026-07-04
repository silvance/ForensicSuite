"""SessionSink: timestamp-based filenames survive recording restarts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from inscription.capture import EnrichedEvent, RawCaptureEvent, SessionSink
from inscription.model import EventKind, ResolvedElement, utcnow
from inscription.platform import ForegroundInfo
from inscription.storage import SessionRepository


def _make_enriched(*, png: bytes, processed_at: datetime | None = None) -> EnrichedEvent:
    return EnrichedEvent(
        raw=RawCaptureEvent(
            kind=EventKind.CLICK,
            button="left",
            x=10,
            y=10,
            png_bytes=png,
            png_width=1,
            png_height=1,
        ),
        processed_at=processed_at or utcnow(),
        foreground=ForegroundInfo(window_title="App", process_name="app.exe", process_id=1),
        image_sha256="hash",
        resolved=ResolvedElement(
            id=None, name="OK", control_type="Button", confidence=0.9, method="uia"
        ),
    )


def test_sink_filenames_are_unique_across_recording_restarts(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Restart")
    try:
        t0 = datetime(2026, 4, 24, 7, 21, 50, 100000, tzinfo=UTC)
        first = SessionSink(repo)
        first.handle(_make_enriched(png=b"first", processed_at=t0))
        first.handle(_make_enriched(png=b"second", processed_at=t0 + timedelta(microseconds=1)))

        # Simulate stop → re-start on the same session.
        second = SessionSink(repo)
        second.handle(_make_enriched(png=b"third", processed_at=t0 + timedelta(microseconds=2)))

        screenshots = repo.list_screenshots()
        paths = sorted(s.relative_path for s in screenshots)
        assert len(paths) == 3
        assert len(set(paths)) == 3  # all unique
        assert all(p.startswith("screenshots/event-") and p.endswith(".png") for p in paths)
    finally:
        repo.close()


def test_sink_uniquifies_identical_processed_at(tmp_path) -> None:
    """Two events sharing a processed_at microsecond must land in two
    files -- overwriting would destroy the first event's screenshot
    while leaving two DB rows pointing at one file.

    The timestamps CAN collide in production: grabs happen on source
    threads (click listener + window poll running concurrently), and
    an NTP step-back can repeat a wall-clock microsecond.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="Collide")
    try:
        t0 = datetime(2026, 4, 24, 7, 21, 50, 123456, tzinfo=UTC)
        sink = SessionSink(repo)
        sink.handle(_make_enriched(png=b"FIRST", processed_at=t0))
        sink.handle(_make_enriched(png=b"SECOND", processed_at=t0))

        shots = repo.list_screenshots()
        assert len(shots) == 2
        paths = [s.relative_path for s in shots]
        assert len(set(paths)) == 2
        bodies = sorted(
            (repo.session.root / p).read_bytes() for p in paths
        )
        assert bodies == [b"FIRST", b"SECOND"]  # neither overwritten
    finally:
        repo.close()


def test_sink_persists_event_when_screenshot_write_fails(tmp_path, monkeypatch) -> None:
    """A failed PNG write must not take the raw event down with it.

    The event row is the evidentiary core; previously an OSError from
    write_bytes aborted the whole handle() and the event vanished from
    the record entirely. Now the event is saved without its image.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="DiskFull")
    try:
        sink = SessionSink(repo)

        def _boom(_self, _data) -> None:
            msg = "No space left on device"
            raise OSError(msg)

        monkeypatch.setattr(Path, "write_bytes", _boom)
        event = _make_enriched(png=b"wont-fit")
        sink.handle(event)

        events = repo.list_events()
        assert len(events) == 1
        assert events[0].screenshot_id is None  # image lost, event kept
        assert repo.list_screenshots() == []    # no dangling artifact row
    finally:
        repo.close()
