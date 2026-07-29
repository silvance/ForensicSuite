"""Custom row renderer for the suggestions panel.

The default ``QListWidget`` row puts a single line of text per row,
which makes the suggestions feed look like a debug log. This delegate
draws each suggestion as a small designed component:

  ┌──────────┐
  │ REQUIRED │  Verify the SHA-256 of the acquired image
  └──────────┘  against the acquisition log.
                [verification]  ↳ depends on 1 step

A coloured priority chip on the left, the action text wrapped to fit
the row width, and a metadata row (category, dependency count) beneath.

The delegate reads the full :class:`caseguide.model.Suggestion` off
``Qt.ItemDataRole.UserRole`` — the panel stores the whole dataclass
on the item so the renderer doesn't have to round-trip through item
text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPalette,
    QPen,
)
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from caseguide.model import (
    PRIORITY_OPTIONAL,
    PRIORITY_RECOMMENDED,
    PRIORITY_REQUIRED,
    Suggestion,
)

if TYPE_CHECKING:
    from PySide6.QtCore import QModelIndex, QPersistentModelIndex


# Suggestion lives on the item via this user role. Same key is used by
# the panel when it stores the dataclass for the delegate to read.
SUGGESTION_ROLE = Qt.ItemDataRole.UserRole + 1

# Per-row "blocked by an incomplete prerequisite" flag, computed by the
# panel (which sees the whole list) and read by the delegate (which
# only sees one item at a time). The names of the missing
# prerequisites travel as a tuple of strings so the metadata row can
# show ``blocked by 2 (incomplete)`` without re-walking the model.
BLOCKED_ROLE = Qt.ItemDataRole.UserRole + 2


@dataclass(frozen=True, slots=True)
class _ChipPalette:
    """The two colours a priority chip needs."""

    bg: str
    fg: str


_PRIORITY_CHIPS: dict[str, _ChipPalette] = {
    PRIORITY_REQUIRED: _ChipPalette(bg="#d70015", fg="#ffffff"),
    PRIORITY_RECOMMENDED: _ChipPalette(bg="#0066cc", fg="#ffffff"),
    PRIORITY_OPTIONAL: _ChipPalette(bg="#e5e5e7", fg="#3a3a3c"),
}

# Completed rows swap the priority chip for a calm green "DONE" tag so
# the eye can scan the list and spot finished work at a glance without
# losing the row entirely.
_DONE_CHIP = _ChipPalette(bg="#34c759", fg="#ffffff")
_DONE_LABEL = "✓ DONE"
_COMPLETED_OPACITY = 0.55

# Blocked rows fade further than the chrome but stay legible — the
# examiner needs to read what's coming up, just shouldn't be tempted
# to start it. A leading lock glyph in the metadata row reinforces
# the "wait" semantics.
_BLOCKED_OPACITY = 0.45

_PADDING_H = 12
_PADDING_V = 10
_CHIP_PADDING_H = 8
_CHIP_PADDING_V = 3
_CHIP_GAP = 12  # space between chip and the action text
_META_GAP = 6  # vertical gap between action and metadata row
_BADGE_GAP = 8  # horizontal gap between metadata badges
_MIN_ROW_HEIGHT = 56


class SuggestionDelegate(QStyledItemDelegate):
    """Draws a :class:`Suggestion` as a designed row."""

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> None:
        suggestion = index.data(SUGGESTION_ROLE)
        if not isinstance(suggestion, Suggestion):
            super().paint(painter, option, index)
            return

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        # Draw selection / hover background via the host style first so
        # the row blends with QListWidget hover and selection.
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        rect = opt.rect.adjusted(_PADDING_H, _PADDING_V, -_PADDING_H, -_PADDING_V)

        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_completed = suggestion.completed
        # Blocked = at least one prerequisite isn't yet completed. The
        # panel computes the missing list once per render and stamps
        # it on the item; we just read it back. Completed wins over
        # blocked when both are set.
        blocked_missing_raw = index.data(BLOCKED_ROLE)
        blocked_missing: tuple[str, ...] = (
            tuple(blocked_missing_raw)
            if isinstance(blocked_missing_raw, (list, tuple)) and not is_completed
            else ()
        )
        if is_completed:
            painter.setOpacity(_COMPLETED_OPACITY)
        elif blocked_missing:
            painter.setOpacity(_BLOCKED_OPACITY)
        text_pen = self._text_pen(opt, selected=is_selected)
        muted_pen = self._muted_pen(opt, selected=is_selected)

        chip_rect = self._draw_priority_chip(
            painter,
            rect,
            suggestion.priority,
            selected=is_selected,
            completed=is_completed,
        )

        body_left = chip_rect.right() + _CHIP_GAP
        body_rect = QRect(body_left, rect.top(), rect.right() - body_left, rect.height())

        action_font = QFont(opt.font)
        action_font.setPointSize(opt.font.pointSize())
        action_font.setWeight(QFont.Weight.Medium)
        if is_completed:
            action_font.setStrikeOut(True)
        action_metrics = QFontMetrics(action_font)
        wrapped = self._elide_to_two_lines(action_metrics, suggestion.action, body_rect.width())
        line_height = action_metrics.lineSpacing()

        painter.setFont(action_font)
        painter.setPen(text_pen)
        for i, line in enumerate(wrapped):
            painter.drawText(
                body_rect.left(),
                body_rect.top() + (i + 1) * line_height - action_metrics.descent(),
                line,
            )

        meta_top = body_rect.top() + len(wrapped) * line_height + _META_GAP
        self._draw_metadata_row(
            painter,
            suggestion=suggestion,
            body_rect=body_rect,
            meta_top=meta_top,
            opt=opt,
            muted_pen=muted_pen,
            blocked_missing=blocked_missing,
        )

        painter.restore()

    def _draw_metadata_row(
        self,
        painter: QPainter,
        *,
        suggestion: Suggestion,
        body_rect: QRect,
        meta_top: int,
        opt: QStyleOptionViewItem,
        muted_pen: QPen,
        blocked_missing: tuple[str, ...] = (),
    ) -> None:
        meta_font = QFont(opt.font)
        meta_font.setPointSize(max(8, opt.font.pointSize() - 1))
        painter.setFont(meta_font)
        painter.setPen(muted_pen)
        meta_metrics = QFontMetrics(meta_font)
        meta_x = body_rect.left()
        if suggestion.category:
            badge_w = self._draw_chip(
                painter,
                QRect(meta_x, meta_top, 0, meta_metrics.height() + 2 * _CHIP_PADDING_V),
                suggestion.category,
                bg=QColor(_PRIORITY_CHIPS[PRIORITY_OPTIONAL].bg),
                fg=QColor(_PRIORITY_CHIPS[PRIORITY_OPTIONAL].fg),
                metrics=meta_metrics,
                size_only=False,
            )
            meta_x += badge_w + _BADGE_GAP

        baseline_y = meta_top + meta_metrics.ascent() + _CHIP_PADDING_V
        if blocked_missing:
            # Blocked rows shadow the depends_on hint with the actual
            # incomplete count so the examiner sees how close the row
            # is to becoming actionable.
            count = len(blocked_missing)
            painter.setPen(muted_pen)
            painter.drawText(
                meta_x,
                baseline_y,
                f"🔒 blocked by {count} incomplete step{'s' if count != 1 else ''}",
            )
        elif suggestion.depends_on:
            count = len(suggestion.depends_on)
            text = f"↳ depends on {count} step{'s' if count != 1 else ''}"
            painter.setPen(muted_pen)
            painter.drawText(meta_x, baseline_y, text)
        elif not suggestion.category and suggestion.expected_result:
            preview = suggestion.expected_result.strip().splitlines()[0]
            preview = meta_metrics.elidedText(
                preview, Qt.TextElideMode.ElideRight, body_rect.width()
            )
            painter.setPen(muted_pen)
            painter.drawText(meta_x, baseline_y, f"Expect: {preview}")

    def sizeHint(  # noqa: N802 - Qt API
        self,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> QSize:
        suggestion = index.data(SUGGESTION_ROLE)
        if not isinstance(suggestion, Suggestion):
            return super().sizeHint(option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        # Best-effort width: the view passes its own width through
        # option.rect, but on first paint it can be 0.
        width = max(opt.rect.width(), 320)
        chip_label = _DONE_LABEL if suggestion.completed else _priority_text(suggestion.priority)
        chip_width = self._chip_width(opt, chip_label)
        body_width = width - 2 * _PADDING_H - chip_width - _CHIP_GAP

        action_font = QFont(opt.font)
        action_font.setWeight(QFont.Weight.Medium)
        action_metrics = QFontMetrics(action_font)
        wrapped = self._elide_to_two_lines(action_metrics, suggestion.action, body_width)
        action_block = max(1, len(wrapped)) * action_metrics.lineSpacing()

        meta_font = QFont(opt.font)
        meta_font.setPointSize(max(8, opt.font.pointSize() - 1))
        meta_metrics = QFontMetrics(meta_font)
        meta_block = meta_metrics.height() + 2 * _CHIP_PADDING_V

        height = action_block + _META_GAP + meta_block + 2 * _PADDING_V
        return QSize(width, max(_MIN_ROW_HEIGHT, int(height)))

    # -------------------------------------------------------- internals

    def _draw_priority_chip(
        self,
        painter: QPainter,
        rect: QRect,
        priority: str,
        *,
        selected: bool,
        completed: bool = False,
    ) -> QRect:
        if completed:
            chip = _DONE_CHIP
            text = _DONE_LABEL
        else:
            chip = _PRIORITY_CHIPS.get(priority, _PRIORITY_CHIPS[PRIORITY_RECOMMENDED])
            text = _priority_text(priority)
        font = _chip_font(painter.font())
        chip_w, chip_h = _chip_size(font, text)
        chip_rect = QRect(rect.left(), rect.top() + 1, chip_w, chip_h)

        bg = QColor(chip.bg)
        fg = QColor(chip.fg)
        if selected:
            # When the row is highlighted, soften the chip so the
            # accent isn't competing with the row selection accent.
            bg.setAlphaF(0.85)

        painter.setBrush(QBrush(bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(chip_rect), 4, 4)

        painter.setFont(font)
        painter.setPen(QPen(fg))
        painter.drawText(chip_rect, Qt.AlignmentFlag.AlignCenter, text)
        return chip_rect

    @staticmethod
    def _chip_width(option: QStyleOptionViewItem, text: str) -> int:
        return _chip_size(_chip_font(option.font), text)[0]

    @staticmethod
    def _draw_chip(
        painter: QPainter,
        rect: QRect,
        text: str,
        *,
        bg: QColor,
        fg: QColor,
        metrics: QFontMetrics,
        size_only: bool,
    ) -> int:
        chip_h = metrics.height() + 2 * _CHIP_PADDING_V
        chip_w = metrics.horizontalAdvance(text) + 2 * _CHIP_PADDING_H
        if size_only:
            return chip_w
        actual = QRect(rect.left(), rect.top(), chip_w, chip_h)
        painter.save()
        painter.setBrush(QBrush(bg))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(QRectF(actual), 4, 4)
        painter.setPen(QPen(fg))
        painter.drawText(actual, Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
        return chip_w

    @staticmethod
    def _elide_to_two_lines(
        metrics: QFontMetrics, text: str, width: int
    ) -> list[str]:
        """Wrap ``text`` to at most two lines, eliding the second if it overflows."""
        if width <= 0:
            return [text]
        single = text.strip().replace("\n", " ")
        if metrics.horizontalAdvance(single) <= width:
            return [single]
        # Greedy two-line wrap.
        words = single.split()
        line1: list[str] = []
        idx = 0
        while idx < len(words):
            candidate = " ".join([*line1, words[idx]])
            if metrics.horizontalAdvance(candidate) > width and line1:
                break
            line1.append(words[idx])
            idx += 1
        line2_words = words[idx:]
        line2 = " ".join(line2_words) if line2_words else ""
        if line2 and metrics.horizontalAdvance(line2) > width:
            line2 = metrics.elidedText(line2, Qt.TextElideMode.ElideRight, width)
        return [" ".join(line1), line2] if line2 else [" ".join(line1)]

    @staticmethod
    def _text_pen(option: QStyleOptionViewItem, *, selected: bool) -> QPen:
        if selected:
            return QPen(option.palette.color(QPalette.ColorRole.HighlightedText))
        return QPen(option.palette.color(QPalette.ColorRole.Text))

    @staticmethod
    def _muted_pen(option: QStyleOptionViewItem, *, selected: bool) -> QPen:
        if selected:
            colour = option.palette.color(QPalette.ColorRole.HighlightedText)
            colour.setAlphaF(0.85)
            return QPen(colour)
        return QPen(option.palette.color(QPalette.ColorRole.PlaceholderText))


def _priority_text(priority: str) -> str:
    return (priority or PRIORITY_RECOMMENDED).upper()


def _chip_font(base: QFont) -> QFont:
    """Build the chip font from the row's base font.

    Used by both ``paint()`` (for drawing) and ``sizeHint()`` (for
    width reservation) so the two paths can never disagree on chip
    width — the 105% letter spacing in particular widens the chip
    enough that a sizeHint without it would clip the action text.
    """
    font = QFont(base)
    font.setPointSize(max(8, base.pointSize() - 1))
    font.setWeight(QFont.Weight.DemiBold)
    font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 105)
    return font


def _chip_size(font: QFont, text: str) -> tuple[int, int]:
    """Return ``(width, height)`` of a chip drawn with ``font`` and ``text``."""
    metrics = QFontMetrics(font)
    width = metrics.horizontalAdvance(text) + 2 * _CHIP_PADDING_H
    height = metrics.height() + 2 * _CHIP_PADDING_V
    return width, height


__all__ = ["SUGGESTION_ROLE", "SuggestionDelegate"]
