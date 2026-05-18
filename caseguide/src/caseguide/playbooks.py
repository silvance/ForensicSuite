"""Procedural playbooks: structured forensic-exam steps with tool variants.

A *playbook* is a JSON file describing one logical step in a forensic
exam — verify the image hash, run the AXIOM CI processing workflow,
walk through a Windows MRU artifact deep-dive — alongside the
match criteria that say which cases it applies to.

Each playbook step has a tool-agnostic body (action, rationale,
category) plus a ``tool_variants`` dict that overrides the action
wording with tool-specific button paths (AXIOM, X-Ways, FTK, …). The
case's ``scope.primary_tool`` decides which variant gets rendered.

Loader resolution order (later wins):

1. Built-in playbooks shipped inside the package
   (``src/caseguide/playbook_data/*.json``).
2. User overlays at ``%LOCALAPPDATA%\\CaseGuide\\playbooks\\*.json``
   — examiners can drop their own JSON files there to extend or
   override the built-in set without modifying the install.

The :class:`PlaybookMatcher` selects which playbooks apply to a given
:class:`caseguide.case_reader.CaseScope`. It's a deliberately simple
matcher (case-insensitive substring across lists) so the rules read
clearly in the JSON; richer matching can wait until the LLM
augmentation pass shows it's needed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from caseguide.model import (
    PRIORITY_CHOICES,
    PRIORITY_RECOMMENDED,
)
from caseguide.paths import BUILTIN_PLAYBOOKS_DIR, USER_PLAYBOOKS_DIR

if TYPE_CHECKING:
    from caseguide.case_reader import CaseScope

logger = logging.getLogger(__name__)

#: Wildcard token in match-criteria lists; matches any value.
WILDCARD = "*"

#: Hard cap on per-playbook list lengths. The matcher walks every entry
#: against scope text on every match call, so a user-supplied playbook
#: with thousands of entries would slow the suggestions panel for
#: every regenerate. The cap is well above any sensible authoring
#: limit but stops a malformed file from DOSing the matcher.
_MAX_RULE_ENTRIES = 200


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolVariant:
    """Tool-specific override for a playbook step's action wording."""

    action: str = ""
    ui_path: str = ""
    notes: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class AppliesTo:
    """Match criteria for a playbook.

    There are three classes of constraint, each with different
    semantics on under-specified scopes:

    - **Soft fields** (``exam_types``, ``device_classes``,
      ``evidence_items``): if the rule is set but the corresponding
      scope field is empty, the constraint is *inconclusive* and
      passes. This favours recall when CaseForge hasn't been told
      every detail yet — the examiner can dismiss false positives
      via the suggestions panel's complete/remove controls.

    - **Strict field** (``primary_tools``): if the rule is set and
      the scope's ``primary_tool`` is empty, the constraint **fails**.
      This keeps tool-specific playbooks (AXIOM, X-Ways, …) from
      leaking into cases that haven't picked a tool, where their
      UI paths would be wrong.

    - **Keywords**: a short-circuit OR. If any keyword (case-insensitive
      substring) appears anywhere in the joined scope text, the
      playbook fires regardless of the other constraints. Lets
      universal steps (hash verification, MRU walk-throughs) catch
      cases where the structured fields are blank but the exam
      type or device class strings hint at relevance.

    Empty rule lists still match anything — universal steps need no
    criteria at all. The wildcard ``*`` keeps its old meaning inside
    a list.
    """

    exam_types: list[str] = field(default_factory=list)
    device_classes: list[str] = field(default_factory=list)
    evidence_items: list[str] = field(default_factory=list)
    primary_tools: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True, kw_only=True)
class Playbook:
    """One procedural step in the playbook library."""

    id: str
    title: str
    action: str
    rationale: str = ""
    category: str = ""
    priority: str = PRIORITY_RECOMMENDED
    expected_result: str = ""
    references: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    tool_variants: dict[str, ToolVariant] = field(default_factory=dict)
    applies_to: AppliesTo = field(default_factory=AppliesTo)
    source_path: str = ""

    def variant_for(self, tool: str) -> ToolVariant | None:
        """Return the variant override for ``tool``, or None if none defined."""
        if not tool:
            return None
        return self.tool_variants.get(tool)

    def rendered_action(self, tool: str) -> str:
        """Action wording with the matching tool variant applied (if any)."""
        variant = self.variant_for(tool)
        if variant is not None and variant.action:
            return variant.action
        return self.action


# ---------------------------------------------------------------- loading


def load_playbooks(
    *,
    builtin_dir: Path | None = None,
    user_dir: Path | None = None,
) -> list[Playbook]:
    """Load every playbook JSON from the built-in and user overlay dirs.

    User overlays with the same ``id`` as a built-in **replace** the
    built-in entirely — examiners can ship local refinements without
    touching the package.
    """
    builtin = builtin_dir or BUILTIN_PLAYBOOKS_DIR
    user = user_dir or USER_PLAYBOOKS_DIR

    by_id: dict[str, Playbook] = {}
    for source in (builtin, user):
        for path in _iter_json(source):
            parsed = _parse_playbook(path)
            if parsed is None:
                continue
            by_id[parsed.id] = parsed
    # Stable ordering: alphabetical by id so callers see the same list
    # across runs without having to deal with dict-order shenanigans.
    return sorted(by_id.values(), key=lambda p: p.id)


def _iter_json(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        return []
    return sorted(directory.glob("*.json"))


def _parse_playbook(path: Path) -> Playbook | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Skipping malformed playbook %s: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Playbook %s top-level is not an object", path)
        return None

    pb_id = str(raw.get("id") or path.stem)
    title = str(raw.get("title") or pb_id)
    action = str(raw.get("action", "")).strip()
    if not action:
        logger.warning("Playbook %s has no action; skipping", path)
        return None

    priority = str(raw.get("priority", PRIORITY_RECOMMENDED))
    if priority not in PRIORITY_CHOICES:
        priority = PRIORITY_RECOMMENDED

    return Playbook(
        id=pb_id,
        title=title,
        action=action,
        rationale=str(raw.get("rationale", "")),
        category=str(raw.get("category", "")),
        priority=priority,
        expected_result=str(raw.get("expected_result", "")),
        references=_string_list(raw.get("references")),
        depends_on=_string_list(raw.get("depends_on")),
        tool_variants=_parse_variants(raw.get("tool_variants")),
        applies_to=_parse_applies_to(raw.get("applies_to")),
        source_path=str(path),
    )


def _parse_variants(raw: object) -> dict[str, ToolVariant]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, ToolVariant] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = ToolVariant(
            action=str(value.get("action", "")),
            ui_path=str(value.get("ui_path", "")),
            notes=str(value.get("notes", "")),
        )
    return out


def _parse_applies_to(raw: object) -> AppliesTo:
    if not isinstance(raw, dict):
        return AppliesTo()
    return AppliesTo(
        exam_types=_string_list(raw.get("exam_types")),
        device_classes=_string_list(raw.get("device_classes")),
        evidence_items=_string_list(raw.get("evidence_items")),
        primary_tools=_string_list(raw.get("primary_tools")),
        keywords=_string_list(raw.get("keywords")),
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [str(item) for item in value if item is not None]
    return cleaned[:_MAX_RULE_ENTRIES]


# --------------------------------------------------------------- matching


class PlaybookMatcher:
    """Selects the playbooks that apply to a given case scope."""

    def __init__(self, playbooks: list[Playbook]) -> None:
        self._playbooks = playbooks

    def match(self, scope: CaseScope) -> list[Playbook]:
        """Return playbooks whose ``applies_to`` overlaps ``scope``."""
        matched = [p for p in self._playbooks if _matches(p.applies_to, scope)]
        # Stable order by priority then id so the LLM augmentation
        # pass sees important steps first.
        priority_order = {p: i for i, p in enumerate(PRIORITY_CHOICES)}
        matched.sort(
            key=lambda p: (priority_order.get(p.priority, len(PRIORITY_CHOICES)), p.id)
        )
        return matched


def _matches(criteria: AppliesTo, scope: CaseScope) -> bool:
    if criteria.keywords and _keyword_present(criteria.keywords, scope):
        return True
    return (
        _soft_match(criteria.exam_types, [scope.exam_type])
        and _soft_match(criteria.device_classes, scope.device_classes)
        and _soft_match(criteria.evidence_items, scope.evidence_items)
        and _strict_match(criteria.primary_tools, [scope.primary_tool])
    )


def _soft_match(rule: list[str], values: list[str]) -> bool:
    """Inconclusive scope counts as a match.

    Used for the descriptive scope fields (exam_types, device_classes,
    evidence_items): when the examiner hasn't filled them in we'd
    rather show a possibly-irrelevant playbook than hide a relevant
    one. The completion + remove controls in the suggestions panel
    make false positives cheap.
    """
    if not rule or WILDCARD in rule:
        return True
    needles = _normalised(rule)
    if not needles:
        return True
    haystack = _normalised(values)
    if not haystack:
        return True  # Inconclusive — pass.
    return _any_overlap(needles, haystack)


def _strict_match(rule: list[str], values: list[str]) -> bool:
    """Empty scope fails the rule.

    Reserved for primary_tools, where firing an AXIOM playbook on a
    case that hasn't picked a tool would teach the examiner the
    wrong UI paths.
    """
    if not rule or WILDCARD in rule:
        return True
    needles = _normalised(rule)
    if not needles:
        return True
    haystack = _normalised(values)
    if not haystack:
        return False
    return _any_overlap(needles, haystack)


def _keyword_present(keywords: list[str], scope: CaseScope) -> bool:
    """True if any keyword appears as a substring in the scope text.

    Joins every scope field into one lowercased haystack so
    ``"image"`` matches ``exam_type="Forensic image acquisition"``
    even when ``evidence_items`` is empty.
    """
    needles = _normalised(keywords)
    if not needles:
        return False
    parts = [scope.exam_type, scope.primary_tool, *scope.device_classes, *scope.evidence_items]
    haystack = " ".join(p for p in parts if p).lower()
    if not haystack:
        return False
    return any(needle in haystack for needle in needles)


def _normalised(values: list[str]) -> list[str]:
    return [v.strip().lower() for v in values if v and v.strip()]


def _any_overlap(needles: list[str], haystack: list[str]) -> bool:
    # Forward containment only: a rule entry like "iphone" should match
    # a scope value of "iphone-13", but a partial scope value like "i"
    # must NOT match a rule of "ios". The reverse direction (``stack in
    # needle``) would let any short scope fragment fire wildly
    # unrelated playbooks, eroding trust in the checklist.
    return any(needle in stack for needle in needles for stack in haystack)
