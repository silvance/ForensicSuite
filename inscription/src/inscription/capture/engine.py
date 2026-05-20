"""Capture engine orchestration.

The engine runs a worker thread that pulls :class:`RawCaptureEvent` objects
off a queue, enriches them with foreground info and (for clicks) a resolved
UI element, and fans the result out to registered sinks.

Screenshots are captured on the source's own thread (``mss`` is not
thread-safe) and attached to the raw event, not taken here. See
:mod:`inscription.capture.events`.

Sources (click, keyboard, window-focus) submit events via
:meth:`CaptureEngine.submit`; they don't need to know about sinks. Sinks
consume enriched events; they don't need to know about sources.

``ForegroundInspector`` and ``ElementResolver`` are constructed inside
the worker thread via factory callables because UIA isn't thread-safe.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import sys
import threading
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from inscription.capture.events import RawCaptureEvent
from inscription.model import EventKind, utcnow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime

    from inscription.model import ResolvedElement
    from inscription.platform import ForegroundInfo, ForegroundInspector
    from inscription.resolve import ElementResolver

logger = logging.getLogger(__name__)

_STOP_SENTINEL = object()

#: HRESULT returned by ``CoInitializeEx`` when COM is already
#: initialised on the calling thread with a different apartment model.
#: Treated as a benign no-op: the existing apartment owner is
#: responsible for the matching ``CoUninitialize``.
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 as a signed 32-bit int


@contextmanager
def _com_apartment() -> Iterator[None]:
    """Initialise STA COM for the lifetime of the worker thread.

    Windows UI Automation is a COM API; ``pywinauto`` ultimately calls
    into it through ``comtypes``. Every thread that touches COM must
    have an apartment initialised first, otherwise the calls are
    undefined behaviour -- in practice the resolver returns garbage,
    or hangs, or works only because some unrelated import happened to
    have run ``CoInitializeEx`` as a side-effect on this thread.

    UIA is documented as STA-compatible (it marshals to its own
    server thread internally), so we use COINIT_APARTMENTTHREADED to
    match what pywinauto's main-thread usage assumes. ``RPC_E_CHANGED_MODE``
    means COM was already initialised on this thread with a different
    apartment model; we leave the existing one in place and skip the
    paired uninit, matching the ``CoInitializeEx`` contract.

    No-op on non-Windows platforms -- there is no resolver there to
    talk to UIA, so there's nothing to initialise.
    """
    if sys.platform != "win32":
        yield
        return
    try:
        import comtypes  # noqa: PLC0415 - Windows-only optional dep
    except Exception as exc:
        logger.warning("comtypes unavailable; UIA resolver may misbehave: %s", exc)
        yield
        return
    initialised = False
    try:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            initialised = True
        except OSError as exc:
            if getattr(exc, "winerror", None) == _RPC_E_CHANGED_MODE:
                logger.debug("COM already initialised on capture thread with a different apartment")
            else:
                logger.warning("CoInitializeEx failed on capture thread: %s", exc)
        yield
    finally:
        if initialised:
            try:
                comtypes.CoUninitialize()
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("CoUninitialize on capture thread raised: %s", exc)


@dataclass(slots=True, kw_only=True)
class EnrichedEvent:
    """A raw event plus everything the sink needs to persist it.

    Sinks run sequentially in registration order; ``SessionSink`` writes
    the persisted ``raw_events.id`` and ``screenshot_artifacts.id`` onto
    these mutable fields so downstream sinks (e.g.
    :class:`inscription.steps.live.LiveStepGenerator`) can reference the
    just-saved row without re-querying.
    """

    raw: RawCaptureEvent
    processed_at: datetime
    foreground: ForegroundInfo
    image_sha256: str = ""
    resolved: ResolvedElement | None = None
    persisted_event_id: int | None = None
    persisted_screenshot_id: int | None = None
    persisted_resolved_id: int | None = None


class CaptureSource(ABC):
    """A producer of :class:`RawCaptureEvent` objects."""

    @abstractmethod
    def start(self, engine: CaptureEngine) -> None:
        """Begin producing events, submitting them to ``engine``."""

    @abstractmethod
    def stop(self) -> None:
        """Stop producing events. Safe to call multiple times."""


@runtime_checkable
class CaptureSink(Protocol):
    """Consumes :class:`EnrichedEvent` objects."""

    def handle(self, event: EnrichedEvent) -> None:  # pragma: no cover - protocol
        ...


class CaptureEngine:
    """Thread-safe producer/consumer capture engine."""

    def __init__(
        self,
        *,
        foreground_factory: Callable[[], ForegroundInspector],
        resolver_factory: Callable[[ForegroundInspector], ElementResolver],
        queue_maxsize: int = 256,
        own_pid: int | None = None,
        on_sink_error: Callable[[CaptureSink, BaseException], None] | None = None,
    ) -> None:
        self._foreground_factory = foreground_factory
        self._resolver_factory = resolver_factory
        self._queue: queue.Queue[object] = queue.Queue(maxsize=queue_maxsize)
        self._sinks: list[CaptureSink] = []
        self._sources: list[CaptureSource] = []
        self._worker: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        # Inscription's own pid. Events whose foreground process matches
        # this are dropped silently — examiners frequently click back into
        # Inscription mid-recording to read the live notes or tweak a
        # step, and those clicks are noise, not part of the workflow.
        # Markers are explicitly exempt because they are user-intent.
        self._own_pid = own_pid if own_pid is not None else os.getpid()
        # Sink-failure observability. The previous behaviour was to log
        # the exception and continue, which masked persistent failures
        # of critical sinks (e.g. SessionSink wedging on a disk-full
        # error meant the operator kept clicking with zero events
        # actually landing in the DB). The counter and optional
        # callback let the controller surface "recording is broken"
        # to the UI; the engine itself stays best-effort because some
        # sinks (LiveStepGenerator) are non-critical and shouldn't
        # stop capture on a transient failure.
        self._sink_failure_count = 0
        self._on_sink_error = on_sink_error

    # -------------------------------------------------------- sinks/sources

    def add_sink(self, sink: CaptureSink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove_sink(self, sink: CaptureSink) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def add_source(self, source: CaptureSource) -> None:
        with self._lock:
            self._sources.append(source)
        source.start(self)

    def stop_sources(self) -> None:
        with self._lock:
            sources = list(self._sources)
            self._sources.clear()
        for src in sources:
            try:
                src.stop()
            except Exception as exc:
                logger.warning("Error stopping source %r: %s", src, exc)

    # -------------------------------------------------------- observability

    @property
    def sink_failure_count(self) -> int:
        """Monotonic count of sink.handle() exceptions across this engine.

        Useful for the controller to assert "recording is healthy" --
        a non-zero count after a few seconds of capture means at least
        one sink has been raising. Reads are unsynchronised because
        Python int reads are atomic, but the value is incremented
        under ``self._lock`` so concurrent failures don't drop counts.
        """
        return self._sink_failure_count

    # -------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stopping.clear()
        self._worker = threading.Thread(target=self._run, name="inscription-capture", daemon=True)
        self._worker.start()
        logger.info("Capture engine started")

    def stop(self, *, timeout: float = 5.0) -> None:
        self.stop_sources()
        self._stopping.set()
        self._queue.put(_STOP_SENTINEL)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
        logger.info("Capture engine stopped")

    # -------------------------------------------------------- submission

    def submit(self, event: RawCaptureEvent) -> bool:
        """Enqueue a raw event. Returns False if the queue is full or the
        engine is shutting down."""
        if self._stopping.is_set():
            return False
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.warning("Capture queue full; dropping %r", event.kind)
            return False
        return True

    # -------------------------------------------------------- internals

    def _run(self) -> None:
        with _com_apartment():
            try:
                foreground = self._foreground_factory()
                resolver = self._resolver_factory(foreground)
            except Exception:
                logger.exception("Failed to initialise capture platform")
                return

            while True:
                item = self._queue.get()
                if item is _STOP_SENTINEL:
                    self._queue.task_done()
                    break
                if not isinstance(item, RawCaptureEvent):
                    # Defensive: only _STOP_SENTINEL or RawCaptureEvent
                    # objects are ever submitted to this queue, so
                    # reaching here means a programmer error in
                    # submit_event. We previously used `assert
                    # isinstance(...)`, which gets stripped under
                    # `python -O`. Logging + skipping is safer: the
                    # rest of the queue keeps draining instead of
                    # having the worker crash.
                    logger.error(
                        "engine: discarded unexpected queue item %r (type %s)",
                        item,
                        type(item).__name__,
                    )
                    self._queue.task_done()
                    continue
                try:
                    self._process(item, foreground=foreground, resolver=resolver)
                except Exception:
                    logger.exception("Processing failed for %r", item)
                finally:
                    self._queue.task_done()

    def _process(
        self,
        raw: RawCaptureEvent,
        *,
        foreground: ForegroundInspector,
        resolver: ElementResolver,
    ) -> None:
        fg = foreground.inspect()

        # Drop everything except markers when the foreground belongs to
        # Inscription itself. The examiner is interacting with the
        # recorder window, not the workflow under examination. Markers
        # come straight from the controller as a deliberate signal, so
        # let those through regardless.
        if (
            raw.kind is not EventKind.MARKER
            and fg.process_id is not None
            and fg.process_id == self._own_pid
        ):
            logger.debug("Dropping self-event (pid=%s, kind=%s)", fg.process_id, raw.kind)
            return

        sha = hashlib.sha256(raw.png_bytes).hexdigest() if raw.png_bytes else ""

        resolved = None
        is_click = raw.kind in {EventKind.CLICK, EventKind.DOUBLE_CLICK}
        if is_click and raw.x is not None and raw.y is not None:
            try:
                resolved = resolver.resolve_at(raw.x, raw.y)
            except Exception:
                logger.exception("Resolver failed at (%s,%s)", raw.x, raw.y)

        enriched = EnrichedEvent(
            raw=raw,
            processed_at=utcnow(),
            foreground=fg,
            image_sha256=sha,
            resolved=resolved,
        )

        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            try:
                sink.handle(enriched)
            except Exception as exc:
                logger.exception("Sink %r failed", sink)
                with self._lock:
                    self._sink_failure_count += 1
                callback = self._on_sink_error
                if callback is not None:
                    # Wrap the callback so a faulty handler can't crash
                    # the capture loop -- the whole point of an error
                    # callback is observability, not a new way to
                    # break recording.
                    try:
                        callback(sink, exc)
                    except Exception:
                        logger.exception("on_sink_error callback raised; ignoring")
