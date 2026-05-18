"""Render a DOCX template against a :class:`ReportContext`.

Wraps :class:`docxtpl.DocxTemplate` so the rest of the codebase
imports docxtpl in exactly one place — easier to swap, easier to
mock, easier to give a useful error message when the dependency is
missing.

The renderer is a pure ``(template_path, context, output_path) →
None`` function with no Qt or stateful surface, so it's testable
headless and reusable from CLI, scripted exports, or the (later)
Reports tab.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from caseforge.report.context import ReportContext

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when the template can't be loaded, parsed, or rendered."""


def render_report(
    *,
    template_path: Path,
    context: ReportContext,
    output_path: Path,
) -> None:
    """Render ``template_path`` against ``context`` and write to ``output_path``.

    Raises :class:`RenderError` for any failure the operator can
    plausibly fix (missing template, bad Jinja syntax, write-blocked
    output dir). Programmer errors propagate unchanged so the stack
    trace is preserved during development.
    """
    if not template_path.is_file():
        msg = f"Template not found or not a regular file: {template_path}"
        raise RenderError(msg)

    try:
        from docxtpl import DocxTemplate  # noqa: PLC0415 - optional extra
        from jinja2 import Environment, StrictUndefined  # noqa: PLC0415 - optional extra
    except ImportError as exc:
        msg = (
            "Report rendering requires the optional 'docxtpl' dependency. "
            "Install caseforge with the 'report' extra: pip install 'caseforge[report]'."
        )
        raise RenderError(msg) from exc

    try:
        template = DocxTemplate(str(template_path))
    except Exception as exc:
        # docxtpl raises a mix of zipfile / xml / KeyError exceptions
        # for malformed DOCX inputs. Funnel them all into RenderError
        # with a message that names the file.
        msg = f"Could not open template {template_path}: {exc}"
        raise RenderError(msg) from exc

    # StrictUndefined turns ``{{ case.bogus }}`` into a hard
    # UndefinedError instead of silently rendering an empty string.
    # That's the right tradeoff for forensic templates: a typo in a
    # token name should surface as a render failure, not as a blank
    # spot in the finished report that the operator might miss.
    #
    # autoescape=False because docxtpl handles XML escaping itself.
    # HTML autoescape would mangle every ``&``, ``<``, ``>``, ``'``,
    # ``"`` in case data into entity refs visible in the rendered docx.
    # S701 doesn't apply here -- this template renders DOCX, not HTML.
    jinja_env = Environment(undefined=StrictUndefined, autoescape=False)  # noqa: S701

    try:
        template.render(context.as_template_dict(), jinja_env=jinja_env)
    except Exception as exc:
        # Most commonly: Jinja2 TemplateSyntaxError (unbalanced
        # ``{% %}`` tags, unknown filter, etc.) or UndefinedError if
        # a token names a non-existent attribute. Surfacing the class
        # name helps the operator distinguish syntax from data issues.
        msg = f"Template render failed ({type(exc).__name__}): {exc}"
        raise RenderError(msg) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        template.save(str(output_path))
    except OSError as exc:
        msg = f"Could not write rendered report to {output_path}: {exc}"
        raise RenderError(msg) from exc
    logger.info("Rendered report from %s to %s", template_path, output_path)
