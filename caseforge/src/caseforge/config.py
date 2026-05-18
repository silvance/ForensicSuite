"""Typed wrapper around ``QSettings`` for CaseForge configuration.

Backed by an INI file at :data:`caseforge.paths.CONFIG_FILE` so it can
be inspected outside the app when needed. Persists examiner identity
defaults (auto-fill on every new case), workspace root, and the path
to the Inscription executable for the launcher.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from PySide6.QtCore import QByteArray, QSettings

from caseforge.paths import CONFIG_FILE, WORKSPACE_DIR

_K_WINDOW_GEOMETRY: Final = "window/geometry"
_K_WINDOW_STATE: Final = "window/state"
_K_WORKSPACE_ROOT: Final = "storage/workspace_root"
_K_EXAMINER_NAME: Final = "examiner/name"
_K_EXAMINER_ORG: Final = "examiner/org"
_K_EXAMINER_BADGE: Final = "examiner/badge_id"
_K_INSCRIPTION_PATH: Final = "launcher/inscription_path"
_K_CASEGUIDE_PATH: Final = "launcher/caseguide_path"
_K_RECENT_CASE_PATHS: Final = "browser/recent_case_paths"
_K_ONBOARDING_COMPLETED: Final = "onboarding/completed"


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

    # ----------------------------------------------------------- storage

    @property
    def workspace_root(self) -> Path:
        val = self._qs.value(_K_WORKSPACE_ROOT, "")
        return Path(str(val)) if val else WORKSPACE_DIR

    @workspace_root.setter
    def workspace_root(self, value: Path) -> None:
        self._qs.setValue(_K_WORKSPACE_ROOT, str(value))

    @property
    def has_explicit_workspace(self) -> bool:
        """True if the operator has explicitly set a workspace path.

        ``workspace_root`` falls back to the LOCALAPPDATA default when
        unset, so it can't be used as the "is it configured?" signal —
        the onboarding dialog needs to know whether to suggest a more
        visible default like ``~/Documents/CaseForge``.
        """
        return bool(str(self._qs.value(_K_WORKSPACE_ROOT, "")))

    # ---------------------------------------------------------- examiner

    @property
    def examiner_name(self) -> str:
        return str(self._qs.value(_K_EXAMINER_NAME, ""))

    @examiner_name.setter
    def examiner_name(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_NAME, value)

    @property
    def examiner_org(self) -> str:
        return str(self._qs.value(_K_EXAMINER_ORG, ""))

    @examiner_org.setter
    def examiner_org(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_ORG, value)

    @property
    def examiner_badge(self) -> str:
        return str(self._qs.value(_K_EXAMINER_BADGE, ""))

    @examiner_badge.setter
    def examiner_badge(self, value: str) -> None:
        self._qs.setValue(_K_EXAMINER_BADGE, value)

    # ---------------------------------------------------------- launcher

    @property
    def inscription_path(self) -> str:
        """Absolute path to the Inscription launcher (exe or script).

        Empty means "fall back to ``python -m inscription``" — useful
        in development. The launcher logs which path it picked so the
        examiner can spot misconfigurations.
        """
        return str(self._qs.value(_K_INSCRIPTION_PATH, ""))

    @inscription_path.setter
    def inscription_path(self, value: str) -> None:
        self._qs.setValue(_K_INSCRIPTION_PATH, value)

    @property
    def caseguide_path(self) -> str:
        """Absolute path to the CaseGuide launcher (exe or script).

        Same fall-through rules as :attr:`inscription_path`.
        """
        return str(self._qs.value(_K_CASEGUIDE_PATH, ""))

    @caseguide_path.setter
    def caseguide_path(self, value: str) -> None:
        self._qs.setValue(_K_CASEGUIDE_PATH, value)

    # ----------------------------------------------------------- browser

    @property
    def recent_case_paths(self) -> list[str]:
        """Most-recently-opened case directories (paths only, newest first).

        Stored as a JSON-encoded list. The previous format was a
        ``;``-separated string, but ``;`` is a legal character in
        Windows file/directory names ("jan;feb-2026"), and a path
        containing ``;`` would split into broken fragments on the next
        read. JSON sidesteps that and survives INI round-tripping.

        Legacy ``;``-separated values written by older builds are read
        back via the fallback below; the first write after upgrade
        re-emits as JSON.
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
        """Push ``case_path`` to the head of the recents list."""
        existing = [p for p in self.recent_case_paths if p != case_path]
        self.recent_case_paths = [case_path, *existing][:limit]

    # -------------------------------------------------------- onboarding

    @property
    def onboarding_completed(self) -> bool:
        """True once the operator has finished or skipped the first-run dialog.

        Stored as a string ("true"/"false") because QSettings round-trips
        booleans as strings through INI files. We treat the unset case as
        False so a fresh install always sees the welcome dialog once.
        """
        raw = str(self._qs.value(_K_ONBOARDING_COMPLETED, "")).strip().lower()
        return raw == "true"

    @onboarding_completed.setter
    def onboarding_completed(self, value: bool) -> None:
        self._qs.setValue(_K_ONBOARDING_COMPLETED, "true" if value else "false")

    # ------------------------------------------------------- persistence

    def sync(self) -> None:
        self._qs.sync()
