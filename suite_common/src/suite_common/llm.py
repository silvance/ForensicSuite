"""OpenAI-compatible chat-completions client.

Stdlib-only — a thin synchronous ``urllib`` POST keeps the dependency
surface flat and matches the one-shot, non-streaming usage pattern both
Inscription's step rewriter and CaseGuide's suggestions refiner need.
Any OpenAI-compatible endpoint works: Ollama's ``/v1``, LM Studio's
server, ``llama.cpp --server``, or a remote provider.

Failures raise :class:`LLMError` (or a subclass) with a human-readable
message; callers show that to the user and fall back to deterministic
defaults.
"""

from __future__ import annotations

import json
import logging
import socket
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class ChatClient(Protocol):
    """Structural type for anything that can chat. Tests supply fakes."""

    def chat(self, *, system: str, user: str) -> str:  # pragma: no cover - protocol
        ...

DEFAULT_TEMPERATURE = 0.2

#: Default LLM endpoint — a local Ollama install on its standard port.
#: The air-gapped bundle's start-suite.ps1 sets SUITE_LLM_BASE_URL to
#: 11435 (its dedicated port) which each app's config layer prefers
#: over this default. Inscription and CaseGuide ship the same value
#: so a forensic operator's settings round-trip identically across
#: the two tools.
DEFAULT_LLM_BASE_URL = "http://localhost:11434/v1"

#: Default model tag pulled by the air-gapped bundle and used by both
#: the rewrite (Inscription) and refine (CaseGuide) flows when nothing
#: more specific is configured.
DEFAULT_LLM_MODEL = "gemma4:latest"

#: HTTP read timeout for chat-completions calls. Local CPU-only models
#: can take well over a minute on a long timeline; the default is set
#: high enough that the real failure mode is "model didn't start" or
#: "endpoint not reachable" rather than "we gave up too early".
DEFAULT_LLM_TIMEOUT_S = 600.0

#: Hard cap on the response body. A verbose chat-completion sits well
#: under 100 KB; we accept up to 10 MB so a chatty model on a huge input
#: can still complete, but a misbehaving / hostile endpoint returning
#: gigabytes of garbage doesn't OOM us.
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

#: Allowed URL schemes for the LLM endpoint. ``urlopen`` would happily
#: dispatch ``file://`` and ``ftp://`` requests; constrain to plain
#: HTTP/HTTPS so a config typo can't turn into local file disclosure.
_ALLOWED_SCHEMES = ("http://", "https://")


class LLMError(Exception):
    """Base class for LLM-related failures."""


class LLMConfigError(LLMError):
    """The client is misconfigured (empty base URL, bad timeout, etc.)."""


class LLMRequestError(LLMError):
    """The HTTP request itself failed (connection, timeout, HTTP 5xx)."""


class LLMResponseError(LLMError):
    """The endpoint responded but the body wasn't a usable chat completion."""


class LLMClient:
    """Minimal OpenAI-compatible chat-completions client."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        timeout_s: float,
        api_key: str | None = None,
    ) -> None:
        if not base_url:
            msg = "LLM base_url is empty"
            raise LLMConfigError(msg)
        if not base_url.lower().startswith(_ALLOWED_SCHEMES):
            msg = (
                f"LLM base_url must start with http:// or https:// (got {base_url!r})"
            )
            raise LLMConfigError(msg)
        if not model:
            msg = "LLM model is empty"
            raise LLMConfigError(msg)
        if timeout_s <= 0:
            msg = f"LLM timeout_s must be > 0 (got {timeout_s})"
            raise LLMConfigError(msg)
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._api_key = api_key

    def chat(
        self,
        *,
        system: str,
        user: str,
        json_mode: bool = True,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> str:
        """Send one chat completion. Returns the assistant's message content.

        ``json_mode`` sets ``response_format={"type": "json_object"}`` which
        Ollama, LM Studio, and OpenAI respect — the model is biased toward
        emitting strict JSON. The caller is still responsible for parsing.
        """
        body: dict[str, object] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "stream": False,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_s) as resp:
                # Read one byte past the cap to detect oversized
                # responses without first buffering them entirely.
                raw = resp.read(_MAX_RESPONSE_BYTES + 1)
                if len(raw) > _MAX_RESPONSE_BYTES:
                    msg = (
                        f"LLM response from {url} exceeds {_MAX_RESPONSE_BYTES} "
                        f"bytes; aborting."
                    )
                    raise LLMResponseError(msg)
        except urllib.error.HTTPError as exc:
            detail = _safe_body(exc.read() if exc.fp else b"")
            msg = f"LLM HTTP {exc.code} from {url}: {detail}"
            raise LLMRequestError(msg) from exc
        except urllib.error.URLError as exc:
            msg = f"LLM request to {url} failed: {exc.reason}"
            raise LLMRequestError(msg) from exc
        except TimeoutError as exc:
            msg = f"LLM request to {url} timed out after {self._timeout_s}s"
            raise LLMRequestError(msg) from exc
        except OSError as exc:
            if isinstance(exc, socket.timeout):
                msg = f"LLM request to {url} timed out after {self._timeout_s}s"
                raise LLMRequestError(msg) from exc
            msg = f"LLM request to {url} failed: {exc}"
            raise LLMRequestError(msg) from exc

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"LLM returned non-JSON from {url}: {_safe_body(raw)}"
            raise LLMResponseError(msg) from exc

        try:
            content = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            msg = f"LLM response missing choices[0].message.content: {parsed!r}"
            raise LLMResponseError(msg) from exc

        if not isinstance(content, str) or not content.strip():
            msg = f"LLM response content empty or non-string: {content!r}"
            raise LLMResponseError(msg)
        return content


def _safe_body(raw: bytes, *, limit: int = 500) -> str:
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - guarded best-effort decode for log/UI text
        return "<unreadable>"
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def list_available_models(
    *,
    base_url: str,
    timeout_s: float = 5.0,
    api_key: str | None = None,
) -> list[str]:
    """Return the model IDs the endpoint advertises.

    Hits the OpenAI-compatible ``GET /models`` endpoint Ollama, LM Studio
    and OpenAI all expose. Returns sorted ``model:tag`` strings on success;
    raises :class:`LLMError` on connection / HTTP / parse failures so the
    caller can decide whether to fall back to free-text entry.
    """
    if not base_url:
        msg = "LLM base_url is empty"
        raise LLMConfigError(msg)
    if not base_url.lower().startswith(_ALLOWED_SCHEMES):
        msg = f"LLM base_url must start with http:// or https:// (got {base_url!r})"
        raise LLMConfigError(msg)
    if timeout_s <= 0:
        msg = f"LLM timeout_s must be > 0 (got {timeout_s})"
        raise LLMConfigError(msg)

    url = f"{base_url.rstrip('/')}/models"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        url, headers=headers, method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                msg = (
                    f"LLM response from {url} exceeds {_MAX_RESPONSE_BYTES} "
                    f"bytes; aborting."
                )
                raise LLMResponseError(msg)
    except urllib.error.HTTPError as exc:
        detail = _safe_body(exc.read() if exc.fp else b"")
        msg = f"LLM HTTP {exc.code} from {url}: {detail}"
        raise LLMRequestError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"LLM request to {url} failed: {exc.reason}"
        raise LLMRequestError(msg) from exc
    except TimeoutError as exc:
        msg = f"LLM request to {url} timed out after {timeout_s}s"
        raise LLMRequestError(msg) from exc
    except OSError as exc:
        if isinstance(exc, socket.timeout):
            msg = f"LLM request to {url} timed out after {timeout_s}s"
            raise LLMRequestError(msg) from exc
        msg = f"LLM request to {url} failed: {exc}"
        raise LLMRequestError(msg) from exc

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        msg = f"LLM returned non-JSON from {url}: {_safe_body(raw)}"
        raise LLMResponseError(msg) from exc

    data = parsed.get("data") if isinstance(parsed, dict) else None
    if not isinstance(data, list):
        msg = f"LLM /models response missing 'data' list: {parsed!r}"
        raise LLMResponseError(msg)
    ids: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid.strip():
                ids.append(mid.strip())
    return sorted(set(ids))


#: Cap on model names listed inline in an error dialog before eliding.
_HINT_MAX_MODELS = 6


def available_models_hint(base_url: str, *, timeout_s: float = 2.0) -> str:
    """Dialog fragment naming the models the endpoint DOES advertise.

    Built for model-not-found error paths: a 404 proves the server is
    reachable, so a quick ``/models`` probe can turn "your model isn't
    here" into "here's what is" -- the difference between a dead end
    and a fix the operator can apply in Settings. Returns ``""`` on
    any failure (server died between the 404 and this probe, empty
    store) so callers can never crash inside their error handling;
    the short timeout keeps a GUI-thread caller responsive.
    """
    try:
        models = list_available_models(base_url=base_url, timeout_s=timeout_s)
    except Exception:  # noqa: BLE001 - best-effort hint; never raise from error paths
        return ""
    if not models:
        return ""
    shown = ", ".join(models[:_HINT_MAX_MODELS])
    if len(models) > _HINT_MAX_MODELS:
        shown += f", … ({len(models) - _HINT_MAX_MODELS} more)"
    return f"Models this server does have: {shown}\n\n"
