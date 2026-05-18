"""Typed wrapper around ``QSettings`` for CaseGuide configuration.

Mirrors Inscription's config pattern so the LLM endpoint settings line
up — same defaults pointing at Ollama on localhost, same examiner
expectation.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, QSettings

from caseguide.paths import CONFIG_FILE

#: Env var the air-gapped launcher sets to share one model choice across
#: the suite. Empty string is treated as "not set".
_ENV_SUITE_LLM_MODEL: Final = "SUITE_LLM_MODEL"
#: Env var the air-gapped launcher sets to point apps at the bundled
#: Ollama (which lives on a non-default port -- 11435 -- so it never
#: collides with a system-wide Ollama).
_ENV_SUITE_LLM_BASE_URL: Final = "SUITE_LLM_BASE_URL"

_K_WINDOW_GEOMETRY: Final = "window/geometry"
_K_WINDOW_STATE: Final = "window/state"
_K_LLM_BASE_URL: Final = "llm/base_url"
_K_LLM_MODEL: Final = "llm/model"
_K_LLM_TIMEOUT_S: Final = "llm/timeout_s"
_K_LLM_API_KEY: Final = "llm/api_key"
_K_RECENT_CASE_PATHS: Final = "browser/recent_case_paths"

# Defaults are shared with Inscription via suite_common; re-exported
# here so callers that already imported them from caseguide.config
# (the test suite, settings_dialog, …) keep working unchanged.
from suite_common.llm import (  # noqa: E402
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT_S,
)


def _bundled_default_model() -> str:
    """Resolve the model the apps fall back to when the user hasn't picked one.

    The air-gapped launcher sets ``SUITE_LLM_MODEL`` after asking the
    operator which bundled model to use.
    """
    return os.environ.get(_ENV_SUITE_LLM_MODEL, "").strip() or DEFAULT_LLM_MODEL


def _bundled_default_base_url() -> str:
    """Resolve the LLM endpoint URL the apps fall back to.

    The air-gapped launcher sets ``SUITE_LLM_BASE_URL`` to point at the
    bundled Ollama on its dedicated non-default port so the apps don't
    accidentally talk to a system-wide Ollama on 11434.
    """
    return os.environ.get(_ENV_SUITE_LLM_BASE_URL, "").strip() or DEFAULT_LLM_BASE_URL


class Config:
    """Typed access to user configuration."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or CONFIG_FILE
        self._qs = QSettings(str(self._path), QSettings.Format.IniFormat)

    # ------------------------------------------------------------ window

    @property
    def window_geometry(self) -> QByteArray | None:
        val = self._qs.value(_K_WINDOW_GEOMETRY)
        if isinstance(val, QByteArray) and not val.isEmpty():
            return val
        return None

    @window_geometry.setter
    def window_geometry(self, value: QByteArray) -> None:
        self._qs.setValue(_K_WINDOW_GEOMETRY, value)

    @property
    def window_state(self) -> QByteArray | None:
        val = self._qs.value(_K_WINDOW_STATE)
        if isinstance(val, QByteArray) and not val.isEmpty():
            return val
        return None

    @window_state.setter
    def window_state(self, value: QByteArray) -> None:
        self._qs.setValue(_K_WINDOW_STATE, value)

    # --------------------------------------------------------------- llm

    @property
    def llm_base_url(self) -> str:
        return str(self._qs.value(_K_LLM_BASE_URL, _bundled_default_base_url()))

    @llm_base_url.setter
    def llm_base_url(self, value: str) -> None:
        self._qs.setValue(_K_LLM_BASE_URL, value)

    @property
    def llm_model(self) -> str:
        return str(self._qs.value(_K_LLM_MODEL, _bundled_default_model()))

    @llm_model.setter
    def llm_model(self, value: str) -> None:
        self._qs.setValue(_K_LLM_MODEL, value)

    @property
    def llm_timeout_s(self) -> float:
        raw = self._qs.value(_K_LLM_TIMEOUT_S, DEFAULT_LLM_TIMEOUT_S)
        if isinstance(raw, (int, float)):
            return float(raw)
        if isinstance(raw, str):
            try:
                return float(raw)
            except ValueError:
                pass
        return DEFAULT_LLM_TIMEOUT_S

    @llm_timeout_s.setter
    def llm_timeout_s(self, value: float) -> None:
        self._qs.setValue(_K_LLM_TIMEOUT_S, float(value))

    @property
    def llm_api_key(self) -> str | None:
        val = self._qs.value(_K_LLM_API_KEY, "")
        return str(val) if val else None

    @llm_api_key.setter
    def llm_api_key(self, value: str | None) -> None:
        self._qs.setValue(_K_LLM_API_KEY, value or "")

    # ----------------------------------------------------------- browser

    @property
    def recent_case_paths(self) -> list[str]:
        """Most-recently-opened case directories (paths only, newest first).

        JSON-encoded list. The legacy ``;``-separated format split on a
        character that's legal in Windows directory names; JSON
        round-trips cleanly. Legacy values are still read via the
        fallback below.
        """
        raw = str(self._qs.value(_K_RECENT_CASE_PATHS, ""))
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return [p for p in raw.split(";") if p]
        if not isinstance(parsed, list):
            return []
        return [str(p) for p in parsed if p]

    @recent_case_paths.setter
    def recent_case_paths(self, value: list[str]) -> None:
        self._qs.setValue(_K_RECENT_CASE_PATHS, json.dumps(list(value)))

    def remember_case(self, case_path: str, *, limit: int = 12) -> None:
        existing = [p for p in self.recent_case_paths if p != case_path]
        self.recent_case_paths = [case_path, *existing][:limit]

    # ------------------------------------------------------- persistence

    def sync(self) -> None:
        self._qs.sync()
