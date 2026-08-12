"""Translate raw LLM exception text into operator-readable guidance.

Lives next to :mod:`inscription.ui.controller` and is used solely by
its rewrite-failure path. Lifted out of ``controller.py`` so the
pattern table is easy to extend without touching the orchestrator,
and so the unit tests in ``tests/test_friendly_llm_error.py`` keep
importing a stable path even as the controller itself evolves.

Each branch maps to a real failure mode the field has hit:
connection refused (Ollama isn't running), timed out (model too big
for the hardware), 404 / model-not-found (configured model isn't
pulled), schema mismatch (model returned JSON in the wrong shape
even after the automatic retry).
"""

from __future__ import annotations

from suite_common.llm import available_models_hint


def friendly_llm_error(raw_message: str, *, base_url: str) -> str:
    """Translate raw LLM exception text into a guided message.

    The local-LLM-not-running case is the dominant failure mode in
    the field — Ollama or LM Studio just isn't started. Catch the
    connection refused / unreachable patterns and tell the user what
    to do, rather than dumping a urllib stacktrace at them.

    Each schema-mismatch branch keeps the raw payload OUT of the
    returned string -- the full reply is already in the log via
    ``logger.exception`` in :class:`RewriteWorker`, so the dialog
    just needs to tell the operator what to try next rather than
    paste 500 chars of dict repr at them.
    """
    lower = raw_message.lower()
    if "connection refused" in lower or "failed to establish" in lower:
        return (
            f"Couldn't reach the local LLM server at {base_url}.\n\n"
            "Start Ollama (or LM Studio / llama.cpp --server) and try "
            "again. If it's running on a different URL or port, open "
            "Edit → Settings → LLM and use 'Test connection' to verify.\n\n"
            f"Original error: {raw_message}"
        )
    if "timed out" in lower:
        return (
            "The LLM took too long to respond.\n\n"
            "On a local model this usually means the model is large for "
            "your hardware. Edit → Settings → LLM lets you raise the "
            "timeout or switch to a smaller model.\n\n"
            f"Original error: {raw_message}"
        )
    if "http 404" in lower or "model not found" in lower or "no such model" in lower:
        # A 404 means the server itself answered, so a quick probe can
        # name the models it does carry -- turning a dead-end dialog
        # into a fix the operator can apply immediately.
        hint = available_models_hint(base_url)
        action = (
            "Switch to one of those in Edit → Settings → LLM, or pull "
            "the missing model (e.g. `ollama pull <name>`).\n\n"
            if hint
            else "Pull it (e.g. `ollama pull <name>`) or change the "
            "model name in Edit → Settings → LLM.\n\n"
        )
        return (
            "The configured model isn't available on the LLM server.\n\n"
            + hint
            + action
            + f"Original error: {raw_message}"
        )
    if (
        "missing top-level 'steps' key" in lower
        or "did not return json" in lower
        or "'steps' must be an array" in lower
        or "zero usable steps" in lower
    ):
        return (
            "The model returned JSON in an unexpected shape, even after "
            "an automatic retry.\n\n"
            "Smaller / less instruction-tuned local models sometimes "
            "drift on the output schema. Try the same Rewrite again, or "
            "switch to a stronger model in Edit → Settings → LLM. The "
            "full payload is in the log file (Help → Show logs folder)."
        )
    return raw_message


# Tests in ``tests/test_friendly_llm_error.py`` import the underscored
# name; alias it so they keep working without churn during the
# extract-from-controller refactor.
_friendly_llm_error = friendly_llm_error
