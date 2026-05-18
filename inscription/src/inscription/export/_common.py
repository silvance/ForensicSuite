"""Helpers shared across the HTML, Markdown, and Forensic-notes exporters.

The three exporters used to each carry their own copies of the
"pick the primary source event for a step" and "render the staged
crop+highlight PNG into the destination folder" routines, which
drifted slightly (especially around what to return when staging
failed). This module is the single source of truth.

Conventions:

- ``select_primary_event`` picks the source event whose screenshot a
  step is displaying. It prefers the event that contributed the
  step's ``screenshot_id``, falling back to the first resolvable
  source event.
- ``stage_step_asset`` writes the rendered PNG into ``assets_dir``
  and returns the **basename only**. Each caller composes the URL
  against its own document (``assets/<name>``,
  ``<custom-dir>/<name>``, or just ``<name>`` for Markdown).
- ``build_event_resolver`` produces the per-session
  resolved-element cache all three exporters were constructing
  inline.
"""

from __future__ import annotations

import logging
import shutil
from typing import TYPE_CHECKING

from inscription.render import crop_highlight

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from inscription.model import (
        DraftStep,
        RawEvent,
        ResolvedElement,
        ScreenshotArtifact,
    )
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


def atomic_write_text(destination: Path, body: str, *, encoding: str = "utf-8") -> None:
    """Write ``body`` to ``destination`` via temp-file + rename.

    A crash or disk-full mid-write must not leave a truncated
    deliverable at the destination path -- a half-written HTML /
    Markdown / forensic-notes file masquerading as the real export
    is worse than a missing file. Mirrors the temp-file pattern used
    by ``submitted.mark`` and the manifest writer.
    """
    tmp = destination.with_suffix(destination.suffix + ".tmp")
    tmp.write_text(body, encoding=encoding)
    tmp.replace(destination)


def select_primary_event(
    step: DraftStep, events_by_id: dict[int, RawEvent]
) -> RawEvent | None:
    """Pick the source event whose screenshot the step is displaying.

    Most steps have exactly one source event so this is usually trivial.
    When a step was merged from several (rapid clicks on the same target),
    we prefer the event that contributed the screenshot — that's the one
    whose click point the crop needs.
    """
    for eid in step.source_event_ids:
        event = events_by_id.get(eid)
        if event is None:
            continue
        if step.screenshot_id is not None and event.screenshot_id == step.screenshot_id:
            return event
    for eid in step.source_event_ids:
        event = events_by_id.get(eid)
        if event is not None:
            return event
    return None


def stage_step_asset(
    *,
    step: DraftStep,
    shot: ScreenshotArtifact,
    primary_event: RawEvent | None,
    element: ResolvedElement | None,
    session_root: Path,
    assets_dir: Path,
) -> str | None:
    """Render the per-step PNG into ``assets_dir``.

    Returns the asset's basename on success, ``None`` on read failure.
    Callers compose the URL relative to their own document.

    When a UIA bounding rect is available, the image is the crop +
    click marker rendered by :func:`inscription.render.crop_highlight`.
    Without a bounding rect, the raw screenshot is copied through
    unchanged.
    """
    src_path = session_root / shot.relative_path
    step_id = step.id if step.id is not None else step.sequence
    target = assets_dir / f"step-{step_id:05d}.png"

    try:
        raw_bytes = src_path.read_bytes()
    except OSError as exc:
        logger.warning("Could not read source screenshot %s: %s", src_path, exc)
        return None

    rect = element.bounding_rect if element is not None else None
    click = (
        (primary_event.x, primary_event.y)
        if primary_event is not None
        and primary_event.x is not None
        and primary_event.y is not None
        else None
    )

    if rect is None:
        try:
            shutil.copyfile(src_path, target)
        except OSError as exc:
            logger.warning("Could not stage asset %s: %s", src_path, exc)
            return None
        return target.name

    rendered = crop_highlight(raw_bytes, bounding_rect=rect, click_point=click)
    target.write_bytes(rendered)
    return target.name


def build_event_resolver(
    repository: SessionRepository,
) -> Callable[[RawEvent], ResolvedElement | None]:
    """Per-session resolved-element cache.

    Click events reference a ``resolved_elements`` row by id; the same
    resolved element often backs many events (a button clicked twice
    points at one row). The cache means we hit the DB once per unique
    element per export.
    """
    cache: dict[int, ResolvedElement | None] = {}

    def resolve(event: RawEvent) -> ResolvedElement | None:
        eid = event.resolved_element_id
        if eid is None:
            return None
        if eid not in cache:
            cache[eid] = repository.get_resolved_element(eid)
        return cache[eid]

    return resolve
