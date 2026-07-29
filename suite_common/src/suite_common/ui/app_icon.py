"""Shared app-icon scaffolding for the suite apps.

Each suite app (Inscription / CaseForge / CaseGuide) renders its
own emblem (an "I" + recording dot, a case folder, a checklist) but
the rest of the work — the rounded accent shell, the gradient, the
top highlight, the size set, the small-size detail cutoff — is the
same. That shared work lives here. Each app's ``ui/app_icon.py``
defines its own emblem-drawing function and asks
:func:`build_app_icon` to render an icon around it.

The shells deliberately stay identical across apps so the suite
reads as one family in the taskbar.
"""

from __future__ import annotations

from typing import Protocol

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

from suite_common.ui.style import LIGHT, Palette

#: Sizes we render the icon at. Covers Windows taskbar (16, 24, 32, 48,
#: 256) and macOS dock (32, 64, 128, 256, 512).
_SIZES = (16, 24, 32, 48, 64, 128, 256, 512)

#: Below this size we skip the inner highlight + gradient -- they look
#: muddy at small sizes where every pixel counts.
DETAIL_MIN_SIZE = 32


class Emblem(Protocol):
    """A callable that paints the per-app emblem on top of the shell.

    Receives a :class:`QPainter` already positioned at (0, 0) on a
    transparent ``size`` x ``size`` pixmap, with antialiasing on.
    The shell has already been drawn. The emblem should respect
    :data:`DETAIL_MIN_SIZE` (skip fine detail below it) so the small
    sizes stay legible.
    """

    def __call__(self, painter: QPainter, size: int) -> None: ...


def build_app_icon(emblem: Emblem, palette: Palette | None = None) -> QIcon:
    """Return a multi-resolution :class:`QIcon` for ``emblem``.

    Renders the standard accent-shell at every size in :data:`_SIZES`,
    then asks ``emblem`` to paint the per-app glyph on top. The
    palette default is :data:`LIGHT` so the icon stays consistent
    across light/dark system themes (the icon doesn't know which
    theme is active when the OS asks for it).
    """
    p = palette or LIGHT
    icon = QIcon()
    for size in _SIZES:
        icon.addPixmap(_render(size, p, emblem))
    return icon


def _render(size: int, p: Palette, emblem: Emblem) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    try:
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        _draw_shell(painter, size, accent=p.accent, accent_hover=p.accent_hover)
        emblem(painter, size)
    finally:
        painter.end()
    return pm


def _draw_shell(painter: QPainter, size: int, *, accent: str, accent_hover: str) -> None:
    """Rounded accent square with gradient + top highlight.

    Stays identical across Inscription / CaseForge / CaseGuide so the
    three icons read as siblings in a taskbar. Per-app emblems are
    drawn afterward by :class:`Emblem` callables.
    """
    radius = size * 0.22
    body = QRectF(0.5, 0.5, size - 1, size - 1)
    path = QPainterPath()
    path.addRoundedRect(body, radius, radius)

    if size >= DETAIL_MIN_SIZE:
        # Vertical gradient: lighter at top, the configured accent at
        # bottom. Reads as ambient lighting, not a colour shift.
        gradient = QLinearGradient(QPointF(0, 0), QPointF(0, size))
        gradient.setColorAt(0.0, QColor(accent_hover))
        gradient.setColorAt(1.0, QColor(accent))
        painter.fillPath(path, QBrush(gradient))
    else:
        painter.fillPath(path, QBrush(QColor(accent)))

    if size >= DETAIL_MIN_SIZE:
        # Inner highlight along the top: a thin lighter band that gives
        # the icon a slight 3D feel without the gradient carrying that
        # work alone.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 38))
        painter.drawRoundedRect(
            QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.20),
            radius * 0.5,
            radius * 0.5,
        )
