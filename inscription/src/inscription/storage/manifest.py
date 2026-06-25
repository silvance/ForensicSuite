"""JSON manifest read/write.

The manifest duplicates a summary of session metadata so the session picker
can populate without opening every SQLite database. It is derived from
``session.db`` and rewritten on every save; the DB remains source of truth.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING

from suite_common.atomic import atomic_write_text

from inscription.model import SCHEMA_VERSION, SessionManifest

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def write_manifest(path: Path, manifest: SessionManifest) -> None:
    data = {
        "name": manifest.name,
        "started_at": manifest.started_at.isoformat(),
        "ended_at": manifest.ended_at.isoformat() if manifest.ended_at else None,
        "event_count": manifest.event_count,
        "step_count": manifest.step_count,
        "schema_version": manifest.schema_version,
        "tags": manifest.tags,
    }
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def read_manifest(path: Path) -> SessionManifest:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ended_raw = raw.get("ended_at")
    return SessionManifest(
        name=raw["name"],
        started_at=datetime.fromisoformat(raw["started_at"]),
        ended_at=datetime.fromisoformat(ended_raw) if ended_raw else None,
        event_count=int(raw.get("event_count", 0)),
        step_count=int(raw.get("step_count", 0)),
        schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        tags=list(raw.get("tags", [])),
    )
