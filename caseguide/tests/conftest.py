"""Pytest configuration for CaseGuide's test suite.

Force-Qt-offscreen lives in suite_common so all four test runs share
the same setup; this conftest is intentionally thin so additions
here stay app-specific.
"""

from __future__ import annotations

import suite_common.testing.qt_offscreen  # noqa: F401 - sets QT_QPA_PLATFORM at import
