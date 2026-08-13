"""Application bootstrap for Inscription."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
from suite_common.ui import worker_registry

from inscription import __version__
from inscription.logging_setup import configure_logging
from inscription.paths import ensure_dirs
from inscription.ui.app_icon import build_app_icon
from inscription.ui.main_window import MainWindow
from inscription.ui.style import apply_global_style

logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    """Return True when running from a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse Inscription's own flags and return them + the leftover argv.

    Qt has its own argv handling (``-platform``, ``-style``, ...) so we
    use ``parse_known_args`` to keep anything we don't recognise and pass
    it through to ``QApplication``.
    """
    parser = argparse.ArgumentParser(
        prog="inscription",
        description=(
            "Record a Windows workflow and turn it into an editable "
            "step-by-step guide with screenshots."
        ),
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Use PATH as the session workspace for this run. Intended for "
            "integration with CaseForge: point Inscription at the case "
            "directory CaseForge created and all sessions land inside it. "
            "Does not modify the saved config."
        ),
    )
    parser.add_argument("--version", action="version", version=f"Inscription {__version__}")
    # First arg is the program name; argparse expects the args to follow.
    return parser.parse_known_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    """Application entry point. Returns the Qt event-loop exit code."""
    argv = list(sys.argv if argv is None else argv)

    args, remaining = _parse_args(argv)

    ensure_dirs()
    configure_logging(console=not _is_frozen())
    logger.info("Starting Inscription %s", __version__)
    if args.case_dir is not None:
        logger.info("Using case directory: %s", args.case_dir)

    QCoreApplication.setOrganizationName("Inscription")
    QCoreApplication.setApplicationName("Inscription")
    QCoreApplication.setApplicationVersion(__version__)

    # Qt expects argv[0] to be the program name; rebuild from our remainder.
    qt_argv = [argv[0], *remaining]
    app = QApplication(qt_argv)
    app.setStyle("Fusion")
    palette = apply_global_style(app)
    app.setWindowIcon(build_app_icon(palette))

    window = MainWindow(case_dir=args.case_dir)
    window.show()

    rc = app.exec()
    logger.info("Inscription exited with code %d", rc)
    # Backstop against zombie processes: a cancelled rewrite /
    # transcription lets its worker run on in the background by design,
    # but once the event loop is done nothing may keep a headless (and,
    # under the air-gapped launcher, elevated) Inscription.exe alive --
    # it holds the global input hooks and every loaded DLL, blocking
    # the suite installer's upgrade swap. May not return.
    worker_registry.shutdown_lingering_workers(rc)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
