"""LLMClient: HTTP against a stdlib http.server fixture.

The real thing talks to Ollama / LM Studio. We spin up a tiny handler
that returns a canned OpenAI-compatible response (or an error) and
assert the client parses it, surfaces useful errors, and bails cleanly
on timeouts.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

import pytest

from suite_common.llm import (
    LLMClient,
    LLMConfigError,
    LLMRequestError,
    LLMResponseError,
    available_models_hint,
    list_available_models,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _ok_response(content: str) -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": content}, "index": 0}],
    }


@contextmanager
def _server(
    handler_fn: Callable[[BaseHTTPRequestHandler], None],
) -> Iterator[str]:
    """Run a single-threaded HTTP server and yield its base URL."""

    class _H(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            handler_fn(self)

        def log_message(self, *args: object) -> None:  # silence stderr
            return

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), _H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _send_json(handler: BaseHTTPRequestHandler, status: int, body: dict[str, object]) -> None:
    payload = json.dumps(body).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


# ----------------------------------------------------------- happy path


def test_client_returns_assistant_content() -> None:
    captured: dict[str, object] = {}

    def handler(h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers["Content-Length"])
        captured["body"] = json.loads(h.rfile.read(length))
        captured["auth"] = h.headers.get("Authorization")
        _send_json(h, 200, _ok_response('{"steps":[]}'))

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="granite3.3:8b", timeout_s=5, api_key="sk-x")
        content = client.chat(system="sys", user="usr")

    assert content == '{"steps":[]}'
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "granite3.3:8b"
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["content"] == "usr"
    assert body["response_format"] == {"type": "json_object"}
    assert captured["auth"] == "Bearer sk-x"


# ----------------------------------------------------------- error paths


def test_client_surfaces_http_5xx_as_request_error() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 500, {"error": "boom"})

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="m", timeout_s=5)
        with pytest.raises(LLMRequestError, match="500"):
            client.chat(system="s", user="u")


def test_client_surfaces_connection_refused() -> None:
    # Bind then close a port to guarantee it's unoccupied.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    client = LLMClient(base_url=f"http://127.0.0.1:{port}/v1", model="m", timeout_s=2)
    with pytest.raises(LLMRequestError):
        client.chat(system="s", user="u")


def test_client_surfaces_malformed_json() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        payload = b"not a json body"
        h.send_response(200)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="m", timeout_s=5)
        with pytest.raises(LLMResponseError, match="non-JSON"):
            client.chat(system="s", user="u")


def test_client_surfaces_missing_choices() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 200, {"unexpected": "shape"})

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="m", timeout_s=5)
        with pytest.raises(LLMResponseError, match="missing"):
            client.chat(system="s", user="u")


def test_client_surfaces_empty_content() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 200, _ok_response(""))

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="m", timeout_s=5)
        with pytest.raises(LLMResponseError, match="empty"):
            client.chat(system="s", user="u")


# ----------------------------------------------------------- config


def test_empty_base_url_rejected() -> None:
    with pytest.raises(LLMConfigError):
        LLMClient(base_url="", model="m", timeout_s=5)


def test_empty_model_rejected() -> None:
    with pytest.raises(LLMConfigError):
        LLMClient(base_url="http://x/v1", model="", timeout_s=5)


def test_bad_timeout_rejected() -> None:
    with pytest.raises(LLMConfigError):
        LLMClient(base_url="http://x/v1", model="m", timeout_s=0)


# ----------------------------------------------------------- timeout


def test_client_times_out_on_slow_server() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        time.sleep(0.8)
        _send_json(h, 200, _ok_response("{}"))

    with _server(handler) as url:
        client = LLMClient(base_url=url, model="m", timeout_s=0.15)
        with pytest.raises(LLMRequestError, match="timed out"):
            client.chat(system="s", user="u")


# ----------------------------------------------------------- list_available_models


@contextmanager
def _models_server(get_handler: Callable[[BaseHTTPRequestHandler], None]) -> Iterator[str]:
    """Sister fixture to ``_server`` but routing GET (the /models verb)."""

    class _H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            get_handler(self)

        def log_message(self, *args: object) -> None:
            return

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = HTTPServer(("127.0.0.1", port), _H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_list_available_models_returns_sorted_unique_ids() -> None:
    captured: dict[str, object] = {}

    def handler(h: BaseHTTPRequestHandler) -> None:
        captured["path"] = h.path
        captured["auth"] = h.headers.get("Authorization")
        body = {
            "object": "list",
            "data": [
                {"id": "gemma4:latest"},
                {"id": "granite4:tiny-h"},
                {"id": "gemma4:latest"},  # duplicate, should dedupe
                {"id": ""},                # empty id, should drop
                {"not_id": "ignored"},     # malformed entry, should drop
            ],
        }
        _send_json(h, 200, body)

    with _models_server(handler) as url:
        ids = list_available_models(base_url=url, api_key="sk-x", timeout_s=2)

    assert ids == ["gemma4:latest", "granite4:tiny-h"]
    assert captured["path"] == "/v1/models"
    assert captured["auth"] == "Bearer sk-x"


def test_list_available_models_raises_on_http_error() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 500, {"error": "down"})

    with _models_server(handler) as url, pytest.raises(LLMRequestError):
        list_available_models(base_url=url, timeout_s=2)


def test_list_available_models_raises_on_malformed_body() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 200, {"unexpected": "shape"})

    with _models_server(handler) as url, pytest.raises(LLMResponseError):
        list_available_models(base_url=url, timeout_s=2)


def test_list_available_models_validates_base_url() -> None:
    with pytest.raises(LLMConfigError):
        list_available_models(base_url="", timeout_s=2)
    with pytest.raises(LLMConfigError):
        list_available_models(base_url="file:///etc/passwd", timeout_s=2)


# ---- available_models_hint -----------------------------------------------


def test_available_models_hint_names_the_models() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(
            h, 200,
            {"data": [{"id": "qwen2.5:7b-instruct-q5_K_M"}, {"id": "granite4:tiny-h"}]},
        )

    with _models_server(handler) as url:
        hint = available_models_hint(url, timeout_s=2)

    assert "Models this server does have:" in hint
    assert "qwen2.5:7b-instruct-q5_K_M" in hint
    assert "granite4:tiny-h" in hint


def test_available_models_hint_elides_long_lists() -> None:
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 200, {"data": [{"id": f"model-{i:02d}"} for i in range(9)]})

    with _models_server(handler) as url:
        hint = available_models_hint(url, timeout_s=2)

    assert "model-05" in hint
    assert "model-06" not in hint
    assert "(3 more)" in hint


def test_available_models_hint_swallows_every_failure() -> None:
    """The hint is built INSIDE error handling -- it must never raise,
    and it must degrade to empty rather than inject noise."""
    # Server unreachable (nothing listens on this port).
    assert available_models_hint("http://127.0.0.1:1/v1", timeout_s=0.2) == ""
    # Config-invalid URL.
    assert available_models_hint("", timeout_s=1) == ""

    # Server reachable but the store is empty.
    def handler(h: BaseHTTPRequestHandler) -> None:
        _send_json(h, 200, {"data": []})

    with _models_server(handler) as url:
        assert available_models_hint(url, timeout_s=2) == ""
