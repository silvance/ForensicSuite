"""Session → LLM → rewritten draft_steps orchestration.

The rewriter is intentionally a thin sequence: read events and elements
from the repository, ask :class:`LLMClient` to rewrite, parse the
response, and write back via :meth:`SessionRepository.replace_steps`.

Manual-edit preservation mirrors the rule-based :class:`StepGenerator`:
a step whose ``source_event_ids`` match an existing ``manual_edit`` row
is copied verbatim rather than overwritten with the LLM's text.

A pre-pass also handles the empty-session case: if there are no events
yet, there is nothing to rewrite — we don't call the LLM at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from suite_common.llm import LLMResponseError

from inscription.llm.prompt import (
    SYSTEM_PROMPT,
    build_user_prompt,
    parse_response,
)
from inscription.model import DraftStep

if TYPE_CHECKING:
    from suite_common.llm import ChatClient

    from inscription.llm.prompt import RewrittenStep
    from inscription.model import RawEvent, ResolvedElement
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)

#: Cap on how much of the model's bad reply we feed back into the
#: corrective retry. Long enough for the model to see the shape it
#: produced, short enough to keep the retry prompt small.
_RETRY_REPLY_PREVIEW_LIMIT = 1500


@dataclass(frozen=True, slots=True)
class _StepFlags:
    """Sticky per-event flags carried across a rewrite.

    Independent of LLM output: the user marks a step evidentiary or
    suppressed via the workspace controls, and that decision survives
    every subsequent rewrite/regenerate cycle regardless of how the
    LLM re-clusters the underlying events.
    """

    evidentiary: bool = False
    suppressed: bool = False


class StepRewriter:
    """Rewrite a session's draft_steps using a language model."""

    def __init__(self, *, repository: SessionRepository, client: ChatClient) -> None:
        self._repo = repository
        self._client = client

    def rewrite(self) -> list[DraftStep]:
        """Rewrite in-place. Returns the persisted :class:`DraftStep` list.

        Raises :class:`inscription.llm.LLMError` subclasses on any failure;
        the caller is expected to catch and fall back to the rule-based
        text (which remains intact in the DB because we only write on
        success).
        """
        events = self._repo.list_events()
        if not events:
            logger.info("rewrite: session has no events, nothing to do")
            return self._repo.list_steps()

        existing_steps = self._repo.list_steps(include_suppressed=True)
        manual_by_sources: dict[tuple[int, ...], DraftStep] = {
            s.source_event_ids: s for s in existing_steps if s.manual_edit
        }
        # Per-event flag index: for each source event the user touched
        # via "mark as evidentiary" or "suppress", remember the flags
        # so a rewrite that re-clusters the events the LLM saw still
        # carries those decisions across. Indexed by event id rather
        # than source_event_ids tuple because the LLM may merge/split
        # events differently from how the user grouped them; any new
        # step that mentions an event the user flagged should inherit
        # the flag. Mirrors the OR-on-merge / propagate-on-split
        # behaviour ``merge_steps`` / ``split_step`` use in storage.
        flag_by_event_id: dict[int, _StepFlags] = {}
        for s in existing_steps:
            if not (s.evidentiary or s.suppressed):
                continue
            for eid in s.source_event_ids:
                prev = flag_by_event_id.get(eid, _StepFlags())
                flag_by_event_id[eid] = _StepFlags(
                    evidentiary=prev.evidentiary or s.evidentiary,
                    suppressed=prev.suppressed or s.suppressed,
                )

        resolved_by_id = self._load_resolved_elements(events)

        user_prompt = build_user_prompt(
            session_name=self._repo.session.info.name,
            events=events,
            resolved_by_id=resolved_by_id,
            existing_steps=existing_steps,
        )

        valid_ids = {e.id for e in events if e.id is not None}
        rewritten = self._chat_and_parse(
            user_prompt=user_prompt, valid_ids=valid_ids
        )

        new_steps = self._materialise(
            rewritten=rewritten,
            events=events,
            manual_by_sources=manual_by_sources,
            flag_by_event_id=flag_by_event_id,
        )
        if not new_steps:
            msg = "LLM produced no usable steps after manual-edit merge"
            raise LLMResponseError(msg)

        saved = self._repo.replace_steps(new_steps)
        self._repo.flush_manifest()
        logger.info(
            "rewrite: %d events → %d steps (from %d proposed)",
            len(events),
            len(saved),
            len(rewritten),
        )
        return saved

    # ---------------------------------------------------------- internals

    def _chat_and_parse(
        self, *, user_prompt: str, valid_ids: set[int]
    ) -> list[RewrittenStep]:
        """Call the LLM and parse the reply, with one corrective retry.

        Smaller / less instruction-tuned local models occasionally
        return JSON in the wrong shape (e.g. echoing back a synthetic
        session record instead of wrapping the events in
        ``{"steps": [...]}``). When that happens we feed the bad reply
        back to the same model with a brief correction and try once
        more before surfacing the error -- one retry is cheap relative
        to a 30-90s rewrite, and it resolves the typical drift case.
        Connection / timeout errors propagate directly: they're not
        improved by retrying through the same client without a delay.
        """
        raw = self._client.chat(system=SYSTEM_PROMPT, user=user_prompt)
        try:
            return parse_response(raw, valid_event_ids=valid_ids)
        except LLMResponseError as first_error:
            logger.warning(
                "rewrite: first reply failed schema check (%s); retrying once",
                first_error,
            )
            preview = raw.strip()
            if len(preview) > _RETRY_REPLY_PREVIEW_LIMIT:
                preview = preview[:_RETRY_REPLY_PREVIEW_LIMIT] + "…[truncated]"
            retry_prompt = (
                f"{user_prompt}\n\n"
                "Your previous reply did not match the required schema. "
                "It must be a JSON object with a top-level 'steps' array; "
                "each entry has 'action' (string), 'result' (string), and "
                "'source_event_ids' (array of integer event ids from the "
                "input). Do not include any other top-level keys. Do not "
                "invent ids that aren't in the input timeline.\n\n"
                "Your previous reply:\n"
                "<previous_reply>\n"
                f"{preview}\n"
                "</previous_reply>\n\n"
                "Reply now with the corrected JSON object only. "
                "Start with { and end with }."
            )
            raw_retry = self._client.chat(system=SYSTEM_PROMPT, user=retry_prompt)
            try:
                return parse_response(raw_retry, valid_event_ids=valid_ids)
            except LLMResponseError as retry_error:
                logger.warning(
                    "rewrite: retry also failed schema check (%s)", retry_error
                )
                raise

    def _load_resolved_elements(self, events: list[RawEvent]) -> dict[int, ResolvedElement]:
        ids = {e.resolved_element_id for e in events if e.resolved_element_id is not None}
        out: dict[int, ResolvedElement] = {}
        for element_id in ids:
            elem = self._repo.get_resolved_element(element_id)
            if elem is not None:
                out[element_id] = elem
        return out

    @staticmethod
    def _materialise(
        *,
        rewritten: list[RewrittenStep],
        events: list[RawEvent],
        manual_by_sources: dict[tuple[int, ...], DraftStep],
        flag_by_event_id: dict[int, _StepFlags],
    ) -> list[DraftStep]:
        """Turn parsed LLM steps into DraftStep rows ready for replace_steps.

        Picks each step's screenshot_id from the last referenced event that
        has one (the "result of the action" frame is usually most useful).
        Preserves manual-edit text when the source_event_ids tuple is an
        exact match, and ORs the evidentiary / suppressed flags across
        every source event the new step references -- so a rewrite that
        re-clusters events doesn't silently drop the operator's
        "mark for report" or "hide from report" decisions.
        """
        events_by_id = {e.id: e for e in events if e.id is not None}
        out: list[DraftStep] = []
        for item in rewritten:
            screenshot_id = _pick_screenshot_id(item.source_event_ids, events_by_id)
            preserved = manual_by_sources.get(item.source_event_ids)
            if preserved is not None:
                action = preserved.action
                result = preserved.result
            else:
                action = item.action
                result = item.result
            evidentiary = any(
                flag_by_event_id.get(eid, _StepFlags()).evidentiary
                for eid in item.source_event_ids
            )
            suppressed = any(
                flag_by_event_id.get(eid, _StepFlags()).suppressed
                for eid in item.source_event_ids
            )
            out.append(
                DraftStep(
                    id=None,
                    sequence=0,  # assigned by replace_steps
                    action=action,
                    result=result,
                    source_event_ids=item.source_event_ids,
                    screenshot_id=screenshot_id,
                    manual_edit=preserved is not None,
                    evidentiary=evidentiary,
                    suppressed=suppressed,
                )
            )
        return out


def _pick_screenshot_id(
    source_event_ids: tuple[int, ...], events_by_id: dict[int, RawEvent]
) -> int | None:
    for eid in reversed(source_event_ids):
        event = events_by_id.get(eid)
        if event is not None and event.screenshot_id is not None:
            return event.screenshot_id
    return None
