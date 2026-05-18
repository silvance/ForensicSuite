"""HTML exporter.

Produces a self-contained ``.html`` file in the session's ``exports/``
directory. Screenshots are staged into ``exports/assets/`` so the output
folder is portable on its own.

For each step with a resolved UIA element, the staged image is a
**crop around the clicked element with a click ring drawn on it** — the
raw layer (``screenshots/event-*.png``) remains untouched, so provenance
is preserved; the exporter simply renders a tighter, annotated variant
for human consumption. Steps without a resolved bounding rect fall back
to the full screenshot.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from inscription.export._common import (
    atomic_write_text,
    build_event_resolver,
    select_primary_event,
    stage_step_asset,
)
from inscription.model import ExportDocument, utcnow
from inscription.util import format_clock_time

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from inscription.model import (
        DraftStep,
        RawEvent,
        ResolvedElement,
        ScreenshotArtifact,
        Session,
    )
    from inscription.storage import SessionRepository

logger = logging.getLogger(__name__)


_CSS = """
:root {
  color-scheme: light dark;
  --fg: #111;
  --muted: #666;
  --rule: #e5e5e5;
  --accent: #1f6feb;
}
@media (prefers-color-scheme: dark) {
  :root { --fg: #f3f3f3; --muted: #aaa; --rule: #333; --accent: #58a6ff; }
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  color: var(--fg);
  max-width: 960px;
  margin: 2rem auto;
  padding: 0 1.5rem;
  line-height: 1.55;
}
header { border-bottom: 1px solid var(--rule); padding-bottom: 1rem; margin-bottom: 2rem; }
header h1 { margin: 0 0 .25rem 0; }
header .meta { color: var(--muted); font-size: .9rem; }
ol.steps { list-style: none; padding: 0; counter-reset: step; }
ol.steps > li {
  counter-increment: step;
  display: grid;
  grid-template-columns: 2.5rem 1fr;
  gap: .75rem;
  padding: 1.25rem 0;
  border-bottom: 1px solid var(--rule);
}
ol.steps > li::before {
  content: counter(step);
  font-weight: 600;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}
.step-time {
  display: inline-block;
  margin-bottom: .35rem;
  color: var(--muted);
  font-size: .8rem;
  font-variant-numeric: tabular-nums;
  letter-spacing: .03em;
}
.step-action { margin: 0 0 .35rem 0; font-weight: 500; }
.step-result {
  margin: 0 0 .5rem 0;
  padding: .25rem 0 .25rem .65rem;
  border-left: 2px solid var(--accent);
  color: var(--muted);
}
.step-shot img {
  max-width: 100%;
  border: 1px solid var(--rule);
  border-radius: 4px;
}
footer { color: var(--muted); font-size: .8rem; margin-top: 2rem; }
"""


def export_html(
    repository: SessionRepository,
    *,
    destination: Path | None = None,
) -> ExportDocument:
    """Render the session as HTML."""
    session = repository.session
    steps = repository.list_steps()
    screenshots = {s.id: s for s in repository.list_screenshots() if s.id is not None}
    events_by_id: dict[int, RawEvent] = {
        e.id: e for e in repository.list_events() if e.id is not None
    }

    if destination is None:
        destination = session.exports_dir / f"{session.root.name}.html"
    destination.parent.mkdir(parents=True, exist_ok=True)
    assets_dir = destination.parent / "assets"
    assets_dir.mkdir(exist_ok=True)

    resolve = build_event_resolver(repository)

    body_parts = [_render_header(session)]
    body_parts.append('<ol class="steps">')
    for step in steps:
        body_parts.append(
            _render_step(
                step=step,
                screenshots=screenshots,
                events_by_id=events_by_id,
                resolver=resolve,
                session_root=session.root,
                assets_dir=assets_dir,
            )
        )
    body_parts.append("</ol>")
    body_parts.append(_render_footer(session))

    html_doc = _wrap(title=session.info.name, body="\n".join(body_parts))
    atomic_write_text(destination, html_doc)
    logger.info("Exported %s to %s", session.info.name, destination)

    return ExportDocument(
        session_name=session.info.name,
        format="html",
        path=destination,
        generated_at=utcnow(),
    )


# -------------------------------------------------------------- rendering


def _wrap(*, title: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n"
        "<body>\n"
        f"{body}\n"
        "</body>\n"
        "</html>\n"
    )


def _render_header(session: Session) -> str:
    info = session.info
    started = info.started_at.strftime("%Y-%m-%d %H:%M")
    ended = info.ended_at.strftime("%Y-%m-%d %H:%M") if info.ended_at else "in progress"
    return (
        "<header>\n"
        f"<h1>{html.escape(info.name)}</h1>\n"
        f'<div class="meta">Recorded {html.escape(started)} &ndash; {html.escape(ended)}</div>\n'
        "</header>\n"
    )


def _render_footer(session: Session) -> str:
    version = session.info.recorder_version or "unknown"
    return f"<footer>\nGenerated by Inscription (recorder {html.escape(version)}).\n</footer>\n"


def _render_step(
    *,
    step: DraftStep,
    screenshots: dict[int, ScreenshotArtifact],
    events_by_id: dict[int, RawEvent],
    resolver: Callable[[RawEvent], ResolvedElement | None],
    session_root: Path,
    assets_dir: Path,
) -> str:
    primary = select_primary_event(step, events_by_id)
    action_text = html.escape(step.action).replace("\n", "<br>")
    parts = ["<li>", '<div class="step-body">']
    if primary is not None:
        ts = html.escape(format_clock_time(primary.occurred_at))
        parts.append(f'<div class="step-time">{ts}</div>')
    parts.append(f'<p class="step-action">{action_text}</p>')
    if step.result.strip():
        result_text = html.escape(step.result).replace("\n", "<br>")
        parts.append(f'<p class="step-result">{result_text}</p>')
    shot = screenshots.get(step.screenshot_id) if step.screenshot_id else None
    if shot is not None:
        element = resolver(primary) if primary else None
        asset_name = stage_step_asset(
            step=step,
            shot=shot,
            primary_event=primary,
            element=element,
            session_root=session_root,
            assets_dir=assets_dir,
        )
        # On stage failure, omit the <img> entirely. Falling back to the
        # session-relative path (``screenshots/event-1.png``) produces a
        # broken-or-worse image reference once the HTML is moved off the
        # build host: at best it 404s; at worst it resolves to whatever
        # unrelated file lives at that path on the recipient's machine.
        if asset_name is not None:
            src = f"assets/{asset_name}"
            alt = html.escape(step.action)[:120]
            parts.append(
                f'<div class="step-shot"><img src="{html.escape(src)}" alt="{alt}"></div>'
            )
    parts.append("</div>")
    parts.append("</li>")
    return "\n".join(parts)
