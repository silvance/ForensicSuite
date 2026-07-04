"""Domain model for Inscription.

Inscription records a *session* — a user workflow on the desktop. A session
is a timeline of :class:`RawEvent` objects (clicks, window focus changes,
keyboard milestones) with optional :class:`ResolvedElement` metadata from
UI Automation and :class:`ScreenshotArtifact` images. Step generation
collapses that timeline into :class:`DraftStep` rows for review and export.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


SCHEMA_VERSION = 7


class EventKind(StrEnum):
    """Kind of raw event captured during a session."""

    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    KEY_PRESS = "key_press"
    SCROLL = "scroll"
    WINDOW_FOCUS = "window_focus"
    MARKER = "marker"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionInfo:
    """Top-level metadata for one recorded session."""

    name: str
    started_at: datetime
    ended_at: datetime | None = None
    recorder_version: str = ""
    schema_version: int = SCHEMA_VERSION


@dataclass(frozen=True, slots=True, kw_only=True)
class Session:
    """Session metadata + the filesystem root where its artifacts live."""

    info: SessionInfo
    root: Path

    @property
    def screenshots_dir(self) -> Path:
        return self.root / "screenshots"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def db_path(self) -> Path:
        return self.root / "session.db"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    @property
    def internal_dir(self) -> Path:
        return self.root / ".inscription"


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedElement:
    """A UI element resolved for a click event.

    ``confidence`` is 0..1. 0 means "nothing useful"; 0.9+ means UIA gave us
    a named, typed control. Step generation uses it to decide how specific
    the draft step text can be.

    ``bounding_rect`` is ``(left, top, right, bottom)`` in screen pixels
    when UIA supplies one, letting the exporter crop the screenshot tight
    around the clicked element.
    """

    id: int | None
    name: str | None = None
    control_type: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    role: str | None = None
    confidence: float = 0.0
    method: str = "none"  # "uia" | "foreground-only" | "none"
    bounding_rect: tuple[int, int, int, int] | None = None
    #: Process that actually owns the element (from UIA's ``process_id``).
    #: When this differs from the foreground process — taskbar, Start menu,
    #: Alt-Tab switcher clicks — step generation drops the "in X window"
    #: suffix, which would otherwise glue the shell element onto whatever
    #: app the user was previously using.
    owner_process_name: str | None = None
    #: Up to a few sibling text labels (e.g. neighboring TextBlocks /
    #: Statics) collected from the clicked element's parent. Lets the
    #: LLM reconstruct the meaning of an icon-only button or a generic
    #: "Pane" / "Custom" element whose own ``name`` is empty or
    #: meaningless. Joined with " | " so the model sees
    #: ``"Hash | Verify | Algorithm: SHA-256"`` as one short hint.
    nearby_text: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ScreenshotArtifact:
    """A screenshot persisted on disk with its relative path.

    ``sha256`` is the hex digest of the PNG bytes, recorded at capture time
    so the raw layer remains verifiable if someone later edits or replaces
    the on-disk file.
    """

    id: int | None
    relative_path: str
    captured_at: datetime
    width: int
    height: int
    sha256: str = ""
    highlight_rect: tuple[int, int, int, int] | None = None
    #: The captured region's top-left in GLOBAL screen coordinates.
    #: (0, 0) for the primary monitor and for legacy rows that predate
    #: schema v7. Export subtracts this to translate screen-space UIA
    #: rects / click points into this image's pixel space -- without
    #: it, a click on a secondary monitor crops the wrong region.
    origin_left: int = 0
    origin_top: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class RawEvent:
    """One raw event on the session timeline."""

    id: int | None
    sequence: int
    occurred_at: datetime
    kind: EventKind
    button: str | None = None
    x: int | None = None
    y: int | None = None
    key: str | None = None
    text: str | None = None
    window_title: str | None = None
    process_name: str | None = None
    screenshot_id: int | None = None
    resolved_element_id: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DraftStep:
    """A draft step produced from one or more raw events.

    ``action`` is what the examiner did, in imperative voice — the same
    column forensic exam notes use ("Loaded E01 file into FTK Imager").
    ``result`` is the outcome or observation — the second column on those
    notes ("Hash verified"). ``result`` is optional; many steps are pure
    actions without a separate observation.

    ``source_event_ids`` records the events that contributed, so regenerating
    steps can preserve manual edits where the underlying events are unchanged.
    ``suppressed`` is a soft-delete: a suppressed step is kept for undo and
    is excluded from export. ``evidentiary`` is the integration flag that
    downstream report-builder tools query on — it's the examiner saying
    "this step matters for the final report".
    """

    id: int | None
    sequence: int
    action: str
    result: str = ""
    source_event_ids: tuple[int, ...] = ()
    screenshot_id: int | None = None
    suppressed: bool = False
    manual_edit: bool = False
    evidentiary: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class ExportDocument:
    """A generated export artifact on disk."""

    session_name: str
    format: str  # "html" for alpha
    path: Path
    generated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionManifest:
    """Lightweight summary persisted alongside ``session.db`` as JSON.

    Lets the session picker list sessions without opening every database.
    """

    name: str
    started_at: datetime
    ended_at: datetime | None
    event_count: int
    step_count: int
    schema_version: int = SCHEMA_VERSION
    tags: list[str] = field(default_factory=list)
