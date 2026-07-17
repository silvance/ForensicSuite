"""Live step generation while a recording is in progress.

The batch :class:`StepGenerator` runs after a recording finishes and
rewrites every step from scratch. That's a great final pass but leaves
the examiner looking at an empty notes panel for the duration of the
exam. This module fills that gap: a :class:`CaptureSink` that builds
draft steps incrementally as events stream in.

Noise filtering happens here too, deterministically, so the live notes
panel shows something close to the post-recording cleanup instead of
one row per raw event:

- Rapid repeat clicks on the same UIA element merge (``ClickDedup``).
- Repeated milestone-key presses merge with a count: ``Press Backspace
  9 times`` instead of nine separate "Press Backspace" steps
  (``KeyPressDedup``).
- Consecutive scrolls in the same window merge (``ScrollDedup``).
- Clicks that resolve to UIA "Text" controls (static labels) never
  produce a step — they are positional accidents in nearly every case.
  The raw event is still preserved by ``SessionSink`` so the AI rewrite
  has full context.

What this module does NOT do is semantic intent merging — collapsing
``File → Save As → name → Enter`` into a single ``Save the file`` step
needs lookahead and language understanding, both of which belong in the
post-recording :class:`StepGenerator` and in the optional LLM rewrite.
Manual edits made mid-recording survive both passes because the batch
generator preserves any step whose ``source_event_ids`` matches an
existing ``manual_edit`` row.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from inscription.model import DraftStep, EventKind, RawEvent
from inscription.steps._dedup import ClickDedup, KeyPressDedup, ScrollDedup, click_key
from inscription.steps.generator import render_repeat_key_press, render_step_action

if TYPE_CHECKING:
    from collections.abc import Callable

    from inscription.capture.engine import EnrichedEvent
    from inscription.model import ResolvedElement
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)

#: UIA control types whose clicks are dropped from the live notes
#: panel entirely. A "Text" element is a static label; a click that
#: lands on one is almost always a positional accident (cursor near a
#: caption, click on the recorder's own UI, etc.). The raw event is
#: still saved — only the visible step is suppressed.
_DROP_CLICK_CONTROL_TYPES = frozenset({"Text"})

#: Milestone keys that never produce a visible step. Backspace and Delete
#: are corrective input — they tell us the examiner edited their typing,
#: not what they actually did. The raw events stay on disk so the AI
#: rewrite can still infer "user retyped the URL" if it wants, but the
#: live notes panel and the post-stop regenerate skip them.
_DROP_KEY_NAMES = frozenset({"backspace", "delete"})


class LiveStepGenerator:
    """Capture sink that maintains the draft_steps table in real time.

    Construct one per recording and register it on the engine *after*
    the :class:`SessionSink` so the raw event has been persisted (and
    has an ``id``) by the time we run.

    ``on_changed`` is called from the capture worker thread after every
    successful append/extend; the controller bridges that to a Qt
    queued signal so the UI reloads on the GUI thread.
    """

    def __init__(
        self,
        repository: SessionRepository,
        *,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        self._repo = repository
        self._on_changed = on_changed
        self._lock = threading.Lock()
        # Track the last appended step's id so the dedup machines can
        # extend its source events when the next event merges in.
        self._last_step_id: int | None = None
        self._click_dedup = ClickDedup()
        self._key_dedup = KeyPressDedup()
        self._scroll_dedup = ScrollDedup()

    # ------------------------------------------------------------ sink API

    def handle(self, event: EnrichedEvent) -> None:
        # SessionSink runs first and stamps the persisted id onto the
        # event. If it's still None something went wrong upstream and
        # there's nothing for the step to reference.
        if event.persisted_event_id is None:
            return

        with self._lock:
            self._handle_locked(event=event, raw_id=event.persisted_event_id)

        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                logger.exception("LiveStepGenerator on_changed callback failed")

    # --------------------------------------------------------- internals

    def _handle_locked(self, *, event: EnrichedEvent, raw_id: int) -> None:
        raw = event.raw
        ts = raw.occurred_at.timestamp()
        window = event.foreground.window_title

        # Drop clicks on static text labels entirely. The raw event is
        # still on disk — only the surfaced step is omitted. Reset the
        # dedup machines so a follow-up real click doesn't accidentally
        # try to merge with whatever was last appended.
        if (
            raw.kind in {EventKind.CLICK, EventKind.DOUBLE_CLICK}
            and self._is_label_click(event.resolved)
        ):
            self._click_dedup.reset()
            self._key_dedup.reset()
            self._scroll_dedup.reset()
            return

        # Drop corrective key presses (Backspace, Delete) before they
        # ever reach the coalescer. These describe the examiner's
        # typing self-corrections, not the workflow's procedural
        # content; they belong in the raw layer (for AI rewrite to
        # interpret) but not in the live notes panel.
        if (
            raw.kind is EventKind.KEY_PRESS
            and raw.key
            and raw.key.lower() in _DROP_KEY_NAMES
        ):
            self._click_dedup.reset()
            self._key_dedup.reset()
            self._scroll_dedup.reset()
            return

        # Click coalescing: rapid repeat clicks on the same element.
        # Keyed on what the element IS (name/type/window/coarse-pos),
        # not its DB id -- the resolver returns id=None for in-flight
        # elements, so an id key merged every rapid click in a window
        # regardless of target.
        resolved = event.resolved
        if self._click_dedup.observe(
            kind=raw.kind,
            key=click_key(
                name=resolved.name if resolved else None,
                control_type=resolved.control_type if resolved else None,
                window_title=window,
                x=raw.x,
                y=raw.y,
            ),
            ts=ts,
        ) and self._last_step_id is not None:
            self._key_dedup.reset()
            self._scroll_dedup.reset()
            # One physical double-click arrives as CLICK then
            # DOUBLE_CLICK; the merged step must carry the double-click
            # verb, not the first event's "Click ...".
            merged_action = (
                render_step_action(self._adapt(raw_id=raw_id, event=event), resolved)
                if raw.kind is EventKind.DOUBLE_CLICK
                else None
            )
            self._repo.extend_step_sources(
                self._last_step_id,
                extra_event_ids=(raw_id,),
                screenshot_id=event.persisted_screenshot_id,
                action=merged_action,
            )
            return

        # Key-press coalescing: Backspace x9 → "Press Backspace 9 times".
        merge_key, key_count = self._key_dedup.observe(
            kind=raw.kind, key=(raw.key, window), ts=ts
        )
        if merge_key and self._last_step_id is not None:
            self._scroll_dedup.reset()
            adapted = self._adapt(raw_id=raw_id, event=event)
            self._repo.extend_step_sources(
                self._last_step_id,
                extra_event_ids=(raw_id,),
                screenshot_id=event.persisted_screenshot_id,
                action=render_repeat_key_press(adapted, count=key_count),
            )
            return

        # Scroll coalescing: many wheel ticks in one window → one step.
        merge_scroll, _ = self._scroll_dedup.observe(
            kind=raw.kind, key=(raw.text, window), ts=ts
        )
        if merge_scroll and self._last_step_id is not None:
            self._repo.extend_step_sources(
                self._last_step_id,
                extra_event_ids=(raw_id,),
                screenshot_id=event.persisted_screenshot_id,
            )
            return

        action_text = render_step_action(
            self._adapt(raw_id=raw_id, event=event),
            event.resolved,
        )
        step = self._repo.append_step(
            DraftStep(
                id=None,
                sequence=0,  # assigned by append_step
                action=action_text,
                source_event_ids=(raw_id,),
                screenshot_id=event.persisted_screenshot_id,
            )
        )
        self._last_step_id = step.id

    @staticmethod
    def _is_label_click(resolved: ResolvedElement | None) -> bool:
        """True when the click resolved to a UIA static-text control.

        Mirrors the batch renderer's downgrade rule: clicks on labels
        are noise, never user intent. Resolution confidence is checked
        because a low-confidence "Text" hit means UIA fell back to the
        nearest text node — those are the worst offenders.
        """
        if resolved is None:
            return False
        return (
            resolved.control_type in _DROP_CLICK_CONTROL_TYPES
            and bool(resolved.name)
        )

    @staticmethod
    def _adapt(*, raw_id: int, event: EnrichedEvent) -> RawEvent:
        """Adapt the in-flight event to the :class:`RawEvent` render helpers.

        ``render_step_action`` was written against the persisted RawEvent
        shape; reusing it keeps the live and batch wording identical.
        """
        raw = event.raw
        fg = event.foreground
        return RawEvent(
            id=raw_id,
            sequence=0,
            occurred_at=raw.occurred_at,
            kind=raw.kind,
            button=raw.button,
            x=raw.x,
            y=raw.y,
            key=raw.key,
            text=raw.text,
            window_title=fg.window_title or None,
            process_name=fg.process_name or None,
            screenshot_id=None,
            resolved_element_id=event.resolved.id if event.resolved else None,
        )
