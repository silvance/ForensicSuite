"""HTML and Markdown export."""

from __future__ import annotations

import io
import struct
import zlib
from typing import TYPE_CHECKING

from PIL import Image

from inscription.export import export_forensic_notes, export_html, export_markdown
from inscription.model import DraftStep, EventKind, ResolvedElement, utcnow
from inscription.steps import generate_steps
from inscription.storage import SessionRepository

if TYPE_CHECKING:
    from pathlib import Path


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Build a minimal PNG of a solid colour."""
    img = Image.new("RGB", (width, height), rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000b49444154789c6360000200000500017a5eab3f0000000049454e44ae"
    "426082"
)


def _seed_click(
    repo: SessionRepository,
    *,
    png_bytes: bytes = _TINY_PNG,
    width: int = 1,
    height: int = 1,
    bounding_rect: tuple[int, int, int, int] | None = None,
    click_xy: tuple[int, int] = (1, 1),
    origin: tuple[int, int] = (0, 0),
) -> None:
    shot_dir = repo.session.screenshots_dir
    shot_dir.mkdir(exist_ok=True)
    shot_path = shot_dir / "click.png"
    shot_path.write_bytes(png_bytes)
    shot = repo.add_screenshot(
        relative_path="screenshots/click.png",
        captured_at=utcnow(),
        width=width,
        height=height,
        sha256="abc",
        origin_left=origin[0],
        origin_top=origin[1],
    )
    resolved = repo.add_resolved_element(
        ResolvedElement(
            id=None,
            name="Save",
            control_type="Button",
            confidence=0.9,
            method="uia",
            bounding_rect=bounding_rect,
        )
    )
    repo.append_event(
        kind=EventKind.CLICK,
        button="left",
        x=click_xy[0],
        y=click_xy[1],
        window_title="App",
        process_name="app.exe",
        screenshot_id=shot.id,
        resolved_element_id=resolved.id,
    )


def test_export_html_writes_self_contained_document(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Exportable")
    try:
        _seed_click(repo)
        generate_steps(repo)
        doc = export_html(repo)
    finally:
        repo.close()

    assert doc.path.exists()
    text = doc.path.read_text(encoding="utf-8")
    assert "<!doctype html>" in text
    assert "Exportable" in text
    assert "Save" in text
    assets = doc.path.parent / "assets"
    assert assets.exists()
    assert any(assets.iterdir())


def test_export_crops_screenshot_when_bounding_rect_available(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="CropMe")
    try:
        full = _solid_png(800, 600, (255, 255, 255))
        _seed_click(
            repo,
            png_bytes=full,
            width=800,
            height=600,
            bounding_rect=(300, 250, 400, 310),
            click_xy=(350, 280),
        )
        generate_steps(repo)
        doc = export_html(repo)
    finally:
        repo.close()

    # The staged asset should be smaller than the source screenshot and
    # should not byte-match it (the ring was drawn on top).
    assets = list((doc.path.parent / "assets").glob("step-*.png"))
    assert assets, "expected at least one step asset"
    staged = assets[0]
    assert staged.read_bytes() != full
    with Image.open(staged) as img:
        w, h = img.size
    assert w < 800
    assert h < 600


def _png_dimensions(data: bytes) -> tuple[int, int]:
    # Lightweight sanity helper independent of Pillow — PNG IHDR at offset 8.
    _ = zlib  # keep the import used (zlib is stdlib, safe)
    w, h = struct.unpack(">II", data[16:24])
    return w, h


def test_png_dimensions_helper_works() -> None:
    data = _solid_png(7, 5, (0, 0, 0))
    assert _png_dimensions(data) == (7, 5)


# --------------------------------------------------- markdown


def test_export_markdown_writes_portable_document(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="MD Demo")
    try:
        _seed_click(repo)
        generate_steps(repo)
        doc = export_markdown(repo)
    finally:
        repo.close()

    assert doc.path.exists()
    assert doc.path.suffix == ".md"
    text = doc.path.read_text(encoding="utf-8")
    # Header + step heading + the resolved element name.
    assert text.startswith("# MD Demo")
    assert "## Step 1" in text
    assert "Save" in text
    # Image markdown reference into the sibling assets folder.
    assert "](" in text
    assets = doc.path.parent / f"{doc.path.stem}-assets"
    assert assets.exists()
    assert any(assets.iterdir())


def test_markdown_skips_image_when_step_has_no_screenshot(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="NoShot")
    try:
        event = repo.append_event(kind=EventKind.KEY_PRESS, key="enter", window_title="Notepad")
        assert event.id is not None
        repo.replace_steps(
            [
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Press Enter in Notepad.",
                    source_event_ids=(event.id,),
                )
            ]
        )
        doc = export_markdown(repo)
    finally:
        repo.close()

    text = doc.path.read_text(encoding="utf-8")
    assert "Press Enter in Notepad." in text
    # No image reference for steps that have no screenshot.
    assert "![" not in text


def test_markdown_omits_suppressed_steps(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Hidden")
    try:
        e1 = repo.append_event(kind=EventKind.CLICK, x=1, y=1, button="left")
        e2 = repo.append_event(kind=EventKind.CLICK, x=2, y=2, button="left")
        assert e1.id is not None
        assert e2.id is not None
        saved = repo.replace_steps(
            [
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Visible step.",
                    source_event_ids=(e1.id,),
                ),
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Hidden step.",
                    source_event_ids=(e2.id,),
                ),
            ]
        )
        assert saved[1].id is not None
        repo.set_step_suppressed(saved[1].id, suppressed=True)
        doc = export_markdown(repo)
    finally:
        repo.close()

    text = doc.path.read_text(encoding="utf-8")
    assert "Visible step." in text
    assert "Hidden step." not in text


# --------------------------------------------------- forensic notes


def test_forensic_notes_writes_three_column_table(tmp_path: Path) -> None:
    repo = SessionRepository.create(workspace_root=tmp_path, name="Case 24-CF-0007")
    try:
        e1 = repo.append_event(kind=EventKind.MARKER, text="Loaded E01 file")
        e2 = repo.append_event(kind=EventKind.MARKER, text="Started AXIOM")
        assert e1.id is not None
        assert e2.id is not None
        repo.replace_steps(
            [
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Loaded E01 file into FTK Imager.",
                    result="Hash verified.",
                    source_event_ids=(e1.id,),
                    evidentiary=True,
                ),
                DraftStep(
                    id=None,
                    sequence=0,
                    action="Began processing in AXIOM.",
                    source_event_ids=(e2.id,),
                ),
            ]
        )
        doc = export_forensic_notes(
            repo,
            examiner="A. Smith",
            case_reference="24-CF-0007",
        )
    finally:
        repo.close()

    assert doc.path.exists()
    text = doc.path.read_text(encoding="utf-8")
    # Header carries the case metadata.
    assert "A. Smith" in text
    assert "24-CF-0007" in text
    # Both steps render and the result column has the action's outcome.
    assert "Loaded E01 file into FTK Imager." in text
    assert "Hash verified." in text
    assert "Began processing in AXIOM." in text
    # Three-column table headings are present.
    assert "Time/Date" in text
    assert "Action" in text
    assert "Result" in text
    # Evidentiary row gets the row class so styling can pick it up.
    assert 'class="evidentiary"' in text


# ------------------------------------------------- multi-monitor origins


def _png_with_red_block(
    width: int, height: int, block: tuple[int, int, int, int]
) -> bytes:
    """White PNG with a red rectangle at image coords ``block``."""
    img = Image.new("RGB", (width, height), (255, 255, 255))
    for px in range(block[0], block[2]):
        for py in range(block[1], block[3]):
            img.putpixel((px, py), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _has_red(png_path: Path) -> bool:
    with Image.open(png_path) as img:
        rgb = img.convert("RGB")
        return any(
            rgb.getpixel((x, y)) == (255, 0, 0)
            for x in range(rgb.width)
            for y in range(rgb.height)
        )


def test_export_crop_translates_secondary_monitor_origin(tmp_path: Path) -> None:
    """A click on a monitor at global x=1920 must crop the RIGHT region.

    Regression: UIA bounding rects are global screen coordinates, but
    the screenshot is a single monitor whose pixel (0,0) sits at the
    monitor's global origin. The crop treated screen coords as image
    coords, so a click at screen x=2000 on a monitor spanning
    1920..2720 cropped around image x=2000 -- clamped to a sliver of
    the monitor's right edge, nowhere near the clicked element. The
    court exhibit showed the wrong UI region.

    Setup: 800x600 "secondary monitor" screenshot with a red block at
    IMAGE coords (10, 300)-(180, 400). The element's SCREEN rect is
    (1930, 300, 2100, 400) -- the same block seen through a monitor
    origin of (1920, 0). A correct crop contains red; the broken one
    contained only white edge pixels.
    """
    repo = SessionRepository.create(workspace_root=tmp_path, name="MultiMon")
    try:
        png = _png_with_red_block(800, 600, (10, 300, 180, 400))
        _seed_click(
            repo,
            png_bytes=png,
            width=800,
            height=600,
            bounding_rect=(1930, 300, 2100, 400),
            click_xy=(2000, 350),
            origin=(1920, 0),
        )
        generate_steps(repo)
        doc = export_html(repo)
    finally:
        repo.close()

    assets = list((doc.path.parent / "assets").iterdir())
    assert len(assets) == 1
    staged = assets[0]
    with Image.open(staged) as img:
        # A real crop happened (not the full-frame degenerate fallback).
        assert img.width < 800
    assert _has_red(staged), (
        "Cropped exhibit does not contain the clicked element -- "
        "monitor origin was not subtracted before cropping"
    )


def test_export_crop_unchanged_for_primary_monitor(tmp_path: Path) -> None:
    """Origin (0,0) -- legacy rows and primary-monitor captures -- is a
    no-op translation; the crop behaves exactly as before."""
    repo = SessionRepository.create(workspace_root=tmp_path, name="PrimaryMon")
    try:
        png = _png_with_red_block(800, 600, (10, 300, 180, 400))
        _seed_click(
            repo,
            png_bytes=png,
            width=800,
            height=600,
            bounding_rect=(10, 300, 180, 400),
            click_xy=(90, 350),
            origin=(0, 0),
        )
        generate_steps(repo)
        doc = export_html(repo)
    finally:
        repo.close()

    assets = list((doc.path.parent / "assets").iterdir())
    staged = assets[0]
    with Image.open(staged) as img:
        assert img.width < 800
    assert _has_red(staged)
