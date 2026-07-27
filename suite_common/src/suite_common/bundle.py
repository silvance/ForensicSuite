r"""Read the bundle's `version.json` stamp at runtime.

`prepare-bundle.ps1` writes a `version.json` at the root of the
air-gapped bundle (alongside `start-suite.ps1`, `Inscription\`,
`CaseForge\`, etc.) carrying:

    {
      "bundle_format_version": 1,
      "build_timestamp": "2026-05-03T00:00:00Z",
      "git_sha": "abcdef1234...",
      "git_branch": "main",
      "models": ["gemma4:latest", "granite4:tiny-h"]
    }

When an app is launched from inside that bundle, it can surface those
values in its About dialog so an operator knows *which* build is in
front of them. From a source checkout, no `version.json` exists and
:func:`read_version_info` returns ``None`` -- About dialogs fall back
to the package version they already display.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

#: Maximum size of a version.json we'll parse. The genuine file sits
#: well under 1 KB; capping keeps a corrupt or hostile version of the
#: file from costing memory.
_MAX_VERSION_BYTES: Final = 64 * 1024


def bundle_root() -> Path | None:
    """Return the air-gapped bundle root, or ``None`` from a dev checkout.

    The bundle layout (set up by `package-airgapped.ps1` +
    `install.ps1`) places each app's PyInstaller one-folder bundle as a
    sibling of `version.json`:

        InscriptionSuite/
        |-- Inscription/Inscription.exe
        |-- CaseForge/CaseForge.exe
        |-- CaseGuide/CaseGuide.exe
        |-- ollama/...
        |-- models/...
        |-- start-suite.ps1
        |-- install.ps1
        +-- version.json    <- this is what we resolve

    When an app's frozen `.exe` is running, ``sys.executable`` points
    at e.g. `<root>/Inscription/Inscription.exe`, so the bundle root
    is two parents up. From a dev checkout (`python -m inscription`),
    ``sys.frozen`` is unset and we return ``None`` so callers fall
    back to the package version they already had.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable).resolve()
    candidate = exe.parent.parent
    # Be defensive: only return the candidate if it actually has a
    # version.json. PyInstaller specs that nest the .exe deeper would
    # otherwise return a wrong answer.
    if (candidate / "version.json").is_file():
        return candidate
    return None


def read_version_info() -> dict | None:
    """Return the parsed `version.json` dict, or ``None``.

    Returns ``None`` when:
      - we're running from a source checkout (no bundle),
      - the bundle was built before the version-stamp feature,
      - the file is missing, oversized, or unparseable.

    Best-effort: any failure logs at debug and returns ``None`` rather
    than raising, so an About dialog never crashes on a malformed
    version.json. Callers are expected to handle ``None``.
    """
    root = bundle_root()
    if root is None:
        return None
    target = root / "version.json"
    try:
        size = target.stat().st_size
    except OSError as exc:
        logger.debug("version.json stat failed: %s", exc)
        return None
    if size > _MAX_VERSION_BYTES:
        logger.debug("version.json is %d bytes; refusing to load", size)
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.debug("version.json parse failed: %s", exc)
        return None
    if not isinstance(raw, dict):
        logger.debug("version.json top-level is not an object")
        return None
    return raw
