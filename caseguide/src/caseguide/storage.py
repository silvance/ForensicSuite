"""Read / write ``<case-root>/.caseguide/suggestions.json``.

CaseGuide owns this file inside the case directory. The format is the
contract documented in ``inscription/docs/integration.md``; Inscription
reads it (read-only) to surface the suggestions panel.

Atomic writes: payload goes to ``suggestions.json.tmp`` and is then
renamed over the existing file so a crash mid-write can't leave the
file truncated.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from suite_common.atomic import atomic_write_text

from caseguide.model import (
    PRIORITY_RECOMMENDED,
    SUGGESTIONS_SCHEMA_VERSION,
    Suggestion,
    SuggestionsDocument,
    coerce_bool,
    coerce_int,
    parse_iso,
    string_list,
    utcnow,
)

logger = logging.getLogger(__name__)

CASEGUIDE_DIRNAME = ".caseguide"
SUGGESTIONS_FILENAME = "suggestions.json"

# Hard cap on the size of suggestions.json. Even a maximalist case with
# hundreds of suggestions and long rationales fits comfortably in a few
# hundred KB — anything larger is corrupt or hostile, and the load path
# refuses to read it into memory rather than risk a slow parse / OOM.
_MAX_SUGGESTIONS_BYTES = 10 * 1024 * 1024


class StorageError(Exception):
    """Wrapper around any suggestions.json read/write failure."""


class SuggestionsSchemaVersionError(StorageError):
    """The on-disk suggestions.json was written by a newer CaseGuide.

    Reading a future format with this build risks silently dropping
    fields the operator's marked decisions live in -- e.g. a new
    ``locked`` flag or a renamed ``priority`` shape. Raising here is
    safer than parsing with defaults and rewriting the file, which
    would truncate the future-build data on the next save.
    """


def suggestions_path(case_dir: Path) -> Path:
    return case_dir / CASEGUIDE_DIRNAME / SUGGESTIONS_FILENAME


def read_suggestions(case_dir: Path) -> SuggestionsDocument | None:
    """Load the saved suggestions document, or None if not present.

    Missing file is the normal "first time CaseGuide opens this case"
    state; callers should fall back to "generate fresh" when this
    returns None.
    """
    target = suggestions_path(case_dir)
    if not target.exists():
        return None
    try:
        size = target.stat().st_size
    except OSError as exc:
        msg = f"Could not stat {target}: {exc}"
        raise StorageError(msg) from exc
    if size > _MAX_SUGGESTIONS_BYTES:
        msg = (
            f"{target} is {size} bytes; refusing to load files larger than "
            f"{_MAX_SUGGESTIONS_BYTES} bytes."
        )
        raise StorageError(msg)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Could not parse {target}: {exc}"
        raise StorageError(msg) from exc
    if not isinstance(raw, dict):
        msg = f"{target} top-level JSON must be an object"
        raise StorageError(msg)
    # Reject a future schema BEFORE _from_json drops unknown fields
    # silently. coerce_int defaults to SUGGESTIONS_SCHEMA_VERSION when
    # the field is missing, which is fine for legacy files but would
    # mask a v3 file we shouldn't load -- so look at the raw value
    # here and refuse explicitly.
    raw_version = raw.get("schema_version")
    if isinstance(raw_version, int) and raw_version > SUGGESTIONS_SCHEMA_VERSION:
        msg = (
            f"{target} schema_version is {raw_version}; this CaseGuide "
            f"build supports up to v{SUGGESTIONS_SCHEMA_VERSION}. Open "
            f"the case with a newer CaseGuide, or back up and delete "
            f"{target.name} to regenerate from scratch."
        )
        raise SuggestionsSchemaVersionError(msg)
    return _from_json(raw)


def write_suggestions(case_dir: Path, doc: SuggestionsDocument) -> Path:
    """Atomically persist ``doc`` to the case's suggestions.json."""
    target = suggestions_path(case_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = _to_json(doc)
    atomic_write_text(
        target,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return target


# ------------------------------------------------------------ JSON shape


def _to_json(doc: SuggestionsDocument) -> dict[str, object]:
    return {
        "schema_version": doc.schema_version,
        "generated_at": doc.generated_at.isoformat(),
        "scope_summary": doc.scope_summary,
        "playbooks": list(doc.playbooks),
        "suggestions": [_suggestion_to_json(s) for s in doc.suggestions],
        "caseguide_version": doc.caseguide_version,
    }


def _suggestion_to_json(s: Suggestion) -> dict[str, object]:
    return {
        "id": s.id,
        "category": s.category,
        "priority": s.priority,
        "action": s.action,
        "expected_result": s.expected_result,
        "rationale": s.rationale,
        "references": list(s.references),
        "depends_on": list(s.depends_on),
        "completed": s.completed,
        "completed_at": s.completed_at.isoformat() if s.completed_at is not None else None,
    }


def _from_json(raw: dict[str, object]) -> SuggestionsDocument:
    suggestions_raw = raw.get("suggestions", [])
    if not isinstance(suggestions_raw, list):
        suggestions_raw = []
    return SuggestionsDocument(
        schema_version=coerce_int(
            raw.get("schema_version", 1), default=SUGGESTIONS_SCHEMA_VERSION
        ),
        generated_at=parse_iso(raw.get("generated_at"), default=utcnow()) or utcnow(),
        scope_summary=str(raw.get("scope_summary", "")),
        playbooks=string_list(raw.get("playbooks")),
        suggestions=[
            _suggestion_from_json(item)
            for item in suggestions_raw
            if isinstance(item, dict)
        ],
        caseguide_version=str(raw.get("caseguide_version", "")),
    )


def _suggestion_from_json(raw: dict[str, object]) -> Suggestion:
    return Suggestion(
        id=str(raw.get("id", "")),
        action=str(raw.get("action", "")),
        category=str(raw.get("category", "")),
        priority=str(raw.get("priority", PRIORITY_RECOMMENDED)),
        expected_result=str(raw.get("expected_result", "")),
        rationale=str(raw.get("rationale", "")),
        references=string_list(raw.get("references")),
        depends_on=string_list(raw.get("depends_on")),
        completed=coerce_bool(raw.get("completed"), default=False),
        completed_at=parse_iso(raw.get("completed_at"), default=None),
    )
