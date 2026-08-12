"""Tests for the controller's _friendly_llm_error translator.

The translator turns raw LLM exception text into operator-readable
guidance shown in the "LLM rewrite failed" QMessageBox. Each branch
maps to a real failure mode the field has hit:

- connection refused: Ollama isn't running
- timed out: model is too big for the hardware
- 404 / model not found: configured model isn't pulled
- schema mismatch: model returned JSON in the wrong shape (the
  field-reported case where the dialog used to dump 500 chars of
  dict repr at the operator)
"""

from __future__ import annotations

from inscription.ui import controller_errors
from inscription.ui.controller import _friendly_llm_error


def test_connection_refused_maps_to_ollama_hint() -> None:
    msg = _friendly_llm_error(
        "LLM request to http://127.0.0.1:11435/v1/chat/completions failed: "
        "[Errno 111] Connection refused",
        base_url="http://127.0.0.1:11435/v1",
    )
    assert "Couldn't reach the local LLM server" in msg
    assert "Start Ollama" in msg
    assert "http://127.0.0.1:11435/v1" in msg


def test_timeout_maps_to_size_or_timeout_hint() -> None:
    msg = _friendly_llm_error(
        "LLM request to http://127.0.0.1:11435/v1/chat/completions timed out after 60s",
        base_url="http://127.0.0.1:11435/v1",
    )
    assert "took too long" in msg
    assert "smaller model" in msg or "raise the timeout" in msg


def test_404_maps_to_pull_hint() -> None:
    msg = _friendly_llm_error(
        "LLM HTTP 404 from http://127.0.0.1:11435/v1/chat/completions: "
        "model 'no-such-model' not found",
        base_url="http://127.0.0.1:11435/v1",
    )
    assert "isn't available on the LLM server" in msg
    assert "ollama pull" in msg


def test_404_lists_available_models_when_probe_succeeds(monkeypatch) -> None:
    """The 404 dialog names the models the server DOES carry -- the
    field case was a launcher exporting a broken model name, leaving
    the operator staring at 'gemma4:latest not found' with no clue
    the store held a perfectly good qwen."""
    monkeypatch.setattr(
        controller_errors,
        "available_models_hint",
        lambda base_url, **_kw: (
            "Models this server does have: qwen2.5:7b-instruct-q5_K_M\n\n"
        ),
    )
    msg = controller_errors.friendly_llm_error(
        "LLM HTTP 404 from http://127.0.0.1:11435/v1/chat/completions: "
        "model 'gemma4:latest' not found",
        base_url="http://127.0.0.1:11435/v1",
    )
    assert "qwen2.5:7b-instruct-q5_K_M" in msg
    assert "Switch to one of those" in msg


def test_missing_steps_key_does_not_dump_payload() -> None:
    """Field bug fix: previously dumped the whole bad payload at the
    operator. Now we summarise + point at the log file."""
    raw = (
        "LLM content missing top-level 'steps' key: "
        "{'session_id': 'deadbeef', 'user_info': {'username': 'test_user_123'}, "
        "'events': [{'id': 1, 'kind': 'click', 'window_title': 'Magnet AXIOM'}]}"
    )
    msg = _friendly_llm_error(raw, base_url="http://127.0.0.1:11435/v1")
    assert "unexpected shape" in msg
    assert "stronger model" in msg
    assert "Show logs folder" in msg
    # Crucially: the raw payload is NOT pasted into the message.
    assert "deadbeef" not in msg
    assert "test_user_123" not in msg


def test_did_not_return_json_maps_to_schema_message() -> None:
    raw = (
        "LLM did not return JSON. The configured model may be ignoring "
        "the json_object response_format directive — try a stronger / "
        "instruction-tuned model in Edit → Settings → LLM. First chars "
        "of reply: 'Sure, here is the rewritten...'"
    )
    msg = _friendly_llm_error(raw, base_url="http://127.0.0.1:11435/v1")
    assert "unexpected shape" in msg


def test_zero_usable_steps_maps_to_schema_message() -> None:
    raw = "LLM returned zero usable steps"
    msg = _friendly_llm_error(raw, base_url="http://127.0.0.1:11435/v1")
    assert "unexpected shape" in msg


def test_unrecognised_error_returns_raw() -> None:
    """Default fallthrough: an error the translator doesn't recognise
    is surfaced verbatim so the operator at least has something to grep
    the log for."""
    raw = "totally novel failure mode"
    assert _friendly_llm_error(raw, base_url="http://127.0.0.1:11435/v1") == raw
