"""Lenient JSON parsing helpers for LLM responses.

Inscription's step rewriter and CaseGuide's suggestions refiner both
have to coax JSON out of replies from small / weakly instruction-tuned
local models. The models routinely:

  - prepend commentary ("Sure! Here's the JSON: { ... }")
  - wrap the reply in ```json ... ``` fences
  - return JSON with trailing prose

The helpers below recover those cases without inventing structure that
wasn't present. ``parse_json_lenient`` is the entry point; the others
are exposed for callers that want to compose the steps differently.

Lives in suite_common so both apps share one implementation -- the
audit found this code duplicated verbatim with the second copy's
docstring literally saying "Mirrors Inscription's parser."
"""

from __future__ import annotations

import json


def parse_json_lenient(body: str) -> object | None:
    """Try strict JSON first; fall back to extracting the first ``{...}``.

    Smaller / weakly instruction-tuned models routinely prepend a
    sentence of commentary before the JSON object even when asked not
    to. Extracting the first balanced brace block recovers those
    cases. Returns ``None`` when neither strategy yields valid JSON.
    """
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    candidate = extract_first_json_object(body)
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def extract_first_json_object(body: str) -> str | None:
    """Return the first balanced ``{...}`` substring in ``body``, or None.

    Walks the string tracking brace depth, ignoring braces inside
    string literals (escape-aware). Stops at the first well-formed
    object and returns its source text. Doesn't validate that the
    extracted substring is parseable JSON -- the caller is expected
    to feed it through ``json.loads`` (parse_json_lenient does this).
    """
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(body):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start >= 0:
                return body[start : i + 1]
    return None


def strip_code_fences(text: str) -> str:
    """Drop a leading / trailing ```language ... ``` fence, if present.

    Tolerant of either ``` or ```json (or any other language tag) on
    the opening fence. Returns ``text`` unchanged when no fence is
    present, so it's safe to call unconditionally.
    """
    if not text.startswith("```"):
        return text
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    inner = text[first_nl + 1 :]
    inner = inner.removesuffix("```")
    return inner.strip()
