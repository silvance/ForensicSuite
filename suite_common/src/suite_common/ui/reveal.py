"""Cross-platform "show in file manager" helper.

Forensic operators frequently need to find the file they just
exported -- to attach it to a discovery email, zip it up, or hand
the directory to a colleague. The "Exported to <full path>" string
in a confirmation dialog tells them WHERE the file is, but they
still have to navigate there manually.

``reveal_in_file_manager`` opens the file's containing folder in
the OS file manager, with the file selected where the platform's
manager supports it. The operation is best-effort: a missing
``explorer.exe`` / ``open`` / ``xdg-open`` is logged and treated as
"no-op" rather than raised, so a controller that calls this from a
dialog button doesn't need to handle exceptions.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def reveal_in_file_manager(path: Path) -> bool:
    """Open ``path``'s containing folder in the OS file manager.

    On Windows, selects the file in Explorer.
    On macOS, reveals the file in Finder.
    On Linux, opens the parent folder (xdg-open doesn't select).

    Returns True when the launch succeeded, False otherwise. Never
    raises -- this is a UX nicety, not a critical path.
    """
    target = Path(path)
    if not target.exists():
        # The most common failure mode: the export the operator just
        # ran was successful but the file is on a network share that
        # hasn't synced yet, OR they're being asked to reveal a path
        # we built from stale state. Log and decline.
        logger.warning("reveal_in_file_manager: %s does not exist", target)
        return False

    if sys.platform == "win32":
        return _reveal_windows(target)
    if sys.platform == "darwin":
        return _reveal_macos(target)
    return _reveal_linux(target)


def _reveal_windows(target: Path) -> bool:
    """Explorer ``/select`` highlights the target file in its parent."""
    try:
        # Use ``str()`` not ``os.fspath`` because Explorer.exe is
        # picky about path normalisation -- forward slashes work in
        # Python's API but Explorer wants backslashes.
        subprocess.Popen(
            ["explorer.exe", "/select,", str(target)],
        )
    except OSError as exc:
        logger.warning("Could not launch Explorer: %s", exc)
        return False
    return True


def _reveal_macos(target: Path) -> bool:
    """``open -R`` reveals the file in Finder."""
    try:
        subprocess.Popen(
            ["open", "-R", str(target)],
        )
    except OSError as exc:
        logger.warning("Could not launch Finder: %s", exc)
        return False
    return True


def _reveal_linux(target: Path) -> bool:
    """xdg-open opens the parent folder; no standard way to select."""
    if shutil.which("xdg-open") is None:
        logger.warning("xdg-open not on PATH; cannot reveal %s", target)
        return False
    try:
        subprocess.Popen(
            ["xdg-open", str(target.parent)],
        )
    except OSError as exc:
        logger.warning("Could not launch xdg-open: %s", exc)
        return False
    return True
