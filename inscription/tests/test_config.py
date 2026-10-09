"""Config: LLM-related settings round-trip through QSettings."""

from __future__ import annotations

import pytest

from inscription.config import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT_S,
    Config,
)


def test_llm_defaults(tmp_path) -> None:
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_enabled is False
    assert cfg.llm_base_url == DEFAULT_LLM_BASE_URL
    assert cfg.llm_model == DEFAULT_LLM_MODEL
    assert cfg.llm_timeout_s == DEFAULT_LLM_TIMEOUT_S
    assert cfg.llm_api_key is None


def test_llm_values_round_trip(tmp_path) -> None:
    ini = tmp_path / "c.ini"
    cfg = Config(path=ini)
    cfg.llm_enabled = True
    cfg.llm_base_url = "http://localhost:1234/v1"
    cfg.llm_model = "gemma2:9b"
    cfg.llm_timeout_s = 60.0
    cfg.llm_api_key = "sk-test"
    cfg.sync()

    # New Config instance reads back from disk.
    reopened = Config(path=ini)
    assert reopened.llm_enabled is True
    assert reopened.llm_base_url == "http://localhost:1234/v1"
    assert reopened.llm_model == "gemma2:9b"
    assert reopened.llm_timeout_s == 60.0
    assert reopened.llm_api_key == "sk-test"


def test_llm_api_key_clears_to_none(tmp_path) -> None:
    cfg = Config(path=tmp_path / "c.ini")
    cfg.llm_api_key = "x"
    cfg.llm_api_key = None
    assert cfg.llm_api_key is None


def test_llm_timeout_coerces_bad_value(tmp_path) -> None:
    ini = tmp_path / "c.ini"
    ini.write_text("[llm]\ntimeout_s=not-a-number\n", encoding="utf-8")
    cfg = Config(path=ini)
    assert cfg.llm_timeout_s == DEFAULT_LLM_TIMEOUT_S


def test_auto_screenshot_default_is_true(tmp_path) -> None:
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.auto_screenshot is True


def test_auto_screenshot_round_trips_false(tmp_path) -> None:
    ini = tmp_path / "c.ini"
    cfg = Config(path=ini)
    cfg.auto_screenshot = False
    cfg.sync()
    assert Config(path=ini).auto_screenshot is False


def test_suite_llm_model_env_overrides_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SUITE_LLM_MODEL", "granite4:tiny-h")
    cfg = Config(path=tmp_path / "c.ini")
    assert cfg.llm_model == "granite4:tiny-h"


def test_suite_llm_model_env_yields_to_user_choice(tmp_path, monkeypatch) -> None:
    """An explicit per-user choice in QSettings beats the launcher's hint."""
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


# ---- defaults pinned by a Settings visit must not override the launcher --
# Field bug: Settings persisted every field on OK, so one visit wrote the
# built-in defaults (gemma4:latest on 11434). On a bundle carrying a
# different model that pinned a guaranteed 404 / connection-refused that
# no launcher choice could fix.

_BUNDLED_MODEL = "qwen2.5:7b-instruct-q5_K_M"
_BUNDLED_URL = "http://127.0.0.1:11435/v1"


def _legacy_pinned_ini(tmp_path):
    """An INI exactly as the old Settings dialog left it after one OK."""
    ini = tmp_path / "c.ini"
    ini.write_text(
        f"[llm]\nmodel={DEFAULT_LLM_MODEL}\nbase_url={DEFAULT_LLM_BASE_URL}\n",
        encoding="utf-8",
    )
    return ini


def test_legacy_pinned_defaults_yield_to_the_launcher(tmp_path, monkeypatch) -> None:
    ini = _legacy_pinned_ini(tmp_path)
    monkeypatch.setenv("SUITE_LLM_MODEL", _BUNDLED_MODEL)
    monkeypatch.setenv("SUITE_LLM_BASE_URL", _BUNDLED_URL)
    cfg = Config(path=ini)
    assert cfg.llm_model == _BUNDLED_MODEL
    assert cfg.llm_base_url == _BUNDLED_URL


def test_settings_ok_without_changes_pins_nothing(tmp_path, monkeypatch) -> None:
    """Saving the values the dialog displayed must not freeze them: the
    next launcher session's choice still applies."""
    ini = tmp_path / "c.ini"
    monkeypatch.setenv("SUITE_LLM_MODEL", _BUNDLED_MODEL)
    cfg = Config(path=ini)
    cfg.llm_model = cfg.llm_model  # what the dialog's commit() does
    cfg.llm_base_url = cfg.llm_base_url
    cfg.sync()

    monkeypatch.setenv("SUITE_LLM_MODEL", "granite4:tiny-h")
    assert Config(path=ini).llm_model == "granite4:tiny-h"


def test_genuine_override_still_survives_the_launcher(tmp_path, monkeypatch) -> None:
    ini = tmp_path / "c.ini"
    monkeypatch.setenv("SUITE_LLM_MODEL", _BUNDLED_MODEL)
    cfg = Config(path=ini)
    cfg.llm_model = "llama3.3:70b-instruct-q4_K_M"
    cfg.sync()
    assert Config(path=ini).llm_model == "llama3.3:70b-instruct-q4_K_M"
