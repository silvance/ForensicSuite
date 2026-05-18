"""Custody tab — a small reusable widget shared by NewCaseDialog and CaseView.

Captures chain-of-custody intake fields: when the evidence was
received, who delivered it, how, evidence bag / seal IDs, whether the
seal was intact at receipt, and free-form notes. Wraps everything that
talks to :class:`CustodyRecord` so neither caller has to know the
field layout.
"""

from __future__ import annotations

from datetime import UTC, datetime

from PySide6.QtCore import QDateTime, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from caseforge.model import CustodyRecord


def _split_csv(text: str) -> list[str]:
    return [chunk.strip() for chunk in text.split(",") if chunk.strip()]


def _join_csv(items: list[str]) -> str:
    return ", ".join(items)


_SEAL_LABELS = {
    None: "Not recorded",
    True: "Yes — intact",
    False: "No — broken or tampered",
}


class CustodyTab(QWidget):
    """Form for editing a :class:`CustodyRecord`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._received_check = QCheckBox("Evidence has been received", self)
        self._received_check.toggled.connect(self._on_received_toggled)

        self._received_at_edit = QDateTimeEdit(self)
        self._received_at_edit.setCalendarPopup(True)
        self._received_at_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self._received_at_edit.setEnabled(False)

        received_row = QHBoxLayout()
        received_row.setSpacing(8)
        received_row.addWidget(self._received_check)
        received_row.addWidget(self._received_at_edit, 1)

        self._received_from_edit = QLineEdit(self)
        self._received_from_edit.setPlaceholderText(
            "Person or agency that delivered the evidence"
        )
        self._delivery_method_edit = QLineEdit(self)
        self._delivery_method_edit.setPlaceholderText(
            "e.g. in person, secure carrier, secure email"
        )
        self._evidence_bag_ids_edit = QLineEdit(self)
        self._evidence_bag_ids_edit.setPlaceholderText(
            "comma-separated bag / seal numbers, e.g. EB-12345, EB-12346"
        )

        self._seal_combo = QComboBox(self)
        # Index 0 = Not recorded, 1 = Yes intact, 2 = No / broken.
        # The bool literals here are dropdown payloads, not flag args.
        for value in (None, True, False):
            self._seal_combo.addItem(_SEAL_LABELS[value], value)

        self._notes_edit = QPlainTextEdit(self)
        self._notes_edit.setPlaceholderText(
            "Anything else worth recording for the chain of custody."
        )
        self._notes_edit.setMaximumHeight(120)

        hint = QLabel(
            "Optional. Filling this in pre-populates the report builder's "
            "chain-of-custody section.",
            self,
        )
        hint.setProperty("muted", "true")
        hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Received at", received_row)
        form.addRow("Received from", self._received_from_edit)
        form.addRow("Delivery method", self._delivery_method_edit)
        form.addRow("Evidence bag IDs", self._evidence_bag_ids_edit)
        form.addRow("Seal intact?", self._seal_combo)
        form.addRow("Custody notes", self._notes_edit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(form)
        layout.addWidget(hint)

    # ------------------------------------------------------------ API

    def set_record(self, record: CustodyRecord) -> None:
        if record.received_at is not None:
            self._received_check.setChecked(True)
            self._received_at_edit.setEnabled(True)
            qt_dt = QDateTime.fromString(
                record.received_at.astimezone().strftime("%Y-%m-%d %H:%M"),
                "yyyy-MM-dd HH:mm",
            )
            if qt_dt.isValid():
                self._received_at_edit.setDateTime(qt_dt)
        else:
            self._received_check.setChecked(False)
            self._received_at_edit.setEnabled(False)
            self._received_at_edit.setDateTime(QDateTime.currentDateTime())
        self._received_from_edit.setText(record.received_from)
        self._delivery_method_edit.setText(record.delivery_method)
        self._evidence_bag_ids_edit.setText(_join_csv(record.evidence_bag_ids))
        for index in range(self._seal_combo.count()):
            if self._seal_combo.itemData(index) == record.seal_intact:
                self._seal_combo.setCurrentIndex(index)
                break
        self._notes_edit.setPlainText(record.notes)

    def to_record(self) -> CustodyRecord:
        if self._received_check.isChecked():
            qt_dt = self._received_at_edit.dateTime()
            py_dt = qt_dt.toPython()
            if isinstance(py_dt, datetime):
                # QDateTimeEdit hands back a naive datetime in local wall-
                # clock time. Tag it with the system zone and convert to
                # UTC for storage; bare ``replace(tzinfo=UTC)`` would
                # mis-label the local reading as UTC and shift the
                # custody timestamp by the local offset.
                received_at: datetime | None = py_dt.astimezone(UTC)
            else:
                received_at = None
        else:
            received_at = None
        seal_value = self._seal_combo.currentData()
        seal: bool | None = seal_value if isinstance(seal_value, bool) else None
        return CustodyRecord(
            received_at=received_at,
            received_from=self._received_from_edit.text().strip(),
            delivery_method=self._delivery_method_edit.text().strip(),
            evidence_bag_ids=_split_csv(self._evidence_bag_ids_edit.text()),
            seal_intact=seal,
            notes=self._notes_edit.toPlainText().strip(),
        )

    # -------------------------------------------------------- internals

    def _on_received_toggled(self, checked: bool) -> None:
        self._received_at_edit.setEnabled(checked)
        if checked and self._received_at_edit.dateTime().isNull():
            self._received_at_edit.setDateTime(QDateTime.currentDateTime())


# ``Qt`` import is used for type/flag references in subclasses.
_ = Qt
