"""RecorderBar widget: persist-failure warning behaviour."""

from __future__ import annotations

import pytest

pytest.importorskip("pytestqt")

from inscription.ui.recorder_bar import RecorderBar


def test_persist_failure_warning_shows_and_hides(qtbot) -> None:  # type: ignore[no-untyped-def]
    """set_persist_failures(0) hides the warning; any positive count
    shows it with the count in the text. The operator must learn
    DURING the exam that events are failing to save -- a capture
    pipeline that fails silently produces an incomplete evidentiary
    record nobody notices until it's too late.
    """
    bar = RecorderBar()
    qtbot.addWidget(bar)
    assert bar._persist_warn_label.isVisible() is False

    bar.show()
    bar.set_persist_failures(3)
    assert bar._persist_warn_label.isVisible() is True
    assert "3 events failed to save" in bar._persist_warn_label.text()

    bar.set_persist_failures(1)
    assert "1 event failed to save" in bar._persist_warn_label.text()

    # New recording resets the counter and hides the warning.
    bar.set_persist_failures(0)
    assert bar._persist_warn_label.isVisible() is False
