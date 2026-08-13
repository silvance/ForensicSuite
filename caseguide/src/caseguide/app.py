"""CaseGuide application bootstrap."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication
from suite_common.ui import worker_registry

from caseguide import __version__
from caseguide.logging_setup import configure_logging
from caseguide.paths import ensure_dirs
from caseguide.ui.app_icon import build_app_icon
from caseguide.ui.main_window import MainWindow
from caseguide.ui.style import apply_global_style

logger = logging.getLogger(__name__)


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    parser = argparse.ArgumentParser(
        prog="caseguide",
        description="LLM-assisted exam coach for the Inscription forensic-exam suite.",
    )
    parser.add_argument(
        "--case-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "Open the case at PATH on launch. Intended for the "
            "CaseForge integration: CaseForge launches CaseGuide pointed "
            "at the open case directory."
        ),
    )
    parser.add_argument("--version", action="version", version=f"CaseGuide {__version__}")
    return parser.parse_known_args(argv[1:])


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    args, remaining = _parse_args(argv)

    ensure_dirs()
    configure_logging(console=not _is_frozen())
    logger.info("Starting CaseGuide %s", __version__)
    if args.case_dir is not None:
        logger.info("Opening case directory: %s", args.case_dir)

    QCoreApplication.setOrganizationName("Silvance")
    QCoreApplication.setApplicationName("CaseGuide")
    QCoreApplication.setApplicationVersion(__version__)

    qt_argv = [argv[0], *remaining]
    app = QApplication(qt_argv)
    app.setStyle("Fusion")
    palette = apply_global_style(app)
    app.setWindowIcon(build_app_icon(palette))

    MainWindow(case_dir=args.case_dir)
    rc = app.exec()
    logger.info("CaseGuide exited with code %d", rc)
    # Backstop against zombie processes: a cancelled LLM refine lets its
    # worker run on by design, but nothing may keep a headless (and,
    # under the air-gapped launcher, elevated) CaseGuide.exe alive after
    # the event loop -- it blocks the suite installer's upgrade swap.
    # May not return.
    worker_registry.shutdown_lingering_workers(rc)
    return rc


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
