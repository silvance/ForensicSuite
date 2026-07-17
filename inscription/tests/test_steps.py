"""Step generation: raw events -> draft steps."""

from __future__ import annotations

from datetime import timedelta

from inscription.model import DraftStep, EventKind, ResolvedElement, utcnow
from inscription.steps import StepGenerator, render_step_action
from inscription.storage import SessionRepository


def _append_click(
    repo: SessionRepository,
    *,
    when,
    resolved_id=None,
    window="App",
    screenshot_id=None,
):
    return repo.append_event(
        kind=EventKind.CLICK,
        occurred_at=when,
        button="left",
        x=10,
        y=10,
        window_title=window,
        process_name="app.exe",
        screenshot_id=screenshot_id,
        resolved_element_id=resolved_id,
    )


def test_render_step_action_prefers_uia_name() -> None:
    event = type(
        "E",
        (),
        {
            "kind": EventKind.CLICK,
            "key": None,
            "text": None,
            "window_title": "Settings",
            "process_name": "settings.exe",
            "button": "left",
        },
    )()
    resolved = ResolvedElement(
        id=1,
        name="Save",
        control_type="Button",
        confidence=0.9,
        method="uia",
        owner_process_name="settings.exe",
    )
    rendered = render_step_action(event, resolved)  # type: ignore[arg-type]
    assert "Save" in rendered
    assert "Button" in rendered
    assert "Settings" in rendered


def test_render_scroll_uses_descriptor_and_window_title() -> None:
    event = type(
        "E",
        (),
        {
            "kind": EventKind.SCROLL,
            "key": None,
            "text": "down 8",
            "window_title": "Google Chrome",
            "process_name": "chrome.exe",
            "button": None,
        },
    )()
    rendered = render_step_action(event, None)  # type: ignore[arg-type]
    assert "Scroll" in rendered
    assert "down 8" in rendered
    assert "Google Chrome" in rendered


def test_render_click_drops_in_window_for_cross_process_element() -> None:
    # Taskbar / Start-menu clicks: the element lives in explorer.exe but
    # the foreground is still the user's previous app. Gluing them produces
    # misleading phrases like "Click the 'Python' Button in Notepad."
    event = type(
        "E",
        (),
        {
            "kind": EventKind.CLICK,
            "key": None,
            "text": None,
            "window_title": "Notepad",
            "process_name": "notepad.exe",
            "button": "left",
        },
    )()
    resolved = ResolvedElement(
        id=1,
        name="Python 3.14 - 1 running window",
        control_type="Button",
        confidence=0.9,
        method="uia",
        owner_process_name="explorer.exe",
    )
    rendered = render_step_action(event, resolved)  # type: ignore[arg-type]
    assert "Python 3.14 - 1 running window" in rendered
    assert "Button" in rendered
    assert "Notepad" not in rendered  # the misleading "in Notepad" must be gone


def test_generator_collapses_redundant_clicks(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Gen")
    try:
        resolved = repo.add_resolved_element(
            ResolvedElement(id=None, name="OK", control_type="Button", confidence=0.9, method="uia")
        )
        t0 = utcnow()
        _append_click(repo, when=t0, resolved_id=resolved.id)
        _append_click(repo, when=t0 + timedelta(milliseconds=300), resolved_id=resolved.id)
        # Third click after dedup window -> separate step
        _append_click(repo, when=t0 + timedelta(seconds=5), resolved_id=resolved.id)

        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 2
        assert "OK" in steps[0].action
    finally:
        repo.close()


def test_generator_suppresses_window_focus_before_click(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Focus")
    try:
        t0 = utcnow()
        repo.append_event(
            kind=EventKind.WINDOW_FOCUS,
            occurred_at=t0,
            text="Target",
        )
        repo.append_event(
            kind=EventKind.CLICK,
            occurred_at=t0 + timedelta(milliseconds=200),
            button="left",
            x=5,
            y=5,
            window_title="Target",
        )
        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 1
        assert "Target" in steps[0].action
    finally:
        repo.close()


def test_render_click_downgrades_text_control_to_window_title() -> None:
    # UIA "Text" controls are static labels; clicks on them are positional
    # accidents (e.g. clicking near a label or on the recorder's own UI).
    # The renderer must fall back to the window-title path and not produce
    # garbage like "Click the 'No screenshot' Text in Inscription."
    event = type(
        "E",
        (),
        {
            "kind": EventKind.CLICK,
            "key": None,
            "text": None,
            "window_title": "Inscription",
            "process_name": "python.exe",
            "button": "left",
        },
    )()
    resolved = ResolvedElement(
        id=1,
        name="No screenshot",
        control_type="Text",
        confidence=0.95,
        method="uia",
        owner_process_name="python.exe",
    )
    rendered = render_step_action(event, resolved)  # type: ignore[arg-type]
    assert "No screenshot" not in rendered
    assert "Text" not in rendered
    assert "Inscription" in rendered  # window-title fallback


def test_generator_preserves_manual_edits(tmp_path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Edits")
    try:
        t0 = utcnow()
        _append_click(repo, when=t0)

        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 1
        first = steps[0]
        assert first.id is not None

        repo.update_step_fields(first.id, action="Click the magical button")

        regenerated = StepGenerator(repo).regenerate()
        assert len(regenerated) == 1
        assert regenerated[0].action == "Click the magical button"
        assert regenerated[0].manual_edit is True
    finally:
        repo.close()


# -------------------------------------------- clustering-correctness regressions


def test_generator_merges_rapid_clicks_across_distinct_resolved_rows(tmp_path) -> None:
    """Production inserts a FRESH resolved_elements row per click, so two
    clicks on the same button never share a row id. The dedup key is the
    element's semantic identity (name/type/window), not the row id --
    keying on the id made batch click-merging dead code in production.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="FreshRows")
    try:
        t0 = utcnow()
        for ms in (0, 300):
            r = repo.add_resolved_element(
                ResolvedElement(
                    id=None, name="OK", control_type="Button",
                    confidence=0.9, method="uia",
                )
            )
            _append_click(repo, when=t0 + timedelta(milliseconds=ms), resolved_id=r.id)
        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 1  # merged despite distinct row ids
    finally:
        repo.close()


def test_generator_does_not_merge_different_controls_in_same_window(tmp_path) -> None:
    """Two rapid clicks on DIFFERENT named controls are two steps.

    Guards the other half of the key fix: the live path used to key on
    (None, window) and merged everything in a window into one step.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="TwoControls")
    try:
        t0 = utcnow()
        for ms, name in ((0, "Save"), (300, "Cancel")):
            r = repo.add_resolved_element(
                ResolvedElement(
                    id=None, name=name, control_type="Button",
                    confidence=0.9, method="uia",
                )
            )
            _append_click(repo, when=t0 + timedelta(milliseconds=ms), resolved_id=r.id)
        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 2
        assert "Save" in steps[0].action
        assert "Cancel" in steps[1].action
    finally:
        repo.close()


def test_generator_double_click_is_one_step_with_double_click_verb(tmp_path) -> None:
    """A physical double-click arrives as CLICK then DOUBLE_CLICK; the
    exhibit must show ONE step that says "Double-click", not two steps
    or a step mislabelled "Click"."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="Dbl")
    try:
        t0 = utcnow()
        r1 = repo.add_resolved_element(
            ResolvedElement(id=None, name="report.pdf", control_type="ListItem",
                            confidence=0.9, method="uia")
        )
        r2 = repo.add_resolved_element(
            ResolvedElement(id=None, name="report.pdf", control_type="ListItem",
                            confidence=0.9, method="uia")
        )
        _append_click(repo, when=t0, resolved_id=r1.id)
        repo.append_event(
            kind=EventKind.DOUBLE_CLICK,
            occurred_at=t0 + timedelta(milliseconds=250),
            button="left", x=10, y=10,
            window_title="App", process_name="app.exe",
            resolved_element_id=r2.id,
        )
        steps = StepGenerator(repo).regenerate()
        assert len(steps) == 1
        assert steps[0].action.startswith("Double-click")
        assert len(steps[0].source_event_ids) == 2
    finally:
        repo.close()


def test_regenerate_preserves_manual_step_with_unmatched_sources(tmp_path) -> None:
    """A split_step half (manual, source set not reproduced by
    clustering) must survive regenerate -- which fires automatically on
    every recording stop. Its event ids are removed from generated
    steps so no event is referenced twice.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="SplitSurvives")
    try:
        t0 = utcnow()
        e1 = _append_click(repo, when=t0)
        e2 = _append_click(repo, when=t0 + timedelta(milliseconds=100))
        assert e1.id is not None
        assert e2.id is not None
        # Simulate a manual split: two manual steps, one event each --
        # clustering would merge these two events into one step.
        repo.replace_steps([
            DraftStep(id=None, sequence=0, action="First half (edited)",
                      source_event_ids=(e1.id,), manual_edit=True),
            DraftStep(id=None, sequence=0, action="Second half (edited)",
                      source_event_ids=(e2.id,), manual_edit=True,
                      evidentiary=True),
        ])
        steps = StepGenerator(repo).regenerate()
        actions = [s.action for s in steps]
        assert "First half (edited)" in actions
        assert "Second half (edited)" in actions
        # Flags survive, and no event id appears in two steps.
        assert any(s.evidentiary for s in steps)
        all_ids = [eid for s in steps for eid in s.source_event_ids]
        assert len(all_ids) == len(set(all_ids))
    finally:
        repo.close()


def test_regenerate_preserves_sourceless_suggestion_draft(tmp_path) -> None:
    """A suggestion-drafted step has source_event_ids=() and manual_edit
    True; regenerate used to silently delete it."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="DraftSurvives")
    try:
        e1 = _append_click(repo, when=utcnow())
        assert e1.id is not None
        repo.replace_steps([
            DraftStep(id=None, sequence=0, action="Verify SHA-256 of the image",
                      source_event_ids=(), manual_edit=True),
        ])
        steps = StepGenerator(repo).regenerate()
        actions = [s.action for s in steps]
        assert "Verify SHA-256 of the image" in actions
        # And it sorts after the event-anchored steps.
        assert actions[-1] == "Verify SHA-256 of the image"
    finally:
        repo.close()
