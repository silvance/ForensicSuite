"""Suite-tool launcher: command construction and resolution order."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING
from unittest.mock import patch

from caseforge.launcher import build_command

if TYPE_CHECKING:
    from pathlib import Path


def test_explicit_path_wins(tmp_path: Path) -> None:
    fake_exe = tmp_path / "Inscription.exe"
    fake_exe.write_text("", encoding="utf-8")
    cmd = build_command(
        executable_path=str(fake_exe),
        module_name="inscription",
        case_dir=tmp_path,
    )
    assert cmd[0] == str(fake_exe)
    assert cmd[1:3] == ["--case-dir", str(tmp_path.resolve())]


def test_missing_explicit_path_falls_back_to_path_lookup(tmp_path: Path) -> None:
    """An explicit path that doesn't exist on disk should fall through.

    Hardening — a stale config (e.g. dragged-and-dropped to a path that
    the user later renamed) should resolve via PATH rather than be
    handed unchanged to ``Popen``.
    """
    bogus = tmp_path / "not-installed.exe"
    with patch(
        "caseforge.launcher.shutil.which",
        side_effect=lambda name: f"/usr/local/bin/{name}" if name == "inscription" else None,
    ):
        cmd = build_command(
            executable_path=str(bogus), module_name="inscription", case_dir=tmp_path
        )
    assert cmd[0] == "/usr/local/bin/inscription"


def test_falls_back_to_path_lookup_for_inscription(tmp_path: Path) -> None:
    with patch(
        "caseforge.launcher.shutil.which",
        side_effect=lambda name: f"/usr/local/bin/{name}" if name == "inscription" else None,
    ):
        cmd = build_command(
            executable_path="", module_name="inscription", case_dir=tmp_path
        )
    assert cmd[0] == "/usr/local/bin/inscription"
    assert cmd[1] == "--case-dir"


def test_falls_back_to_path_lookup_for_caseguide(tmp_path: Path) -> None:
    with patch(
        "caseforge.launcher.shutil.which",
        side_effect=lambda name: f"/opt/bin/{name}" if name == "caseguide" else None,
    ):
        cmd = build_command(
            executable_path="", module_name="caseguide", case_dir=tmp_path
        )
    assert cmd[0] == "/opt/bin/caseguide"


def test_falls_back_to_python_module_when_path_misses(tmp_path: Path) -> None:
    with patch("caseforge.launcher.shutil.which", return_value=None):
        cmd = build_command(
            executable_path="", module_name="caseguide", case_dir=tmp_path
        )
    assert cmd[:3] == [sys.executable, "-m", "caseguide"]
    assert cmd[-2:] == ["--case-dir", str(tmp_path.resolve())]


def test_whitespace_only_path_falls_through(tmp_path: Path) -> None:
    with patch("caseforge.launcher.shutil.which", return_value=None):
        cmd = build_command(
            executable_path="   ", module_name="inscription", case_dir=tmp_path
        )
    assert cmd[:3] == [sys.executable, "-m", "inscription"]


def test_frozen_bundle_resolves_to_sibling_exe(tmp_path: Path) -> None:
    """In the air-gapped bundle, CaseForge.exe must launch the real
    Inscription.exe rather than re-launching itself via the python-module
    fallback (which PyInstaller silently ignores, opening a second
    CaseForge window instead).
    """
    bundle_root = tmp_path / "InscriptionSuite-Airgapped"
    (bundle_root / "CaseForge").mkdir(parents=True)
    (bundle_root / "Inscription").mkdir()
    (bundle_root / "CaseGuide").mkdir()
    fake_caseforge = bundle_root / "CaseForge" / "CaseForge.exe"
    fake_caseforge.write_bytes(b"")
    fake_inscription = bundle_root / "Inscription" / "Inscription.exe"
    fake_inscription.write_bytes(b"")

    with (
        patch("caseforge.launcher.sys.frozen", create=True, new=True),
        patch("caseforge.launcher.sys.executable", str(fake_caseforge)),
        patch("caseforge.launcher.shutil.which", return_value=None),
    ):
        cmd = build_command(
            executable_path="", module_name="inscription", case_dir=tmp_path
        )
    assert cmd[0] == str(fake_inscription)
    assert cmd[1:3] == ["--case-dir", str(tmp_path.resolve())]


def test_frozen_bundle_with_missing_sibling_falls_through(tmp_path: Path) -> None:
    """If the bundle is incomplete (e.g. a corrupted copy lost CaseGuide),
    fall through to the python-module path rather than synthesising a
    nonexistent .exe path that Popen would then crash on.
    """
    bundle_root = tmp_path / "InscriptionSuite-Airgapped"
    (bundle_root / "CaseForge").mkdir(parents=True)
    fake_caseforge = bundle_root / "CaseForge" / "CaseForge.exe"
    fake_caseforge.write_bytes(b"")
    # No Inscription/Inscription.exe present.

    with (
        patch("caseforge.launcher.sys.frozen", create=True, new=True),
        patch("caseforge.launcher.sys.executable", str(fake_caseforge)),
        patch("caseforge.launcher.shutil.which", return_value=None),
    ):
        cmd = build_command(
            executable_path="", module_name="inscription", case_dir=tmp_path
        )
    assert cmd[:3] == [str(fake_caseforge), "-m", "inscription"]


def test_frozen_bundle_resolves_caseguide_with_correct_case(tmp_path: Path) -> None:
    """CaseGuide's bundle folder is ``CaseGuide`` (camel-case), not
    ``Caseguide`` (which ``"caseguide".capitalize()`` would produce).

    Regression: ``_frozen_sibling_exe`` used ``module_name.capitalize()``
    to derive the folder name. Windows' case-insensitive filesystem
    hid the mismatch -- ``Caseguide\\Caseguide.exe`` resolved to the
    real ``CaseGuide\\CaseGuide.exe`` -- but on a case-sensitive
    volume the launcher would silently fall through to ``python -m``
    and re-launch CaseForge.exe instead of the real CaseGuide.exe.
    """
    bundle_root = tmp_path / "InscriptionSuite-Airgapped"
    (bundle_root / "CaseForge").mkdir(parents=True)
    (bundle_root / "CaseGuide").mkdir()
    fake_caseforge = bundle_root / "CaseForge" / "CaseForge.exe"
    fake_caseforge.write_bytes(b"")
    fake_caseguide = bundle_root / "CaseGuide" / "CaseGuide.exe"
    fake_caseguide.write_bytes(b"")

    with (
        patch("caseforge.launcher.sys.frozen", create=True, new=True),
        patch("caseforge.launcher.sys.executable", str(fake_caseforge)),
        patch("caseforge.launcher.shutil.which", return_value=None),
    ):
        cmd = build_command(
            executable_path="", module_name="caseguide", case_dir=tmp_path
        )
    assert cmd[0] == str(fake_caseguide)


def test_frozen_bundle_unknown_module_falls_through(tmp_path: Path) -> None:
    """An unrecognised module name has no entry in the bundle-name
    map; fall through to ``python -m <module>`` rather than guessing
    a folder layout we don't actually own.

    This protects against future module names whose capitalisation
    doesn't follow the obvious pattern -- they need an explicit
    entry in ``_FROZEN_BUNDLE_NAMES`` to be reachable as a bundled
    sibling.
    """
    bundle_root = tmp_path / "InscriptionSuite-Airgapped"
    (bundle_root / "CaseForge").mkdir(parents=True)
    fake_caseforge = bundle_root / "CaseForge" / "CaseForge.exe"
    fake_caseforge.write_bytes(b"")

    with (
        patch("caseforge.launcher.sys.frozen", create=True, new=True),
        patch("caseforge.launcher.sys.executable", str(fake_caseforge)),
        patch("caseforge.launcher.shutil.which", return_value=None),
    ):
        cmd = build_command(
            executable_path="", module_name="reportbuilder", case_dir=tmp_path
        )
    # Falls through to ``python -m reportbuilder``.
    assert cmd[:3] == [str(fake_caseforge), "-m", "reportbuilder"]
