"""Draft step generation.

Reads raw events (and the resolved elements they reference) from a session
repository, collapses them into a reduced set of user-meaningful actions,
and writes the result into the ``draft_steps`` table via
:meth:`SessionRepository.replace_steps`.

Regeneration policy: manually-edited steps (``manual_edit=True``) are kept
verbatim when their source event set hasn't changed. Only untouched or
source-changed steps are rewritten. This preserves examiner edits across
re-runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from inscription.model import DraftStep, EventKind
from inscription.steps._dedup import (
    CLICK_DEDUP_WINDOW_S,
    WINDOW_FOCUS_COALESCE_S,
    ClickDedup,
    KeyPressDedup,
    ScrollDedup,
    click_key,
)

if TYPE_CHECKING:
    from inscription.model import RawEvent, ResolvedElement
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


#: Resolver-confidence threshold above which we trust the UIA name + type.
HIGH_CONFIDENCE = 0.6

#: Milestone keys whose presses are dropped entirely (mirrors the live
#: generator's set). Backspace/Delete are corrective input, not
#: procedural content; the raw events remain on disk for the AI rewrite
#: but never appear as their own step.
_DROP_KEY_NAMES = frozenset({"backspace", "delete"})

#: Re-exported for callers that import the dedup window through this module.
__all__ = [
    "CLICK_DEDUP_WINDOW_S",
    "WINDOW_FOCUS_COALESCE_S",
    "render_repeat_key_press",
    "render_step_action",
]


@dataclass(frozen=True, slots=True)
class _Action:
    """An intermediate, pre-text representation of a draft step."""

    kind: EventKind
    source_event_ids: tuple[int, ...]
    screenshot_id: int | None
    action: str
    result: str = ""


def _render_click(event: RawEvent, resolved: ResolvedElement | None) -> str:
    verb = "Double-click" if event.kind is EventKind.DOUBLE_CLICK else "Click"
    # UIA "Text" elements are static labels, not interactive controls.
    # Clicks that resolve to them are almost always positional accidents
    # (e.g. clicking near a label or on the recording tool's own UI).
    # Fall through to the lower-confidence window-title path instead.
    if (
        resolved
        and resolved.confidence >= HIGH_CONFIDENCE
        and resolved.name
        and resolved.control_type != "Text"
    ):
        control = resolved.control_type or "item"
        in_window = _in_window_clause(event, resolved)
        return f"{verb} the {resolved.name!r} {control}{in_window}.".replace("''", "'")
    if event.window_title:
        return f"{verb} in the {event.window_title} window."
    return f"{verb} the mouse."


def _in_window_clause(event: RawEvent, resolved: ResolvedElement) -> str:
    """Return the `` in <window>`` suffix, or `` `` when it would mislead.

    The suffix is dropped when the resolved element's owning process
    differs from the foreground process at click time. That catches the
    common taskbar / Start menu / Alt-Tab case: UIA resolves the shell
    element correctly, but the foreground window is whatever app the
    user was previously in — gluing the two together produces phrases
    like "Click the 'Python' Button in World of Warcraft."
    """
    if not event.window_title:
        return ""
    owner = resolved.owner_process_name
    if owner and event.process_name and owner != event.process_name:
        return ""
    return f" in {event.window_title}"


def _render_key_press(event: RawEvent) -> str:
    key = (event.key or "a key").replace("_", " ")
    if event.window_title:
        return f"Press {key.capitalize()} in {event.window_title}."
    return f"Press {key.capitalize()}."


def render_repeat_key_press(event: RawEvent, *, count: int) -> str:
    """Render a key-press step that has merged ``count`` repeats.

    Used by both step generators when the keypress dedup machine signals
    a merge: ``Press Enter 3 times in Notepad`` rather than three
    separate "Press Enter" steps. ``count == 1`` falls back to the
    single-press wording so the same renderer handles both cases.
    """
    if count <= 1:
        return _render_key_press(event)
    key = (event.key or "a key").replace("_", " ")
    if event.window_title:
        return f"Press {key.capitalize()} {count} times in {event.window_title}."
    return f"Press {key.capitalize()} {count} times."


def _render_window_focus(event: RawEvent) -> str:
    if event.window_title:
        return f"Switch to the {event.window_title} window."
    return "Switch windows."


def _render_marker(event: RawEvent) -> str:
    return event.text or "Marker placed."


def _render_scroll(event: RawEvent) -> str:
    descriptor = event.text or "scroll"
    if event.window_title:
        return f"Scroll {descriptor} in {event.window_title}."
    return f"Scroll {descriptor}."


def render_step_action(
    event: RawEvent,
    resolved: ResolvedElement | None,
) -> str:
    """Build a single-step Action string from an event + its resolved element.

    The wording scales with resolver confidence:

    - High-confidence (UIA resolved): control name + type.
    - Low-confidence (foreground only): window title only.
    - No resolution: generic ("Click the mouse").
    """
    if event.kind is EventKind.CLICK or event.kind is EventKind.DOUBLE_CLICK:
        return _render_click(event, resolved)
    if event.kind is EventKind.KEY_PRESS:
        return _render_key_press(event)
    if event.kind is EventKind.WINDOW_FOCUS:
        return _render_window_focus(event)
    if event.kind is EventKind.MARKER:
        return _render_marker(event)
    if event.kind is EventKind.SCROLL:
        return _render_scroll(event)
    return f"{event.kind.value}."


class StepGenerator:
    """Build :class:`DraftStep` rows from a session's raw event stream."""

    def __init__(self, repository: SessionRepository) -> None:
        self._repo = repository

    # -------------------------------------------------------------- API

    def regenerate(self) -> list[DraftStep]:
        """Replace the session's draft steps with freshly-generated ones.

        Manual-step preservation has two tiers: a manual step whose
        exact source set is reproduced by clustering keeps its text and
        flags in place; one that is NOT reproduced (a split half, a
        suggestion-drafted step with no source events, or any step the
        clustering groups differently) is re-inserted at its timeline
        position with its event ids removed from the generated steps.
        Regenerate fires automatically on recording stop -- it must
        never destroy examiner work.
        """
        existing = self._repo.list_steps(include_suppressed=True)
        manual_by_sources = {step.source_event_ids: step for step in existing if step.manual_edit}

        # Per-event sticky flags (same policy as StepRewriter): a step
        # flag the examiner set follows each underlying event through
        # whatever regrouping this pass produces.
        flag_by_event: dict[int, tuple[bool, bool]] = {}
        for step in existing:
            if not (step.evidentiary or step.suppressed):
                continue
            for eid in step.source_event_ids:
                prev = flag_by_event.get(eid, (False, False))
                flag_by_event[eid] = (
                    prev[0] or step.evidentiary,
                    prev[1] or step.suppressed,
                )

        events = self._repo.list_events()
        actions = self._reduce_to_actions(events)

        matched_manual: set[tuple[int, ...]] = set()
        new_steps: list[DraftStep] = []
        for action in actions:
            preserved = manual_by_sources.get(action.source_event_ids)
            if preserved is not None:
                matched_manual.add(action.source_event_ids)
                new_steps.append(
                    DraftStep(
                        id=None,
                        sequence=0,  # reassigned by replace_steps
                        action=preserved.action,
                        result=preserved.result,
                        source_event_ids=action.source_event_ids,
                        screenshot_id=preserved.screenshot_id or action.screenshot_id,
                        manual_edit=True,
                        suppressed=preserved.suppressed,
                        evidentiary=preserved.evidentiary,
                    )
                )
                continue
            evidentiary = any(
                flag_by_event.get(eid, (False, False))[0]
                for eid in action.source_event_ids
            )
            suppressed = any(
                flag_by_event.get(eid, (False, False))[1]
                for eid in action.source_event_ids
            )
            new_steps.append(
                DraftStep(
                    id=None,
                    sequence=0,
                    action=action.action,
                    result=action.result,
                    source_event_ids=action.source_event_ids,
                    screenshot_id=action.screenshot_id,
                    manual_edit=False,
                    suppressed=suppressed,
                    evidentiary=evidentiary,
                )
            )

        new_steps = _merge_unmatched_manual_steps(
            generated=new_steps,
            existing=existing,
            matched_manual=matched_manual,
        )

        saved = self._repo.replace_steps(new_steps)
        self._repo.flush_manifest()
        return saved

    # -------------------------------------------------------- reduction

    def _reduce_to_actions(self, events: list[RawEvent]) -> list[_Action]:
        actions: list[_Action] = []
        click_dedup = ClickDedup()
        key_dedup = KeyPressDedup()
        scroll_dedup = ScrollDedup()

        for i, event in enumerate(events):
            if event.kind is EventKind.WINDOW_FOCUS and self._window_focus_is_noise(events, i):
                # A suppressed focus is still a window boundary: reset
                # the dedup machines (like every other drop path) so
                # events straddling it never merge across the switch.
                click_dedup.reset()
                key_dedup.reset()
                scroll_dedup.reset()
                continue

            # Drop corrective key presses (Backspace, Delete) — same
            # rule as the live generator. Reset all dedup state so a
            # post-drop event doesn't accidentally merge into a step
            # that no longer exists in the action list.
            if (
                event.kind is EventKind.KEY_PRESS
                and event.key
                and event.key.lower() in _DROP_KEY_NAMES
            ):
                click_dedup.reset()
                key_dedup.reset()
                scroll_dedup.reset()
                continue

            resolved = self._resolve(event.resolved_element_id)

            # Drop clicks that resolved to UIA "Text" labels — they are
            # positional accidents, not intentional interactions.
            if (
                event.kind in {EventKind.CLICK, EventKind.DOUBLE_CLICK}
                and resolved is not None
                and resolved.control_type == "Text"
                and resolved.name
            ):
                click_dedup.reset()
                key_dedup.reset()
                scroll_dedup.reset()
                continue

            ts = event.occurred_at.timestamp()

            if click_dedup.observe(
                kind=event.kind,
                key=click_key(
                    name=resolved.name if resolved else None,
                    control_type=resolved.control_type if resolved else None,
                    window_title=event.window_title,
                    x=event.x,
                    y=event.y,
                ),
                ts=ts,
            ) and actions:
                last = actions[-1]
                # A DOUBLE_CLICK merging into its own preceding CLICK is
                # one physical gesture -- the step must say what the
                # gesture WAS, so re-render with the double-click verb.
                if event.kind is EventKind.DOUBLE_CLICK:
                    merged_action = render_step_action(event, resolved)
                    merged_kind = EventKind.DOUBLE_CLICK
                else:
                    merged_action = last.action
                    merged_kind = last.kind
                actions[-1] = _Action(
                    kind=merged_kind,
                    source_event_ids=(*last.source_event_ids, event.id or 0),
                    screenshot_id=last.screenshot_id or event.screenshot_id,
                    action=merged_action,
                    result=last.result,
                )
                continue

            merge_key, key_count = key_dedup.observe(
                kind=event.kind, key=(event.key, event.window_title), ts=ts
            )
            if merge_key and actions:
                last = actions[-1]
                actions[-1] = _Action(
                    kind=last.kind,
                    source_event_ids=(*last.source_event_ids, event.id or 0),
                    screenshot_id=last.screenshot_id or event.screenshot_id,
                    action=render_repeat_key_press(event, count=key_count),
                    result=last.result,
                )
                continue

            merge_scroll, _ = scroll_dedup.observe(
                kind=event.kind, key=(event.text, event.window_title), ts=ts
            )
            if merge_scroll and actions:
                last = actions[-1]
                actions[-1] = _Action(
                    kind=last.kind,
                    source_event_ids=(*last.source_event_ids, event.id or 0),
                    screenshot_id=last.screenshot_id or event.screenshot_id,
                    action=last.action,
                    result=last.result,
                )
                continue

            actions.append(
                _Action(
                    kind=event.kind,
                    source_event_ids=(event.id or 0,),
                    screenshot_id=event.screenshot_id,
                    action=render_step_action(event, resolved),
                )
            )
        return actions

    def _resolve(self, element_id: int | None) -> ResolvedElement | None:
        if element_id is None:
            return None
        return self._repo.get_resolved_element(element_id)

    @staticmethod
    def _window_focus_is_noise(events: list[RawEvent], index: int) -> bool:
        """Return True if this window-focus event is caused by a nearby click."""
        event = events[index]
        focus_ts = event.occurred_at.timestamp()
        for other in events[index + 1 : index + 4]:
            if other.kind not in {EventKind.CLICK, EventKind.DOUBLE_CLICK}:
                continue
            if (other.occurred_at.timestamp() - focus_ts) <= WINDOW_FOCUS_COALESCE_S:
                return True
            break
        return False


def _merge_unmatched_manual_steps(
    *,
    generated: list[DraftStep],
    existing: list[DraftStep],
    matched_manual: set[tuple[int, ...]],
) -> list[DraftStep]:
    """Re-insert manual steps the clustering didn't reproduce.

    Their event ids are removed from generated steps first (an event
    must never be referenced by two steps); generated steps left with
    no events are dropped. Timeline position comes from the smallest
    source event id; steps with no source events (suggestion drafts)
    sort by their original sequence at the end.
    """
    unmatched = [
        s
        for s in existing
        if s.manual_edit and s.source_event_ids not in matched_manual
    ]
    if not unmatched:
        return generated

    claimed = {eid for s in unmatched for eid in s.source_event_ids}
    kept: list[DraftStep] = []
    for step in generated:
        if step.manual_edit:
            kept.append(step)
            continue
        remaining = tuple(e for e in step.source_event_ids if e not in claimed)
        if not remaining:
            continue  # fully claimed by a manual step
        trimmed = step
        if remaining != step.source_event_ids:
            trimmed = DraftStep(
                id=None,
                sequence=0,
                action=step.action,
                result=step.result,
                source_event_ids=remaining,
                screenshot_id=step.screenshot_id,
                manual_edit=False,
                suppressed=step.suppressed,
                evidentiary=step.evidentiary,
            )
        kept.append(trimmed)

    reinserted = [
        DraftStep(
            id=None,
            sequence=0,
            action=s.action,
            result=s.result,
            source_event_ids=s.source_event_ids,
            screenshot_id=s.screenshot_id,
            manual_edit=True,
            suppressed=s.suppressed,
            evidentiary=s.evidentiary,
        )
        for s in unmatched
    ]
    # Original sequence rank for sourceless steps, so suggestion
    # drafts keep their relative order after event-anchored steps.
    rank_by_action = {
        (s.action, s.result): s.sequence for s in existing if not s.source_event_ids
    }

    def _position(step: DraftStep) -> tuple[float, int]:
        if step.source_event_ids:
            return (float(min(step.source_event_ids)), 0)
        return (float("inf"), rank_by_action.get((step.action, step.result), 0))

    merged = kept + reinserted
    merged.sort(key=_position)
    return merged


def generate_steps(repository: SessionRepository) -> list[DraftStep]:
    """Convenience wrapper: regenerate a session's draft steps."""
    return StepGenerator(repository).regenerate()
