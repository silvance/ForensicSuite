"""Core logic for the single-file suite installer/launcher.

``InscriptionSuite-Setup.exe`` is a small PyInstaller stub with the
entire staged air-gapped bundle appended as a ZIP archive. ZIP's
central directory lives at the END of a file, so :mod:`zipfile` opens
the concatenated ``stub.exe + payload.zip`` directly -- the same
mechanism classic self-extracting archives use. No markers, no
offsets, no custom container format to maintain.

This module is the stub's brain, kept in suite_common so it is unit
tested with the rest of the suite. It is strictly stdlib-only: the
stub is a standalone one-file build and must not drag Qt (or anything
else) into it. The tkinter GUI shell lives in
``scripts/setup_stub.py`` and just calls into here.

Runtime behaviour of the stub, expressed as :func:`plan_action`:

- payload version already installed  -> LAUNCH the installed suite
- different / no version installed   -> EXTRACT, run the bundle's own
  ``Install-Suite.cmd`` (unchanged from the folder-based flow), then
  clean up the extraction cache

so the same exe is both the installer (first run) and the day-to-day
launcher (every run after) on the air-gapped workstation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

#: Top-level directory name inside the payload ZIP (and of the staged
#: folder the build zips up). Everything in the archive lives under it.
BUNDLE_DIRNAME = "InscriptionSuite-Airgapped"

#: The bundle's own installer shim, run after extraction. Reusing it
#: keeps ONE install path for both the folder-on-USB flow and the
#: single-exe flow -- shortcuts, PS7 bootstrap, and future installer
#: changes apply to both automatically.
INSTALLER_NAME = "Install-Suite.cmd"

#: Launcher script inside the INSTALLED copy.
LAUNCHER_NAME = "start-suite.ps1"

#: Cap on version.json size (mirrors suite_common.bundle).
_MAX_VERSION_JSON_BYTES = 64 * 1024


class Action(StrEnum):
    """What the stub should do this run."""

    LAUNCH = "launch"
    INSTALL = "install"


@dataclass(frozen=True, slots=True, kw_only=True)
class Plan:
    """Decision for this stub run."""

    action: Action
    #: Human-readable reason, shown in the stub's status label / log.
    reason: str
    payload_version: str
    installed_version: str | None


def default_install_root() -> Path:
    """Where install.ps1 puts the suite (its own default)."""
    import os

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        return Path(local) / "Programs" / "InscriptionSuite"
    # Non-Windows dev/test fallback; never hit on a real target.
    return Path.home() / ".local" / "share" / "InscriptionSuite"


def open_payload(exe_path: Path) -> zipfile.ZipFile:
    """Open the ZIP payload appended to ``exe_path``.

    Raises :class:`zipfile.BadZipFile` when no payload is present
    (e.g. someone runs a bare stub) -- the GUI turns that into a
    "this build is broken, re-create it" message.
    """
    return zipfile.ZipFile(exe_path)


def _version_from_json(raw: bytes) -> str:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    # prepare-bundle.ps1 stamps git_sha + build_timestamp; older /
    # other builders may use the generic names. git_sha is the
    # strongest identity (exact source), so it wins.
    for key in ("git_sha", "bundle_version", "version", "build_timestamp", "built_at"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def payload_version(zf: zipfile.ZipFile) -> str:
    """Version stamp of the embedded bundle ('' when unstamped).

    Reads the ``version.json`` prepare-bundle writes at the bundle
    root. An unstamped payload still installs -- it just always
    re-installs, which is the safe direction.
    """
    name = f"{BUNDLE_DIRNAME}/version.json"
    try:
        info = zf.getinfo(name)
    except KeyError:
        return ""
    if info.file_size > _MAX_VERSION_JSON_BYTES:
        return ""
    return _version_from_json(zf.read(name))


def installed_version(install_root: Path) -> str | None:
    """Version of the installed copy, or None when not installed."""
    marker = install_root / "version.json"
    if not marker.is_file():
        return None
    try:
        if marker.stat().st_size > _MAX_VERSION_JSON_BYTES:
            return ""
        return _version_from_json(marker.read_bytes())
    except OSError:
        return None


def plan_action(*, zf: zipfile.ZipFile, install_root: Path) -> Plan:
    """Decide between launching the installed suite and (re)installing.

    A matching non-empty version short-circuits to LAUNCH. Anything
    else -- not installed, unstamped payload, version drift in either
    direction -- installs; downgrades are deliberate (the exe in your
    hand is the version you chose to run).
    """
    pv = payload_version(zf)
    iv = installed_version(install_root)
    launcher = install_root / LAUNCHER_NAME
    if iv is not None and launcher.is_file() and pv and iv == pv:
        return Plan(
            action=Action.LAUNCH,
            reason=f"Version {pv} already installed",
            payload_version=pv,
            installed_version=iv,
        )
    if iv is None:
        reason = "Suite not installed yet"
    elif not pv:
        reason = "Payload is unstamped; reinstalling to be safe"
    else:
        reason = f"Installed {iv!r} != payload {pv!r}"
    return Plan(
        action=Action.INSTALL,
        reason=reason,
        payload_version=pv,
        installed_version=iv,
    )


def extract_payload(
    zf: zipfile.ZipFile,
    dest: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> Path:
    """Extract the bundle into ``dest``; returns the bundle directory.

    Per-member extraction (not ``extractall``) so the GUI can drive a
    real progress bar over a multi-GB payload. Member names are
    validated against path traversal -- an archive is an input like
    any other, even one we built ourselves.
    """
    members = zf.infolist()
    total = len(members)
    dest_resolved = dest.resolve()
    for i, member in enumerate(members):
        target = (dest / member.filename).resolve()
        if not target.is_relative_to(dest_resolved):
            msg = f"Archive member escapes destination: {member.filename!r}"
            raise zipfile.BadZipFile(msg)
        zf.extract(member, dest)
        if progress is not None:
            progress(i + 1, total)
    bundle_dir = dest / BUNDLE_DIRNAME
    if not bundle_dir.is_dir():
        msg = f"Payload did not contain a {BUNDLE_DIRNAME}/ directory"
        raise zipfile.BadZipFile(msg)
    return bundle_dir


def cleanup_extraction(dest: Path) -> None:
    """Best-effort removal of the extraction cache after install.

    The installer has copied everything it needs into the install
    root; keeping the cache would double the multi-GB footprint.
    Failure is logged, never raised -- a leftover cache is untidy,
    not broken.
    """
    try:
        shutil.rmtree(dest)
    except OSError as exc:
        logger.warning("Could not remove extraction cache %s: %s", dest, exc)


#: Download chunk size. 1 MiB balances progress-callback granularity
#: against syscall overhead on a multi-GB payload.
_DOWNLOAD_CHUNK = 1024 * 1024


class PayloadDownloadError(Exception):
    """Network fetch or integrity verification of the payload failed."""


def download_file(
    url: str,
    dest: Path,
    *,
    expected_sha256: str,
    progress: Callable[[int, int], None] | None = None,
    timeout_s: float = 60.0,
) -> Path:
    """Download ``url`` to ``dest`` and verify its SHA-256. Stdlib-only.

    The expected hash is baked into the online-installer stub at
    release-build time, so integrity doesn't depend on trusting the
    transport or a checksum file fetched over the same channel: the
    exe the operator chose to run pins exactly one payload.

    ``progress`` receives ``(bytes_done, bytes_total)``;
    ``bytes_total`` is 0 when the server sends no Content-Length.
    Any failure (network, disk, hash mismatch) raises
    :class:`PayloadDownloadError` with operator-facing text and
    removes the partial file.
    """
    if not url.lower().startswith("https://"):
        msg = f"Refusing non-HTTPS payload URL: {url}"
        raise PayloadDownloadError(msg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "InscriptionSuite-Setup"})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with dest.open("wb") as out:
                while True:
                    chunk = resp.read(_DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
    except PayloadDownloadError:
        raise
    except Exception as exc:
        dest.unlink(missing_ok=True)
        msg = (
            f"Could not download the suite payload.\n\n{exc}\n\n"
            "Check the internet connection and try again. (This online "
            "installer needs network access; for air-gapped machines "
            "use the offline Setup exe or the USB bundle instead.)"
        )
        raise PayloadDownloadError(msg) from exc
    actual = digest.hexdigest()
    if actual.lower() != expected_sha256.lower():
        dest.unlink(missing_ok=True)
        msg = (
            "Downloaded payload failed integrity verification.\n\n"
            f"expected SHA-256: {expected_sha256}\n"
            f"actual   SHA-256: {actual}\n\n"
            "The download may have been corrupted or tampered with. "
            "Re-download this installer from the official release page."
        )
        raise PayloadDownloadError(msg)
    return dest
