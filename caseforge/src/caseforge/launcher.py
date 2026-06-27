"""Launch sibling suite tools (Inscription, CaseGuide) against a case directory.

Both tools resolve the same way:

1. The explicit path from CaseForge's config (``inscription_path`` /
   ``caseguide_path``).
2. The tool's exe on ``PATH``.
3. A sibling app folder relative to the running .exe — only when frozen
   into the air-gapped PyInstaller bundle. Layout:
   ``<bundle>\\Inscription\\Inscription.exe`` / ``CaseGuide\\CaseGuide.exe``.
4. ``python -m <module>`` in the same Python that's running CaseForge
   (development fall-back).

Returns a :class:`LaunchResult` describing what was tried and what
ran. Failures don't raise -- the UI wants a friendly message either way.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class LaunchResult:
    """Outcome of one launch attempt against a sibling suite tool."""

    ok: bool
    command: list[str]
    message: str
    tool_label: str = ""


#: Bundle-folder name for each module. ``str.capitalize`` is wrong
#: here -- ``"caseguide".capitalize()`` is ``"Caseguide"``, but the
#: PyInstaller spec stages the bundle as ``CaseGuide\CaseGuide.exe``.
#: Windows' case-insensitive filesystem hides the mismatch in
#: practice, but a case-sensitive volume (Linux, an NTFS volume with
#: per-folder case-sensitivity enabled) would silently miss the
#: sibling exe and fall through to ``python -m <module>``.
_FROZEN_BUNDLE_NAMES = {
    "inscription": "Inscription",
    "caseguide": "CaseGuide",
}


def _frozen_sibling_exe(module_name: str) -> Path | None:
    """Find a sibling app's .exe in the air-gapped bundle, if we're frozen.

    The PyInstaller spec emits each app under its own one-folder bundle,
    and ``package-airgapped.ps1`` stages them as siblings under
    ``InscriptionSuite-Airgapped\\``. Resolving relative to the running
    ``CaseForge.exe`` finds the matching ``Inscription\\Inscription.exe``
    or ``CaseGuide\\CaseGuide.exe`` without the operator setting a path.
    Returns ``None`` outside a PyInstaller bundle so dev runs fall through
    to ``python -m <module>`` as before.
    """
    if not getattr(sys, "frozen", False):
        return None
    bundle_name = _FROZEN_BUNDLE_NAMES.get(module_name)
    if bundle_name is None:
        return None
    bundle_root = Path(sys.executable).resolve().parent.parent
    candidate = bundle_root / bundle_name / f"{bundle_name}.exe"
    if candidate.is_file():
        return candidate
    return None


def build_command(*, executable_path: str, module_name: str, case_dir: Path) -> list[str]:
    """Compose the argv that should run a tool against ``case_dir``.

    ``executable_path`` of ``""`` triggers the PATH-then-bundle-then-
    python-module fall-through. Pure helper so unit tests can exercise
    the resolution order without spawning subprocesses.

    An explicit path is validated to point at an existing regular file
    before being trusted; if it doesn't, we fall through to the PATH
    lookup rather than handing a stale or hostile path to ``Popen``.
    """
    case_arg = str(case_dir.resolve())
    explicit = executable_path.strip()
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return [explicit, "--case-dir", case_arg]
        logger.warning(
            "Configured %s path %r is not a regular file; falling back to PATH.",
            module_name,
            explicit,
        )
    on_path = shutil.which(module_name) or shutil.which(f"{module_name}.exe")
    if on_path:
        return [on_path, "--case-dir", case_arg]
    sibling = _frozen_sibling_exe(module_name)
    if sibling is not None:
        return [str(sibling), "--case-dir", case_arg]
    return [sys.executable, "-m", module_name, "--case-dir", case_arg]


def _spawn(command: list[str], *, tool_label: str, case_dir: Path) -> LaunchResult:
    try:
        subprocess.Popen(  # noqa: S603 - command pieces come from config or our own sys.executable
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        logger.warning("%s launch failed: %s (cmd=%r)", tool_label, exc, command)
        return LaunchResult(
            ok=False,
            command=command,
            tool_label=tool_label,
            message=(
                f"Could not launch {tool_label}: {exc}.\n\n"
                f"Tried: {' '.join(command)}\n\n"
                f"Set the path explicitly in Settings → Storage and launcher."
            ),
        )
    logger.info("Launched %s: %s", tool_label, command)
    return LaunchResult(
        ok=True,
        command=command,
        tool_label=tool_label,
        message=f"Launched {tool_label} against {case_dir}.",
    )


def launch_tool(
    *, tool_label: str, module_name: str, executable_path: str, case_dir: Path
) -> LaunchResult:
    """Spawn ``module_name`` pointed at ``case_dir``. Non-blocking."""
    command = build_command(
        executable_path=executable_path,
        module_name=module_name,
        case_dir=case_dir,
    )
    return _spawn(command, tool_label=tool_label, case_dir=case_dir)
