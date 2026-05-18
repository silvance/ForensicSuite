"""Read / write the ``case.json`` artifact.

CaseForge owns this file inside the case directory. Inscription and
the future report builder read it for admin metadata; CaseGuide reads
the ``scope`` block.

The on-disk format is forward-compatible: unknown fields on read are
preserved verbatim and re-emitted on save, so a newer version of
CaseForge that adds a field can write it without older versions
clobbering it. Schema bumps run a forward-only migration.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import shutil
from pathlib import Path

from caseforge.model import (
    CASE_SCHEMA_VERSION,
    Case,
    CaseSummary,
    CustodyRecord,
    ExaminerIdentity,
    ExamScope,
    coerce_bool,
    coerce_int,
    parse_iso,
    parse_optional_iso,
    string_list,
    utcnow,
)

logger = logging.getLogger(__name__)

CASE_FILENAME = "case.json"
ARCHIVE_DIRNAME = "_archive"

#: Slug character whitelist; everything else collapses to a single dash.
_SLUG_INVALID = re.compile(r"[^a-zA-Z0-9._-]+")

#: Windows reserved device names. A directory created with one of these
#: names (case-insensitive, with or without an extension) is unusable
#: on Windows — file creation inside it returns ERROR_INVALID_NAME and
#: the case looks broken to the examiner. We suffix the slug to dodge
#: the collision while keeping the name recognisable.
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con", "prn", "aux", "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

#: Hard cap on the case.json size we'll load. Files in the wild sit in
#: the low-tens-of-KB range; refuse anything larger to bound the memory
#: + parse-time exposure for corrupt or hostile files.
_MAX_CASE_BYTES = 5 * 1024 * 1024


class StorageError(Exception):
    """Wrapper around any case.json read/write failure."""


class CaseAlreadyExistsError(StorageError):
    """Raised when create_case is asked to land in an existing directory."""


class ArchiveError(StorageError):
    """Raised when archive_case can't move the directory cleanly."""


class DeleteError(StorageError):
    """Raised when delete_case can't recursively remove the directory."""


def slugify(name: str) -> str:
    """Build a filesystem-safe directory name from a free-form case name.

    Beyond character whitelisting, reserved Windows device names (CON,
    PRN, LPT1, …) get a ``-case`` suffix so a portable case folder
    works the same on every platform we ship for.
    """
    text = _SLUG_INVALID.sub("-", name).strip("-._")
    slug = text or "case"
    base = slug.split(".", 1)[0].lower()
    if base in _WINDOWS_RESERVED_NAMES:
        slug = f"{slug}-case"
    return slug


def case_path_for(workspace_root: Path, name: str) -> Path:
    """Compute (without creating) the directory a new case would land in."""
    return workspace_root / slugify(name)


def create_case(
    *,
    workspace_root: Path,
    case: Case,
    overwrite: bool = False,
) -> Path:
    """Create the case directory and write ``case.json``.

    Returns the absolute case directory path. Raises
    :class:`CaseAlreadyExistsError` if the target exists and
    ``overwrite`` is False.
    """
    target = case_path_for(workspace_root, case.name)
    if target.exists() and not overwrite:
        msg = f"Case directory already exists: {target}"
        raise CaseAlreadyExistsError(msg)
    target.mkdir(parents=True, exist_ok=overwrite)
    write_case(target, case)
    return target


def write_case(case_dir: Path, case: Case) -> None:
    """Serialise ``case`` to ``<case_dir>/case.json``."""
    case_dir.mkdir(parents=True, exist_ok=True)
    payload = _to_json(case)
    target = case_dir / CASE_FILENAME
    tmp = target.with_suffix(".json.tmp")
    # Drop any leftover .tmp from a prior crash before we write our own.
    tmp.unlink(missing_ok=True)
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_case(case_dir: Path) -> Case:
    """Load ``case.json`` from ``case_dir``. Migrates older schemas in place."""
    target = case_dir / CASE_FILENAME
    if not target.exists():
        msg = f"No case.json at {target}"
        raise StorageError(msg)
    try:
        size = target.stat().st_size
    except OSError as exc:
        msg = f"Could not stat {target}: {exc}"
        raise StorageError(msg) from exc
    if size > _MAX_CASE_BYTES:
        msg = f"{target} is {size} bytes; refusing to load (cap is {_MAX_CASE_BYTES})."
        raise StorageError(msg)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"Could not parse {target}: {exc}"
        raise StorageError(msg) from exc
    return _from_json(raw)


def list_cases(workspace_root: Path) -> list[CaseSummary]:
    """Enumerate every active case directory under ``workspace_root``.

    Skips directories that don't contain a ``case.json`` so partially
    initialised folders don't pollute the browser. The reserved
    ``_archive/`` subdirectory is hidden — archived cases live there
    and don't surface in the welcome list.
    """
    if not workspace_root.exists():
        return []
    summaries: list[CaseSummary] = []
    for child in sorted(workspace_root.iterdir()):
        if not child.is_dir() or child.name == ARCHIVE_DIRNAME:
            continue
        path = child / CASE_FILENAME
        if not path.exists():
            continue
        try:
            case = read_case(child)
        except StorageError as exc:
            logger.warning("Skipping malformed case at %s: %s", child, exc)
            continue
        summaries.append(_summary_for(case=case, path=child))
    return summaries


def archive_case(case_dir: Path) -> Path:
    """Move ``case_dir`` to ``<workspace>/_archive/<slug>``.

    The archive directory is created on demand. Returns the new path.
    Existing archive entries with the same slug get a numeric suffix
    so re-archiving doesn't clobber the previous copy — we never
    silently drop an examiner's work.
    """
    if not case_dir.exists() or not case_dir.is_dir():
        msg = f"archive_case: not a directory: {case_dir}"
        raise ArchiveError(msg)
    workspace_root = case_dir.parent
    archive_root = workspace_root / ARCHIVE_DIRNAME
    archive_root.mkdir(parents=True, exist_ok=True)
    target = _unique_archive_target(archive_root, case_dir.name)
    try:
        case_dir.rename(target)
    except OSError as exc:
        msg = f"archive_case: could not move {case_dir} -> {target}: {exc}"
        raise ArchiveError(msg) from exc
    logger.info("Archived %s -> %s", case_dir, target)
    return target


def delete_case(case_dir: Path) -> None:
    """Recursively remove ``case_dir``.

    Defensive: refuses to delete a directory that doesn't look like a
    case (no ``case.json``) so a path mix-up doesn't take out the
    workspace root.
    """
    if not case_dir.exists():
        return
    if not case_dir.is_dir() or not (case_dir / CASE_FILENAME).exists():
        msg = f"delete_case: refusing to remove non-case path {case_dir}"
        raise DeleteError(msg)
    try:
        shutil.rmtree(case_dir)
    except OSError as exc:
        msg = f"delete_case: could not remove {case_dir}: {exc}"
        raise DeleteError(msg) from exc
    logger.info("Deleted %s", case_dir)


def _unique_archive_target(archive_root: Path, slug: str) -> Path:
    """Pick a destination that doesn't collide with prior archive entries."""
    candidate = archive_root / slug
    if not candidate.exists():
        return candidate
    counter = 2
    while True:
        candidate = archive_root / f"{slug}-{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


def case_summary_at(path: Path) -> CaseSummary:
    """Build a :class:`CaseSummary` for an explicit case directory.

    Used by the recents list which stores arbitrary paths rather than
    relying on workspace-root scanning.
    """
    return _summary_for(case=read_case(path), path=path)


# ------------------------------------------------------------ JSON shape


#: Top-level keys this build understands. Anything else found on read
#: is parked on ``Case.extras`` so a round-trip through an older build
#: doesn't strip fields a newer build wrote -- the forward-compat
#: contract this module's docstring promises.
_KNOWN_TOPLEVEL_KEYS = frozenset({
    "schema_version",
    "caseforge_version",
    "name",
    "case_reference",
    "created_at",
    "updated_at",
    "examiner",
    "scope",
    "custody",
})


def _to_json(case: Case) -> dict[str, object]:
    # Spread ``case.extras`` first so the canonical keys we emit below
    # win on conflict -- an older build still re-asserts its own
    # contract for every field it understands.
    payload: dict[str, object] = dict(case.extras)
    payload.update({
        "schema_version": case.schema_version,
        "caseforge_version": case.caseforge_version,
        "name": case.name,
        "case_reference": case.case_reference,
        "created_at": case.created_at.isoformat(),
        "updated_at": case.updated_at.isoformat(),
        "examiner": {
            "name": case.examiner.name,
            "organisation": case.examiner.organisation,
            "badge_id": case.examiner.badge_id,
        },
        "scope": {
            "exam_type": case.scope.exam_type,
            "device_classes": list(case.scope.device_classes),
            "evidence_items": list(case.scope.evidence_items),
            "agencies": list(case.scope.agencies),
            "primary_tool": case.scope.primary_tool,
            "summary": case.scope.summary,
            "notes": case.scope.notes,
            "use_caseguide": case.scope.use_caseguide,
        },
        "custody": {
            "received_at": (
                case.custody.received_at.isoformat()
                if case.custody.received_at is not None
                else None
            ),
            "received_from": case.custody.received_from,
            "delivery_method": case.custody.delivery_method,
            "evidence_bag_ids": list(case.custody.evidence_bag_ids),
            "seal_intact": case.custody.seal_intact,
            "notes": case.custody.notes,
        },
    })
    return payload


def _from_json(raw: dict[str, object]) -> Case:
    schema_version = coerce_int(raw.get("schema_version", 1), default=1)
    raw = _migrate(raw, schema_version)
    examiner_raw = _sub(raw, "examiner")
    scope_raw = _sub(raw, "scope")
    custody_raw = _sub(raw, "custody")

    return Case(
        name=str(raw.get("name", "")),
        case_reference=str(raw.get("case_reference", "")),
        created_at=parse_iso(raw.get("created_at")),
        updated_at=parse_iso(raw.get("updated_at")),
        examiner=ExaminerIdentity(
            name=str(examiner_raw.get("name", "")),
            organisation=str(examiner_raw.get("organisation", "")),
            badge_id=str(examiner_raw.get("badge_id", "")),
        ),
        scope=ExamScope(
            exam_type=str(scope_raw.get("exam_type", "")),
            device_classes=string_list(scope_raw.get("device_classes")),
            evidence_items=string_list(scope_raw.get("evidence_items")),
            agencies=string_list(scope_raw.get("agencies")),
            primary_tool=str(scope_raw.get("primary_tool", "")),
            summary=str(scope_raw.get("summary", "")),
            notes=str(scope_raw.get("notes", "")),
            use_caseguide=coerce_bool(scope_raw.get("use_caseguide"), default=False),
        ),
        custody=CustodyRecord(
            received_at=parse_optional_iso(custody_raw.get("received_at")),
            received_from=str(custody_raw.get("received_from", "")),
            delivery_method=str(custody_raw.get("delivery_method", "")),
            evidence_bag_ids=string_list(custody_raw.get("evidence_bag_ids")),
            seal_intact=_coerce_optional_bool(custody_raw.get("seal_intact")),
            notes=str(custody_raw.get("notes", "")),
        ),
        schema_version=CASE_SCHEMA_VERSION,
        caseforge_version=str(raw.get("caseforge_version", "")),
        # Snapshot unknown top-level keys so a write back to disk
        # round-trips them verbatim instead of silently stripping
        # fields a newer CaseForge added.
        extras={k: v for k, v in raw.items() if k not in _KNOWN_TOPLEVEL_KEYS},
    )


def _migrate(raw: dict[str, object], from_version: int) -> dict[str, object]:
    """Forward-only schema migration.

    Both jumps so far (v1 -> v2 added ``custody``, v2 -> v3 added
    ``scope.primary_tool``) are additive — :func:`_from_json` falls back
    to safe defaults when the new fields are missing — but we still
    surface "newer than this build" explicitly so the case browser
    doesn't silently pretend a future schema is fine.
    """
    if from_version > CASE_SCHEMA_VERSION:
        msg = (
            f"case.json schema version {from_version} is newer than this "
            f"CaseForge build supports (max {CASE_SCHEMA_VERSION})."
        )
        raise StorageError(msg)
    return raw


def _summary_for(*, case: Case, path: Path) -> CaseSummary:
    return CaseSummary(
        name=case.name,
        case_reference=case.case_reference,
        created_at=case.created_at,
        updated_at=case.updated_at,
        examiner_name=case.examiner.name,
        path=str(path.resolve()),
    )


def _sub(raw: dict[str, object], key: str) -> dict[str, object]:
    """Return ``raw[key]`` if it is a dict, else an empty dict."""
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _coerce_optional_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "yes", "1"}:
            return True
        if value.strip().lower() in {"false", "no", "0"}:
            return False
    return None


def touch_updated_at(case: Case) -> Case:
    """Return ``case`` with ``updated_at`` bumped to now."""
    return dataclasses.replace(case, updated_at=utcnow())
