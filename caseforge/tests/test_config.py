"""Config: remember_case dedupes cosmetic path variants."""

from __future__ import annotations

from typing import TYPE_CHECKING

from caseforge.config import Config

if TYPE_CHECKING:
    from pathlib import Path


def test_remember_case_dedupes_trailing_slash_variant(tmp_path: Path) -> None:
    """Same physical case, different trailing slash, must collapse.

    Regression: ``remember_case`` used exact string equality, so
    ``/cases/foo`` and ``/cases/foo/`` both ended up in the list and
    the recents picker showed duplicates of the same case.
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/foo/")
    assert len(cfg.recent_case_paths) == 1


def test_remember_case_dedupes_dot_components(tmp_path: Path) -> None:
    """``./`` / ``..`` components also count as the same path."""
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/./foo")
    cfg.remember_case("/cases/bar/../foo")
    assert len(cfg.recent_case_paths) == 1


def test_remember_case_distinguishes_genuinely_different_paths(tmp_path: Path) -> None:
    """Sanity: two truly different cases stay as two entries."""
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/bar")
    assert len(cfg.recent_case_paths) == 2


def test_remember_case_moves_duplicate_to_head(tmp_path: Path) -> None:
    """Re-opening the same case (cosmetically different path) bumps
    its existing entry to the head instead of leaving a stale older
    copy. Order matters for the recents picker -- newest-first.
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/bar")
    cfg.remember_case("/cases/foo/")
    paths = cfg.recent_case_paths
    assert len(paths) == 2
    assert paths[0] == "/cases/foo/"
    assert paths[1] == "/cases/bar"


def test_remember_case_respects_limit(tmp_path: Path) -> None:
    """The limit cap still bounds the list size after dedupe."""
    cfg = Config(path=tmp_path / "c.ini")
    for i in range(20):
        cfg.remember_case(f"/cases/case-{i}")
    # Default limit is 12 -- verify by passing the same explicit cap.
    assert len(cfg.recent_case_paths) <= 12
