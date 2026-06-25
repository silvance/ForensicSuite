"""Tests for the shared LLM JSON parsing helpers."""

from __future__ import annotations

from suite_common.llm_parse import (
    extract_first_json_object,
    parse_json_lenient,
    strip_code_fences,
)


# ----------------------------------------------------- parse_json_lenient


def test_parse_strict_json_directly() -> None:
    """A clean JSON reply round-trips through the lenient path too."""
    assert parse_json_lenient('{"steps": []}') == {"steps": []}


def test_parse_recovers_from_leading_commentary() -> None:
    """Small models often prepend "Sure! Here's the JSON:" -- we
    should still recover the first balanced {...} block."""
    body = 'Sure! Here is the JSON:\n\n{"steps": [{"action": "Click"}]}'
    assert parse_json_lenient(body) == {"steps": [{"action": "Click"}]}


def test_parse_returns_none_when_no_brace_at_all() -> None:
    """Pure prose with no JSON anywhere returns None, not raises."""
    assert parse_json_lenient("I cannot help with that.") is None


def test_parse_returns_none_when_extracted_block_is_invalid_json() -> None:
    """Block looks balanced but isn't valid JSON (e.g. unquoted key).
    Lenient mode shouldn't invent structure -- return None and let
    the caller surface a schema error."""
    assert parse_json_lenient("Reply: {unquoted: value}") is None


# ----------------------------------------------------- extract_first_json_object


def test_extract_handles_strings_with_braces() -> None:
    """A ``{`` or ``}`` inside a quoted string must not change depth."""
    body = 'prelude {"action": "press }", "result": ""} trailing'
    assert extract_first_json_object(body) == '{"action": "press }", "result": ""}'


def test_extract_handles_escaped_quotes_in_strings() -> None:
    """Escape-aware: a ``\\"`` doesn't end the string."""
    body = r'{"k": "a\"b{}c"}'
    assert extract_first_json_object(body) == r'{"k": "a\"b{}c"}'


def test_extract_returns_none_when_unbalanced() -> None:
    """Unmatched braces shouldn't trigger a partial result."""
    assert extract_first_json_object('{"steps": [') is None


def test_extract_returns_first_complete_object() -> None:
    """If there are two distinct objects, return the first balanced one."""
    body = 'a {"first": 1} b {"second": 2}'
    assert extract_first_json_object(body) == '{"first": 1}'


# ----------------------------------------------------- strip_code_fences


def test_strip_code_fences_passes_through_unchanged_text() -> None:
    """No fence, no change. Safe to call unconditionally."""
    assert strip_code_fences('{"steps": []}') == '{"steps": []}'


def test_strip_code_fences_handles_json_language_tag() -> None:
    body = '```json\n{"steps": []}\n```'
    assert strip_code_fences(body) == '{"steps": []}'


def test_strip_code_fences_handles_bare_fence() -> None:
    body = '```\n{"steps": []}\n```'
    assert strip_code_fences(body) == '{"steps": []}'


def test_strip_code_fences_single_line_fence_is_left_alone() -> None:
    """A fence with no newline is malformed; return as-is rather than
    eating data."""
    assert strip_code_fences("```text```") == "```text```"
