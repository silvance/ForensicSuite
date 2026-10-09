"""Typed wrapper around ``QSettings`` for Inscription configuration.

Settings live in an INI file at :data:`inscription.paths.CONFIG_FILE` so
they can be inspected and edited outside the application when needed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, QSettings

from inscription.paths import CONFIG_FILE, WORKSPACE_DIR

#: Env var the air-gapped launcher sets to share one model choice across
#: the suite. Empty string is treated as "not set" so a stale unset
#: shell variable doesn't accidentally override the bundled default.
_ENV_SUITE_LLM_MODEL: Final = "SUITE_LLM_MODEL"
#: Env var the air-gapped launcher sets to point apps at the bundled
#: Ollama instance (which lives on a non-default port -- 11435 -- so
#: it never collides with a system-wide Ollama). Same empty-string
#: semantics as SUITE_LLM_MODEL.
_ENV_SUITE_LLM_BASE_URL: Final = "SUITE_LLM_BASE_URL"

_K_WINDOW_GEOMETRY: Final = "window/geometry"
_K_WINDOW_STATE: Final = "window/state"
_K_WORKSPACE_ROOT: Final = "storage/workspace_root"
_K_THEME: Final = "ui/theme"
_K_LLM_ENABLED: Final = "llm/enabled"
_K_LLM_BASE_URL: Final = "llm/base_url"
_K_LLM_MODEL: Final = "llm/model"
_K_LLM_TIMEOUT_S: Final = "llm/timeout_s"
_K_LLM_API_KEY: Final = "llm/api_key"
_K_CAPTURE_AUTO_SCREENSHOT: Final = "capture/auto_screenshot"
_K_EXAMINER_NAME: Final = "examiner/name"
_K_EXAMINER_ORG: Final = "examiner/org"
_K_EXAMINER_ID: Final = "examiner/id"
_K_TRAY_CLOSE_HINT_SHOWN: Final = "tray/close_hint_shown"
_K_MINI_DOCK_POSITION: Final = "tray/mini_dock_position"

# Defaults are shared with CaseGuide via suite_common; re-exported
# here so callers that already imported them from inscription.config
# (the test suite, settings_dialog, …) keep working unchanged.
from suite_common.llm import (  # noqa: E402
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT_S,
    llm_setting_to_store,
    resolve_llm_setting,
)


class Config:
    """Typed access to user configuration, backed by a QSettings INI file."""

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

    # ----------------------------------------------------------- storage

    @property
    def workspace_root(self) -> Path:
        """Root directory for all session folders on this machine."""
        val = self._qs.value(_K_WORKSPACE_ROOT, "")
        return Path(str(val)) if val else WORKSPACE_DIR

    @workspace_root.setter
    def workspace_root(self, value: Path) -> None:
        self._qs.setValue(_K_WORKSPACE_ROOT, str(value))

    # ----------------------------------------------------------- capture

    @property
    def auto_screenshot(self) -> bool:
        """Whether ClickSource and WindowFocusSource grab a screenshot
        on every event.

        Default is True (the original behaviour). When False, only the
        manual snapshot hotkey produces images — useful for forensic
        workflows where the examiner wants screenshots only at moments
        they've already decided are evidentiary, and for keeping session
        sizes lean.
        """
        return _as_bool(self._qs.value(_K_CAPTURE_AUTO_SCREENSHOT, True))

    @auto_screenshot.setter
    def auto_screenshot(self, value: bool) -> None:
        self._qs.setValue(_K_CAPTURE_AUTO_SCREENSHOT, bool(value))

    # ---------------------------------------------------------------- ui

    @property
    def theme(self) -> str:
        return str(self._qs.value(_K_THEME, "system"))

    @theme.setter
    def theme(self, value: str) -> None:
        self._qs.setValue(_K_THEME, value)

    # --------------------------------------------------------------- llm

    @property
    def llm_enabled(self) -> bool:
        return _as_bool(self._qs.value(_K_LLM_ENABLED, False))

    @llm_enabled.setter
    def llm_enabled(self, value: bool) -> None:
        self._qs.setValue(_K_LLM_ENABLED, bool(value))

    def _store_llm_setting(
        self, key: str, value: str, *, env_var: str, builtin_default: str
    ) -> None:
        """Persist only genuine overrides; a value equal to the current
        default removes the key so the launcher's choice keeps flowing."""
        stored = llm_setting_to_store(value, env_var=env_var, builtin_default=builtin_default)
        if stored is None:
            self._qs.remove(key)
        else:
            self._qs.setValue(key, stored)

    @property
    def llm_base_url(self) -> str:
        return resolve_llm_setting(
            self._qs.value(_K_LLM_BASE_URL),
            env_var=_ENV_SUITE_LLM_BASE_URL,
            builtin_default=DEFAULT_LLM_BASE_URL,
        )

    @llm_base_url.setter
    def llm_base_url(self, value: str) -> None:
        self._store_llm_setting(
            _K_LLM_BASE_URL,
            value,
            env_var=_ENV_SUITE_LLM_BASE_URL,
            builtin_default=DEFAULT_LLM_BASE_URL,
        )

    @property
    def llm_model(self) -> str:
        return resolve_llm_setting(
            self._qs.value(_K_LLM_MODEL),
            env_var=_ENV_SUITE_LLM_MODEL,
            builtin_default=DEFAULT_LLM_MODEL,
        )

    @llm_model.setter
    def llm_model(self, value: str) -> None:
        self._store_llm_setting(
            _K_LLM_MODEL, value, env_var=_ENV_SUITE_LLM_MODEL, builtin_default=DEFAULT_LLM_MODEL
        )

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
        """Optional bearer token. Local Ollama/LM Studio ignore it; set
        this only when pointing at a remote OpenAI-compatible service.
        """
        val = self._qs.value(_K_LLM_API_KEY, "")
        return str(val) if val else None

    @llm_api_key.setter
    def llm_api_key(self, value: str | None) -> None:
        self._qs.setValue(_K_LLM_API_KEY, value or "")

    # ----------------------------------------------------------- examiner

    @property
    def examiner_name(self) -> str:
        """Examiner's display name; auto-fills the forensic-notes header."""
        return str(self._qs.value(_K_EXAMINER_NAME, ""))

    @examiner_name.setter
    def examiner_name(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_NAME, value)

    @property
    def examiner_org(self) -> str:
        """Examiner's organisation / unit (e.g. 'Cyber Crimes Unit')."""
        return str(self._qs.value(_K_EXAMINER_ORG, ""))

    @examiner_org.setter
    def examiner_org(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_ORG, value)

    @property
    def examiner_id(self) -> str:
        """Examiner's badge / employee ID — printed on the notes header."""
        return str(self._qs.value(_K_EXAMINER_ID, ""))

    @examiner_id.setter
    def examiner_id(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_ID, value)

    def has_examiner_identity(self) -> bool:
        """True once the examiner has filled in at least their name."""
        return bool(self.examiner_name.strip())

    # --------------------------------------------------------- tray / dock

    @property
    def tray_close_hint_shown(self) -> bool:
        """First-close toast: shown once so the user knows the app is still
        running after they hit X (it minimised to the tray)."""
        return _as_bool(self._qs.value(_K_TRAY_CLOSE_HINT_SHOWN, False))

    @tray_close_hint_shown.setter
    def tray_close_hint_shown(self, value: bool) -> None:
        self._qs.setValue(_K_TRAY_CLOSE_HINT_SHOWN, bool(value))

    @property
    def mini_dock_position(self) -> tuple[int, int] | None:
        """Last-saved screen coordinates for the compact overlay."""
        raw = self._qs.value(_K_MINI_DOCK_POSITION, "")
        if not raw:
            return None
        text = str(raw)
        try:
            x_str, y_str = text.split(",", 1)
            return int(x_str), int(y_str)
        except ValueError:
            return None

    @mini_dock_position.setter
    def mini_dock_position(self, value: tuple[int, int] | None) -> None:
        if value is None:
            self._qs.setValue(_K_MINI_DOCK_POSITION, "")
        else:
            self._qs.setValue(_K_MINI_DOCK_POSITION, f"{int(value[0])},{int(value[1])}")

    # ------------------------------------------------------- persistence

    def sync(self) -> None:
        """Flush pending writes to disk."""
        self._qs.sync()


def _as_bool(raw: object) -> bool:
    """QSettings round-trips bools through strings; normalise back."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"true", "1", "yes", "on"}
    return bool(raw)
