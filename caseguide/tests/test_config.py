"""Config: SUITE_LLM_MODEL / SUITE_LLM_BASE_URL env vars feed defaults."""

from __future__ import annotations

import pytest

from caseguide.config import DEFAULT_LLM_BASE_URL, DEFAULT_LLM_MODEL, Config


def test_suite_llm_model_env_overrides_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUITE_LLM_MODEL", "granite4:tiny-h")
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_model == "granite4:tiny-h"


def test_suite_llm_model_env_yields_to_user_choice(tmp_path, monkeypatch) -> None:
    ini = tmp_path / "c.ini"
    cfg = Config(path=ini)
    cfg.llm_model = "qwen2.5:7b-instruct"
    cfg.sync()

    monkeypatch.setenv("SUITE_LLM_MODEL", "granite4:tiny-h")
    assert Config(path=ini).llm_model == "qwen2.5:7b-instruct"


@pytest.mark.parametrize("env_value", ["", "   "])
def test_suite_llm_model_empty_env_falls_back(tmp_path, monkeypatch, env_value) -> None:
    monkeypatch.setenv("SUITE_LLM_MODEL", env_value)
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_model == DEFAULT_LLM_MODEL


# ---- SUITE_LLM_BASE_URL --------------------------------------------------


def test_suite_llm_base_url_env_overrides_default(tmp_path, monkeypatch) -> None:
    """Air-gapped launcher exports this so the apps target the bundled
    Ollama on port 11435 instead of the system Ollama on 11434."""
    monkeypatch.setenv("SUITE_LLM_BASE_URL", "http://127.0.0.1:11435/v1")
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_base_url == "http://127.0.0.1:11435/v1"


def test_suite_llm_base_url_env_yields_to_user_choice(tmp_path, monkeypatch) -> None:
    """A user who pointed Settings at a remote endpoint shouldn't have
    that quietly overridden by the launcher's env var."""
    ini = tmp_path / "c.ini"
    cfg = Config(path=ini)
    cfg.llm_base_url = "https://my-server.local:8000/v1"
    cfg.sync()

    monkeypatch.setenv("SUITE_LLM_BASE_URL", "http://127.0.0.1:11435/v1")
    assert Config(path=ini).llm_base_url == "https://my-server.local:8000/v1"


@pytest.mark.parametrize("env_value", ["", "   "])
def test_suite_llm_base_url_empty_env_falls_back(tmp_path, monkeypatch, env_value) -> None:
    monkeypatch.setenv("SUITE_LLM_BASE_URL", env_value)
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_base_url == DEFAULT_LLM_BASE_URL


# ---- remember_case dedupe ------------------------------------------------


def test_remember_case_dedupes_trailing_slash_variant(tmp_path) -> None:
    """Same physical case, different trailing slash, must collapse.

    Regression: ``remember_case`` used exact string equality, so
    ``/cases/foo`` and ``/cases/foo/`` both ended up in the list and
    the recents picker showed duplicates of the same case.
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/foo/")
    assert len(cfg.recent_case_paths) == 1


def test_remember_case_dedupes_dot_components(tmp_path) -> None:
    """``./`` / ``..`` components also count as the same path.

    Operator types ``cd .. && open ./cases/foo`` from one tool and
    ``open /home/me/cases/foo`` from another -- both should yield a
    single recents entry.
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/./foo")
    cfg.remember_case("/cases/bar/../foo")
    assert len(cfg.recent_case_paths) == 1


def test_remember_case_distinguishes_genuinely_different_paths(tmp_path) -> None:
    """Sanity: two truly different cases stay as two entries.

    Guards against the normalisation being too aggressive (e.g.
    collapsing siblings or unrelated cases into one).
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/bar")
    assert len(cfg.recent_case_paths) == 2


def test_remember_case_moves_duplicate_to_head(tmp_path) -> None:
    """Re-opening the same case (cosmetically different path) bumps
    its existing entry to the head instead of leaving a stale older
    copy. Order matters for the recents picker -- newest-first.
    """
    cfg = Config(path=tmp_path / "c.ini")
    cfg.remember_case("/cases/foo")
    cfg.remember_case("/cases/bar")
    cfg.remember_case("/cases/foo/")  # cosmetic variant of foo
    paths = cfg.recent_case_paths
    assert len(paths) == 2
    assert paths[0] == "/cases/foo/"  # newest, bumped to head
    assert paths[1] == "/cases/bar"
