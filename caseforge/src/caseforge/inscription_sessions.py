"""Read Inscription session manifests from a case directory.

CaseForge is read-only on Inscription's output: it walks the case
directory, parses each ``<session-slug>/manifest.json``, and returns a
list of :class:`InscriptionSession` summaries for the case view's
"Sessions" tab. We deliberately avoid opening Inscription's
``session.db`` to keep CaseForge dependency-free of SQLite parsing —
the manifest carries everything the case home page needs.

The manifest schema lives in Inscription
(``inscription/storage/manifest.py``) and is part of the integration
contract documented in ``inscription/docs/integration.md``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from caseforge.model import coerce_int, parse_iso, parse_optional_iso

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "manifest.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class InscriptionSession:
    """One Inscription session's manifest summary."""

    slug: str
    name: str
    started_at: datetime
    ended_at: datetime | None
    event_count: int
    step_count: int
    path: str

    @property
    def is_in_progress(self) -> bool:
        return self.ended_at is None


def list_inscription_sessions(case_dir: Path) -> list[InscriptionSession]:
    """Return every Inscription session under ``case_dir``, newest first.

    Subdirectories without a manifest are silently skipped (a partially
    initialised or non-Inscription folder shouldn't break the case
    view). Malformed manifests are logged and skipped.
    """
    if not case_dir.exists() or not case_dir.is_dir():
        return []
    sessions: list[InscriptionSession] = []
    # No pre-sort: the final ``sessions.sort`` below orders by
    # started_at, so any pre-sorted directory order is wasted CPU and
    # misleads readers into thinking the directory order matters.
    for child in case_dir.iterdir():
        if not child.is_dir():
            continue
        manifest_path = child / MANIFEST_FILENAME
        # ``is_file`` rejects symlinks-to-directories and a directory
        # that someone substituted for the manifest file.
        if not manifest_path.is_file():
            continue
        parsed = _parse_manifest(manifest_path, slug=child.name)
        if parsed is not None:
            sessions.append(parsed)
    sessions.sort(key=lambda s: s.started_at, reverse=True)
    return sessions


def _parse_manifest(path: Path, *, slug: str) -> InscriptionSession | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Skipping malformed manifest %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Manifest %s is not a JSON object", path)
        return None
    return InscriptionSession(
        slug=slug,
        name=str(raw.get("name") or slug),
        started_at=parse_iso(raw.get("started_at")),
        ended_at=parse_optional_iso(raw.get("ended_at")),
        event_count=coerce_int(raw.get("event_count"), default=0),
        step_count=coerce_int(raw.get("step_count"), default=0),
        path=str(path.parent.resolve()),
    )
