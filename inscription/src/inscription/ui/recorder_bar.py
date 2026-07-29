"""Top-of-window recorder controls.

A single horizontal strip with a Record/Stop button, a Marker button,
the session name, a live recording duration / event counter, and a
pulsing red REC indicator while a recording is in progress. The bar is
purely presentational — it emits Qt signals and lets the controller
own all state transitions.
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget


class _PulsingDot(QWidget):
    """Small recording indicator that fades in and out while visible.

    A 12 px red dot with an alpha that oscillates between full and ~30%.
    Painted manually so we don't depend on a GIF asset.
    """

    DIAMETER = 12
    PERIOD_MS = 1200

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(self.DIAMETER + 4, self.DIAMETER + 4)
        self._timer = QTimer(self)
        self._timer.setInterval(40)  # ~25 fps
        self._timer.timeout.connect(self.update)
        self._t0 = 0.0
        self.setVisible(False)

    def start(self) -> None:
        self._t0 = time.monotonic()
        self.setVisible(True)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.setVisible(False)

    def paintEvent(self, _event: object) -> None:  # noqa: N802 - Qt API
        elapsed = time.monotonic() - self._t0
        # Triangle wave between 0..1 over PERIOD_MS, then map to alpha 0.3..1.0.
        phase = (elapsed * 1000.0 / self.PERIOD_MS) % 1.0
        wave = 1.0 - abs(phase * 2.0 - 1.0)  # 0..1..0
        alpha = int(0.3 * 255 + wave * 0.7 * 255)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(215, 0, 21, alpha))
        cx = self.width() / 2
        cy = self.height() / 2
        painter.drawEllipse(
            int(cx - self.DIAMETER / 2),
            int(cy - self.DIAMETER / 2),
            self.DIAMETER,
            self.DIAMETER,
        )
        painter.end()


class RecorderBar(QWidget):
    """Recorder control strip."""

    record_toggled = Signal(bool)  # True -> start, False -> stop
    marker_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._record_btn = QPushButton("● Record", self)
        self._record_btn.setCheckable(True)
        self._record_btn.setProperty("role", "primary")
        self._record_btn.setMinimumWidth(110)
        self._record_btn.toggled.connect(self.record_toggled)

        self._marker_btn = QPushButton("Mark", self)
        self._marker_btn.setToolTip("Drop a marker (Ctrl+Shift+M)")
        self._marker_btn.clicked.connect(self.marker_requested)
        self._marker_btn.setEnabled(False)

        self._rec_dot = _PulsingDot(self)
        self._rec_label = QLabel("REC", self)
        rec_font = self._rec_label.font()
        rec_font.setBold(True)
        self._rec_label.setFont(rec_font)
        self._rec_label.setStyleSheet("color: #d70015;")
        self._rec_label.setVisible(False)

        self._duration_label = QLabel("", self)
        self._duration_label.setStyleSheet("font-variant-numeric: tabular-nums;")
        self._duration_label.setVisible(False)

        self._name_label = QLabel("No session open", self)
        font = self._name_label.font()
        font.setBold(True)
        font.setPointSize(font.pointSize() + 1)
        self._name_label.setFont(font)

        self._count_label = QLabel("0 events", self)
        self._count_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._count_label.setProperty("muted", True)

        # Persist-failure warning. Hidden while everything saves
        # cleanly; turns visible (and stays visible for the rest of
        # the recording) the moment any sink raises, so an operator
        # whose events are silently failing to reach the database
        # finds out DURING the exam, not after.
        self._persist_warn_label = QLabel("", self)
        self._persist_warn_label.setStyleSheet("color: #d70015; font-weight: bold;")
        self._persist_warn_label.setVisible(False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(10)
        layout.addWidget(self._record_btn)
        layout.addWidget(self._marker_btn)
        layout.addSpacing(8)
        layout.addWidget(self._rec_dot)
        layout.addWidget(self._rec_label)
        layout.addWidget(self._duration_label)
        layout.addStretch(0)
        layout.addWidget(self._name_label, 1)
        layout.addWidget(self._persist_warn_label)
        layout.addWidget(self._count_label)

        self._duration_timer = QTimer(self)
        self._duration_timer.setInterval(500)
        self._duration_timer.timeout.connect(self._tick_duration)
        self._record_started_at: float | None = None

        # Capture is Windows-only; on other OSes we hard-disable the
        # buttons via set_capture_supported(). Default True so tests
        # and the Windows code path don't need to opt in explicitly.
        self._capture_supported = True

    # ----------------------------------------------------------- API

    def set_capture_supported(self, supported: bool, *, reason: str = "") -> None:
        """Toggle whether the Record / Mark buttons are usable.

        Called from MainWindow at startup with ``supported=False`` on
        non-Windows hosts to keep the operator from kicking off a
        capture session that would produce zero-confidence
        ResolvedElements (the Linux foreground inspector returns
        empty title/process strings, so steps would all be generic
        "click in unknown" placeholders -- worse than no capture in a
        forensic context).
        """
        self._capture_supported = supported
        self._record_btn.setEnabled(supported)
        # Mark also requires an open session; we honour that on top of
        # the capture-supported flag.
        self._marker_btn.setEnabled(supported and self._marker_btn.isEnabled())
        tip = "" if supported else (reason or "Capture is not available on this platform.")
        self._record_btn.setToolTip(tip)
        self._marker_btn.setToolTip(tip)

    def set_session_name(self, name: str | None) -> None:
        self._name_label.setText(name or "No session open")
        self._marker_btn.setEnabled(name is not None and self._capture_supported)

    def set_recording(self, recording: bool) -> None:
        """Force the record button state without emitting ``record_toggled``."""
        blocked = self._record_btn.blockSignals(True)
        self._record_btn.setChecked(recording)
        self._record_btn.blockSignals(blocked)
        self._apply_recording_state(recording)

    def set_event_count(self, count: int) -> None:
        self._count_label.setText(f"{count} event{'s' if count != 1 else ''}")

    def set_persist_failures(self, count: int) -> None:
        """Show / update the sink-failure warning.

        ``count`` is the running total of persist failures for the
        current recording. Zero hides the warning (fresh recording);
        any positive value shows it with a tooltip pointing the
        operator at the log file. The label never auto-clears during
        a recording -- a capture pipeline that failed once cannot be
        trusted to have failed only once.
        """
        if count <= 0:
            self._persist_warn_label.setVisible(False)
            self._persist_warn_label.setText("")
            return
        self._persist_warn_label.setText(
            f"⚠ {count} event{'s' if count != 1 else ''} failed to save"
        )
        self._persist_warn_label.setToolTip(
            "Some captured events could not be written to the session "
            "database or screenshot folder. Check free disk space and "
            "the log file (Help → Show logs folder). The on-disk "
            "record for this recording may be incomplete."
        )
        self._persist_warn_label.setVisible(True)

    def toggle_record(self) -> None:
        """Flip the record button and fire ``record_toggled``.

        Used by the global Ctrl+Shift+R hotkey so the user can start and
        stop recording without clicking on Inscription's own window.
        """
        self._record_btn.setChecked(not self._record_btn.isChecked())

    # -------------------------------------------------------- internals

    def _apply_recording_state(self, recording: bool) -> None:
        self._record_btn.setText("■ Stop" if recording else "● Record")
        self._record_btn.setProperty(
            "role", "danger" if recording else "primary"
        )
        # Re-polish so the property change picks up new stylesheet rules.
        self._record_btn.style().unpolish(self._record_btn)
        self._record_btn.style().polish(self._record_btn)

        if recording:
            self._record_started_at = time.monotonic()
            self._rec_dot.start()
            self._rec_label.setVisible(True)
            self._duration_label.setVisible(True)
            self._duration_label.setText("00:00")
            self._duration_timer.start()
        else:
            self._record_started_at = None
            self._rec_dot.stop()
            self._rec_label.setVisible(False)
            self._duration_label.setVisible(False)
            self._duration_timer.stop()

    def _tick_duration(self) -> None:
        if self._record_started_at is None:
            return
        elapsed = int(time.monotonic() - self._record_started_at)
        mm, ss = divmod(elapsed, 60)
        hh, mm = divmod(mm, 60)
        text = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        self._duration_label.setText(text)
