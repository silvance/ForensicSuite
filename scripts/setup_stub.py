"""GUI stub for the single-file suite installer/launcher.

Built with ``pyinstaller --onefile`` and concatenated with the staged
bundle ZIP by ``make-single-exe.ps1``. All decision logic lives in
``suite_common.selfextract`` (unit-tested with the suite); this file
is only the tkinter shell: a progress window for the multi-GB
extraction, then hand-off to the bundle's own installer or the
installed launcher.

Stdlib-only by design -- tkinter ships with the python.org Windows
builds PyInstaller bundles from, and nothing here needs Qt.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import messagebox, ttk

from suite_common.selfextract import (
    Action,
    cleanup_extraction,
    default_install_root,
    download_file,
    extract_payload,
    open_payload,
    plan_action,
)

# Release metadata baked in at build time by the release workflow for
# the ONLINE installer variant: the payload URL on the GitHub release
# plus its SHA-256. Absent (ImportError) in the offline variant, which
# carries the payload appended to the exe instead.
try:
    import _release_meta  # type: ignore[import-not-found]
except ImportError:
    _release_meta = None

TITLE = "Inscription Suite"


def _fail(message: str) -> None:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror(TITLE, message)
    sys.exit(1)


def _launch_installed(install_root: Path) -> None:
    launcher = install_root / "start-suite.ps1"
    # start-suite.ps1 self-elevates for UIA visibility; launch it the
    # same way the Start Menu shortcut does.
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(launcher),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _run_installer(bundle_dir: Path) -> int:
    """Run the bundle's Install-Suite.cmd and wait for it."""
    return subprocess.call(
        ["cmd.exe", "/c", str(bundle_dir / "Install-Suite.cmd")],
        cwd=bundle_dir,
    )


def _download_payload_gui():
    """Online-installer path: fetch the release payload with progress.

    Returns an opened ZipFile, or None after showing an error dialog.
    The expected SHA-256 is baked into this exe at release-build time,
    so the download is pinned to exactly one payload regardless of
    what the transport serves.
    """
    import tempfile as _tf

    root = tk.Tk()
    root.title(TITLE)
    root.geometry("420x120")
    root.resizable(False, False)
    tk.Label(
        root,
        text=f"Downloading Inscription Suite {_release_meta.TAG}…",
        anchor="w",
    ).pack(fill="x", padx=16, pady=(16, 4))
    bar = ttk.Progressbar(root, maximum=100)
    bar.pack(fill="x", padx=16, pady=8)
    status = tk.Label(root, text="Connecting…", anchor="w")
    status.pack(fill="x", padx=16)
    root.update()

    dest = Path(_tf.gettempdir()) / f"InscriptionSuite-{_release_meta.TAG}.zip"

    def _tick(done: int, total: int) -> None:
        if total:
            bar["value"] = done * 100 / total
            status.configure(
                text=f"Downloading… {done // 1_048_576} / {total // 1_048_576} MB"
            )
        else:
            status.configure(text=f"Downloading… {done // 1_048_576} MB")
        root.update()

    try:
        download_file(
            _release_meta.PAYLOAD_URL,
            dest,
            expected_sha256=_release_meta.PAYLOAD_SHA256,
            progress=_tick,
        )
        root.destroy()
        return open_payload(dest)
    except Exception as exc:  # noqa: BLE001 - stub must dialog, never die silently
        try:
            root.destroy()
        except tk.TclError:
            pass
        _fail(str(exc))
        return None


def main() -> None:
    exe = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    try:
        zf = open_payload(exe)
    except Exception:  # noqa: BLE001 - stub must dialog, never die silently
        if _release_meta is None:
            _fail(
                "This launcher has no embedded suite bundle and no "
                "release metadata.\n\n"
                "Rebuild with Build-Bundle.bat -SingleExe (offline) or "
                "via the release workflow (online installer)."
            )
            return
        zf = _download_payload_gui()
        if zf is None:
            return

    install_root = default_install_root()
    plan = plan_action(zf=zf, install_root=install_root)

    if plan.action is Action.LAUNCH:
        zf.close()
        _launch_installed(install_root)
        return

    # INSTALL path: progress window over the multi-GB extraction.
    root = tk.Tk()
    root.title(TITLE)
    root.geometry("420x140")
    root.resizable(False, False)
    label = tk.Label(
        root,
        text=f"Setting up Inscription Suite…\n({plan.reason})",
        justify="left",
        anchor="w",
    )
    label.pack(fill="x", padx=16, pady=(16, 4))
    bar = ttk.Progressbar(root, maximum=100)
    bar.pack(fill="x", padx=16, pady=8)
    status = tk.Label(root, text="Extracting…", anchor="w")
    status.pack(fill="x", padx=16)

    cache = Path(tempfile.gettempdir()) / "InscriptionSuite-Extract"

    def _tick(done: int, total: int) -> None:
        bar["value"] = done * 100 / max(1, total)
        status.configure(text=f"Extracting… {done}/{total} files")
        root.update()

    def _work() -> None:
        try:
            bundle_dir = extract_payload(zf, cache, progress=_tick)
            zf.close()
            status.configure(text="Running installer…")
            root.update()
            code = _run_installer(bundle_dir)
            cleanup_extraction(cache)
            root.destroy()
            if code != 0:
                _fail(
                    f"The suite installer exited with code {code}.\n"
                    "See its console output for details."
                )
                return
            _launch_installed(install_root)
        except Exception:  # noqa: BLE001 - stub must dialog, never die silently
            cleanup_extraction(cache)
            try:
                root.destroy()
            except tk.TclError:
                pass
            _fail("Setup failed:\n\n" + traceback.format_exc(limit=3))

    # Run after the window paints; extraction drives update() itself.
    root.after(100, _work)
    root.mainloop()


if __name__ == "__main__":
    main()
