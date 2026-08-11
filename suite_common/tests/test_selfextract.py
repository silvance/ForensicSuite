"""Self-extracting installer core: exe+zip concatenation behaviour.

The single-exe bundle is ``stub.exe`` with a ZIP appended; zipfile
finds the central directory from the end of the file, so the
concatenated artifact opens directly. These tests prove that
mechanism plus the plan / extract / version logic, using a fake stub
(arbitrary bytes) -- no PyInstaller or Windows needed.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import TYPE_CHECKING

import pytest

from suite_common.selfextract import (
    BUNDLE_DIRNAME,
    Action,
    extract_payload,
    installed_version,
    open_payload,
    payload_version,
    plan_action,
)

if TYPE_CHECKING:
    from pathlib import Path


def _make_single_exe(
    path: Path,
    *,
    version: str | None = "2026.07.29-1",
    extra: dict[str, bytes] | None = None,
) -> Path:
    """Write fake-stub-bytes + payload zip to ``path``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if version is not None:
            zf.writestr(
                f"{BUNDLE_DIRNAME}/version.json",
                json.dumps({"bundle_version": version}),
            )
        zf.writestr(f"{BUNDLE_DIRNAME}/Install-Suite.cmd", "@echo off\r\n")
        zf.writestr(f"{BUNDLE_DIRNAME}/start-suite.ps1", "# launcher\n")
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    path.write_bytes(b"MZ\x90\x00fake-pyinstaller-stub" * 100 + buf.getvalue())
    return path


def test_zip_opens_directly_from_concatenated_exe(tmp_path: Path) -> None:
    """The load-bearing trick: zipfile tolerates prepended data, so
    the exe itself IS the archive. If this ever breaks, the whole
    single-exe design breaks with it."""
    exe = _make_single_exe(tmp_path / "Setup.exe")
    with open_payload(exe) as zf:
        assert f"{BUNDLE_DIRNAME}/Install-Suite.cmd" in zf.namelist()
        assert payload_version(zf) == "2026.07.29-1"


def test_bare_stub_without_payload_raises_badzip(tmp_path: Path) -> None:
    exe = tmp_path / "bare.exe"
    exe.write_bytes(b"MZ\x90\x00no payload here")
    with pytest.raises(zipfile.BadZipFile):
        open_payload(exe)


def test_plan_installs_when_nothing_installed(tmp_path: Path) -> None:
    exe = _make_single_exe(tmp_path / "Setup.exe")
    with open_payload(exe) as zf:
        plan = plan_action(zf=zf, install_root=tmp_path / "not-there")
    assert plan.action is Action.INSTALL
    assert plan.installed_version is None


def test_plan_launches_when_versions_match(tmp_path: Path) -> None:
    exe = _make_single_exe(tmp_path / "Setup.exe", version="v7")
    install_root = tmp_path / "installed"
    install_root.mkdir()
    (install_root / "version.json").write_text(
        json.dumps({"bundle_version": "v7"}), encoding="utf-8"
    )
    (install_root / "start-suite.ps1").write_text("# launcher", encoding="utf-8")
    with open_payload(exe) as zf:
        plan = plan_action(zf=zf, install_root=install_root)
    assert plan.action is Action.LAUNCH


def test_plan_reinstalls_on_version_drift_and_unstamped(tmp_path: Path) -> None:
    install_root = tmp_path / "installed"
    install_root.mkdir()
    (install_root / "version.json").write_text(
        json.dumps({"bundle_version": "v6"}), encoding="utf-8"
    )
    (install_root / "start-suite.ps1").write_text("# launcher", encoding="utf-8")

    exe = _make_single_exe(tmp_path / "SetupNew.exe", version="v7")
    with open_payload(exe) as zf:
        assert plan_action(zf=zf, install_root=install_root).action is Action.INSTALL

    # Unstamped payload must reinstall even over a matching-less state.
    exe2 = _make_single_exe(tmp_path / "SetupUnstamped.exe", version=None)
    with open_payload(exe2) as zf:
        plan = plan_action(zf=zf, install_root=install_root)
    assert plan.action is Action.INSTALL
    assert "unstamped" in plan.reason.lower() or "!=" in plan.reason


def test_launcher_missing_forces_reinstall_despite_matching_version(
    tmp_path: Path,
) -> None:
    """A version.json with no start-suite.ps1 beside it is a broken
    install (half-deleted, interrupted copy) -- launching would fail,
    so the plan must repair by reinstalling."""
    exe = _make_single_exe(tmp_path / "Setup.exe", version="v7")
    install_root = tmp_path / "installed"
    install_root.mkdir()
    (install_root / "version.json").write_text(
        json.dumps({"bundle_version": "v7"}), encoding="utf-8"
    )
    with open_payload(exe) as zf:
        assert plan_action(zf=zf, install_root=install_root).action is Action.INSTALL


def test_extract_writes_bundle_and_reports_progress(tmp_path: Path) -> None:
    exe = _make_single_exe(
        tmp_path / "Setup.exe",
        extra={f"{BUNDLE_DIRNAME}/apps/CaseForge/CaseForge.exe": b"MZ-fake"},
    )
    ticks: list[tuple[int, int]] = []
    with open_payload(exe) as zf:
        bundle = extract_payload(
            zf, tmp_path / "cache", progress=lambda d, t: ticks.append((d, t))
        )
    assert bundle.name == BUNDLE_DIRNAME
    assert (bundle / "Install-Suite.cmd").is_file()
    assert (bundle / "apps" / "CaseForge" / "CaseForge.exe").read_bytes() == b"MZ-fake"
    assert ticks[-1][0] == ticks[-1][1]  # completed


def test_extract_rejects_path_traversal_members(tmp_path: Path) -> None:
    """A member like ``../../evil`` must abort extraction, not write
    outside the cache. Our own build writes clean names, but archives
    are inputs and inputs get validated."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{BUNDLE_DIRNAME}/ok.txt", "fine")
        info = zipfile.ZipInfo("../escape.txt")
        zf.writestr(info, "evil")
    exe = tmp_path / "Evil.exe"
    exe.write_bytes(b"STUB" + buf.getvalue())
    with open_payload(exe) as zf, pytest.raises(zipfile.BadZipFile):
        extract_payload(zf, tmp_path / "cache")
    assert not (tmp_path / "escape.txt").exists()


def test_installed_version_reads_common_key_variants(tmp_path: Path) -> None:
    root = tmp_path
    (root / "version.json").write_text(
        json.dumps({"built_at": "2026-07-29T12:00:00Z"}), encoding="utf-8"
    )
    assert installed_version(root) == "2026-07-29T12:00:00Z"


# --------------------------------------------------------- download mode


def _serve_once(tmp_path: Path, body: bytes) -> tuple[str, object]:
    """Loopback HTTP server serving ``body``; returns (url, server)."""
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:  # silence
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_port}/payload.zip", server


def test_download_verifies_hash_and_reports_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transfer + hash verification + progress against a loopback
    server. The HTTPS-only guard is production behaviour; the test
    maps the https URL back to the local http listener at the
    urllib layer so the guard itself stays exercised."""
    import hashlib

    from suite_common import selfextract as mod

    body = b"payload-bytes" * 1000
    url, server = _serve_once(tmp_path, body)
    real_urlopen = mod.urllib.request.urlopen
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda req, timeout=None: real_urlopen(
            (req.full_url if hasattr(req, "full_url") else req).replace(
                "https://", "http://"
            ),
            timeout=timeout,
        ),
    )
    ticks: list[tuple[int, int]] = []
    try:
        dest = mod.download_file(
            url.replace("http://", "https://"),
            tmp_path / "dl.zip",
            expected_sha256=hashlib.sha256(body).hexdigest(),
            progress=lambda d, t: ticks.append((d, t)),
        )
    finally:
        server.shutdown()
    assert dest.read_bytes() == body
    assert ticks[-1] == (len(body), len(body))


def test_download_rejects_hash_mismatch_and_removes_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from suite_common import selfextract as mod

    body = b"tampered-bytes"
    url, server = _serve_once(tmp_path, body)
    real_urlopen = mod.urllib.request.urlopen
    monkeypatch.setattr(
        mod.urllib.request,
        "urlopen",
        lambda req, timeout=None: real_urlopen(
            (req.full_url if hasattr(req, "full_url") else req).replace(
                "https://", "http://"
            ),
            timeout=timeout,
        ),
    )
    try:
        with pytest.raises(mod.PayloadDownloadError) as excinfo:
            mod.download_file(
                url.replace("http://", "https://"),
                tmp_path / "dl.zip",
                expected_sha256="0" * 64,
            )
    finally:
        server.shutdown()
    assert "integrity" in str(excinfo.value).lower()
    assert not (tmp_path / "dl.zip").exists()


def test_download_refuses_plain_http() -> None:
    from pathlib import Path as _P

    from suite_common import selfextract as mod

    with pytest.raises(mod.PayloadDownloadError):
        mod.download_file(
            "http://example.com/x.zip", _P("/tmp/never"), expected_sha256="0" * 64
        )
