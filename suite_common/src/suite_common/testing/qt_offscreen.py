"""Force Qt to use the offscreen platform plugin.

Headless CI runners (Linux GitHub Actions, our Linux dev container)
have no display server, so ``pytest-qt`` would fail at import time
when PySide6 tries to load the default ``xcb`` platform plugin.
Setting ``QT_QPA_PLATFORM=offscreen`` before PySide6 is imported
swaps in a software renderer that needs no display.

Importing this module sets the environment variable at module-load
time, so any conftest.py that imports it gets the side effect
before the test modules pull in PySide6:

    # tests/conftest.py
    import suite_common.testing.qt_offscreen  # noqa: F401

We use ``setdefault`` so a developer who's explicitly set the env
var to something else (xcb on a workstation with a display, eglfs on
a Raspberry Pi) keeps their choice.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
