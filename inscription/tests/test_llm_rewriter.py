"""StepRewriter: end-to-end with a fake LLMClient."""

from __future__ import annotations

import json

import pytest
from suite_common.llm import LLMRequestError, LLMResponseError

from inscription.llm.rewriter import StepRewriter
from inscription.model import EventKind, ResolvedElement, utcnow
from inscription.steps import generate_steps
from inscription.storage import SessionRepository


class _FakeClient:
    """Stand-in for :class:`LLMClient`; returns a canned content string."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[tuple[str, str]] = []

    def chat(self, *, system: str, user: str, **_kwargs: object) -> str:
        self.calls.append((system, user))
        return self.content


class _SequenceClient:
    """Returns ``responses[i]`` on the i-th call, then raises if out of items.

    Used to test the rewriter's one-shot retry-on-schema-mismatch path:
    first call returns garbage, second call returns valid JSON.
    """

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat(self, *, system: str, user: str, **_kwargs: object) -> str:
        self.calls.append((system, user))
        if not self._responses:
            msg = "_SequenceClient ran out of canned responses"
            raise AssertionError(msg)
        return self._responses.pop(0)


class _ExplodingClient:
    def chat(self, *, system: str, user: str, **_kwargs: object) -> str:
        msg = "connection refused"
        raise LLMRequestError(msg)


def _seed_one_click(repo: SessionRepository) -> int:
    resolved = repo.add_resolved_element(
        ResolvedElement(id=None, name="Save", control_type="Button", confidence=0.9, method="uia")
    )
    event = repo.append_event(
        kind=EventKind.CLICK,
        occurred_at=utcnow(),
        button="left",
        x=1,
        y=1,
        window_title="Notepad",
        process_name="notepad.exe",
        resolved_element_id=resolved.id,
    )
    assert event.id is not None
    return event.id


def test_rewriter_replaces_steps_from_llm_response(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="LLM")
    try:
        eid = _seed_one_click(repo)
        content = json.dumps(
            {
                "steps": [
                    {
                        "action": "Save the document.",
                        "result": "File saved.",
                        "source_event_ids": [eid],
                    }
                ]
            }
        )
        rewriter = StepRewriter(repository=repo, client=_FakeClient(content))
        steps = rewriter.rewrite()

        assert len(steps) == 1
        assert steps[0].action == "Save the document."
        assert steps[0].result == "File saved."
        assert steps[0].source_event_ids == (eid,)
        assert steps[0].manual_edit is False
    finally:
        repo.close()


def test_rewriter_preserves_manual_edits(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Edits")
    try:
        eid = _seed_one_click(repo)

        # Simulate an earlier rule-based generation + manual edit.
        generate_steps(repo)
        initial = repo.list_steps()[0]
        assert initial.id is not None
        repo.update_step_fields(initial.id, action="Custom human text", result="Observed.")

        # LLM suggests different text; the human edit should win.
        content = json.dumps(
            {
                "steps": [
                    {"action": "Robot text.", "result": "robot result", "source_event_ids": [eid]}
                ]
            }
        )
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()

        assert len(steps) == 1
        assert steps[0].action == "Custom human text"
        assert steps[0].result == "Observed."
        assert steps[0].manual_edit is True
    finally:
        repo.close()


def test_rewriter_surfaces_client_failure(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Boom")
    try:
        _seed_one_click(repo)
        rewriter = StepRewriter(repository=repo, client=_ExplodingClient())
        with pytest.raises(LLMRequestError):
            rewriter.rewrite()
    finally:
        repo.close()


def test_rewriter_noop_on_empty_session(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Empty")
    try:
        client = _FakeClient("should not be called")
        rewriter = StepRewriter(repository=repo, client=client)
        steps = rewriter.rewrite()
        assert steps == []
        assert client.calls == []
    finally:
        repo.close()


def test_rewriter_picks_last_screenshot_for_merged_step(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Merge")
    try:
        shot_a = repo.add_screenshot(
            relative_path="screenshots/a.png",
            captured_at=utcnow(),
            width=1,
            height=1,
        )
        shot_b = repo.add_screenshot(
            relative_path="screenshots/b.png",
            captured_at=utcnow(),
            width=1,
            height=1,
        )
        e1 = repo.append_event(
            kind=EventKind.CLICK,
            button="left",
            x=1,
            y=1,
            screenshot_id=shot_a.id,
        )
        e2 = repo.append_event(
            kind=EventKind.CLICK,
            button="left",
            x=2,
            y=2,
            screenshot_id=shot_b.id,
        )
        assert e1.id is not None
        assert e2.id is not None

        content = json.dumps(
            {
                "steps": [
                    {
                        "action": "Merged step.",
                        "result": "",
                        "source_event_ids": [e1.id, e2.id],
                    },
                ]
            }
        )
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()

        # Last-event-with-a-screenshot wins.
        assert steps[0].screenshot_id == shot_b.id
    finally:
        repo.close()


# ----------------------------------------------------- schema-retry path

def _bad_shape_reply() -> str:
    """The exact failure mode the operator reported in the field:

    The model returned a JSON object, but with the WRONG top-level keys
    (it echoed a synthetic session record back to us instead of wrapping
    the events in ``{"steps": [...]}``). parse_response raises
    ``LLMResponseError`` on this; the rewriter retries once.
    """
    return json.dumps({
        "session_id": "deadbeef",
        "user_info": {"username": "test_user_123"},
        "events": [{"id": 1, "kind": "click"}],
    })


def test_rewriter_retries_once_on_bad_schema(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Retry")
    try:
        eid = _seed_one_click(repo)
        good = json.dumps({
            "steps": [
                {
                    "action": "Click Save.",
                    "result": "",
                    "source_event_ids": [eid],
                }
            ]
        })
        client = _SequenceClient([_bad_shape_reply(), good])
        steps = StepRewriter(repository=repo, client=client).rewrite()
        assert len(steps) == 1
        assert steps[0].action == "Click Save."
        # Two LLM calls: original + corrective retry.
        assert len(client.calls) == 2
        # The retry user prompt embeds the bad reply for the model to see.
        retry_user = client.calls[1][1]
        assert "previous_reply" in retry_user
        assert "deadbeef" in retry_user
    finally:
        repo.close()


def test_rewriter_surfaces_retry_failure(tmp_path) -> None:
    """If the retry also returns the wrong shape, the rewriter raises
    -- it doesn't loop forever."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="RetryBad")
    try:
        _seed_one_click(repo)
        client = _SequenceClient([_bad_shape_reply(), _bad_shape_reply()])
        rewriter = StepRewriter(repository=repo, client=client)
        with pytest.raises(LLMResponseError):
            rewriter.rewrite()
        assert len(client.calls) == 2  # original + one retry, no third
    finally:
        repo.close()


# --------------------------------------------------- evidentiary/suppressed


def _seed_three_clicks(repo: SessionRepository) -> tuple[int, int, int]:
    """Three click events sharing one resolved element. Returns event ids."""
    resolved = repo.add_resolved_element(
        ResolvedElement(
            id=None, name="Save", control_type="Button", confidence=0.9, method="uia"
        )
    )
    ids = []
    for x in (1, 2, 3):
        ev = repo.append_event(
            kind=EventKind.CLICK,
            occurred_at=utcnow(),
            button="left",
            x=x,
            y=1,
            window_title="Notepad",
            process_name="notepad.exe",
            resolved_element_id=resolved.id,
        )
        assert ev.id is not None
        ids.append(ev.id)
    return ids[0], ids[1], ids[2]


def test_rewriter_preserves_evidentiary_across_exact_match(tmp_path) -> None:
    """A step the user marked evidentiary must stay evidentiary even
    when the LLM rewrites it with the same source_event_ids tuple.

    Before the fix, _materialise dropped the flag because the rewriter
    constructed new DraftStep objects with no evidentiary= arg, so they
    defaulted to False -- silently removing the step from the
    downstream report after every rewrite.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="EvidStay")
    try:
        eid = _seed_one_click(repo)
        generate_steps(repo)
        initial = repo.list_steps()[0]
        assert initial.id is not None
        repo.set_step_evidentiary(initial.id, evidentiary=True)

        content = json.dumps({
            "steps": [
                {"action": "Rewritten.", "result": "", "source_event_ids": [eid]}
            ]
        })
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()

        assert len(steps) == 1
        assert steps[0].evidentiary is True


    finally:
        repo.close()


def test_rewriter_preserves_suppressed_across_exact_match(tmp_path) -> None:
    """Same as above but for suppressed: a suppressed step must stay
    suppressed across a rewrite, or it resurrects into the report."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="SuppStay")
    try:
        eid = _seed_one_click(repo)
        generate_steps(repo)
        initial = repo.list_steps()[0]
        assert initial.id is not None
        repo.set_step_suppressed(initial.id, suppressed=True)

        content = json.dumps({
            "steps": [
                {"action": "Rewritten.", "result": "", "source_event_ids": [eid]}
            ]
        })
        # include_suppressed=True so list_steps surfaces the suppressed
        # row -- a rewrite that loses the flag would re-surface it
        # under the default filter.
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()
        assert len(steps) == 1
        assert steps[0].suppressed is True
    finally:
        repo.close()


def test_rewriter_propagates_evidentiary_when_llm_splits_step(tmp_path) -> None:
    """If the LLM splits one user-flagged step into two, both halves
    inherit the flag.

    Mirrors the OR-on-split behaviour storage.split_step uses for
    manual splits -- a forensic decision the operator made about a
    cluster of events must reach every event in that cluster, not just
    the first half.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="EvidSplit")
    try:
        e1, e2, _e3 = _seed_three_clicks(repo)
        generate_steps(repo)
        initial = repo.list_steps()[0]
        assert initial.id is not None
        # Step covers e1, e2, e3 -- mark the whole thing evidentiary.
        repo.set_step_evidentiary(initial.id, evidentiary=True)

        # LLM rewrites into two steps, splitting the cluster.
        content = json.dumps({
            "steps": [
                {"action": "First half.", "result": "", "source_event_ids": [e1]},
                {"action": "Second half.", "result": "", "source_event_ids": [e2]},
            ]
        })
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()

        assert len(steps) == 2
        assert all(s.evidentiary for s in steps)
    finally:
        repo.close()


def test_rewriter_unions_flags_when_llm_merges_steps(tmp_path) -> None:
    """If the LLM merges two user-flagged steps into one, the merged
    step inherits both flags (OR across all source events).

    Mirrors merge_steps' "union the flags" behaviour: combining
    forensic annotations should be additive, never lossy.
    """
    from inscription.model import DraftStep  # noqa: PLC0415

    repo = SessionRepository.create(workspace_root=tmp_path, name="FlagsMerge")
    try:
        e1, e2, _e3 = _seed_three_clicks(repo)
        # Skip generate_steps -- it collapses our identical clicks
        # into a single row. Plant two distinct draft steps directly
        # so we can exercise the rewriter's flag-union logic.
        repo.replace_steps([
            DraftStep(
                id=None,
                sequence=0,
                action="First",
                source_event_ids=(e1,),
                evidentiary=True,
            ),
            DraftStep(
                id=None,
                sequence=0,
                action="Second",
                source_event_ids=(e2,),
                suppressed=True,
            ),
        ])

        # LLM proposes a single merged step covering both clusters.
        content = json.dumps({
            "steps": [
                {"action": "All in one.", "result": "", "source_event_ids": [e1, e2]}
            ]
        })
        steps = StepRewriter(repository=repo, client=_FakeClient(content)).rewrite()

        assert len(steps) == 1
        assert steps[0].evidentiary is True
        assert steps[0].suppressed is True
    finally:
        repo.close()
