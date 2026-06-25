"""Shared bits for the Inscription / CaseForge / CaseGuide suite.

Five halves now:

- :mod:`suite_common.atomic` — crash-safe temp+rename+fsync writer
  used by every persistent JSON / HTML / Markdown / manifest path in
  the suite.
- :mod:`suite_common.llm` — OpenAI-compatible chat-completions client used
  by Inscription's step rewriter and CaseGuide's suggestions refiner.
- :mod:`suite_common.coerce` — JSON-tolerant coercion helpers used by every
  app that reads case.json / suggestions.json / manifest.json.
- :mod:`suite_common.bundle` — read the air-gapped bundle's version.json
  stamp at runtime so each app's About dialog can show the build
  provenance the operator actually has on their machine.
- :mod:`suite_common.paths` — per-user data-root helper so each app's
  ``paths.py`` doesn't have to duplicate the LOCALAPPDATA-vs-
  ~/.local/share decision.
"""

from suite_common.atomic import atomic_write_text
from suite_common.bundle import bundle_root, read_version_info
from suite_common.coerce import (
    coerce_bool,
    coerce_int,
    parse_iso,
    parse_optional_iso,
    string_list,
)
from suite_common.errors import friendly_storage_error, show_error
from suite_common.llm import (
    DEFAULT_LLM_BASE_URL,
    DEFAULT_LLM_MODEL,
    DEFAULT_LLM_TIMEOUT_S,
    DEFAULT_TEMPERATURE,
    ChatClient,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMRequestError,
    LLMResponseError,
    list_available_models,
)
from suite_common.paths import default_data_root, ensure_dirs

__all__ = [
    "DEFAULT_LLM_BASE_URL",
    "DEFAULT_LLM_MODEL",
    "DEFAULT_LLM_TIMEOUT_S",
    "DEFAULT_TEMPERATURE",
    "ChatClient",
    "LLMClient",
    "LLMConfigError",
    "LLMError",
    "LLMRequestError",
    "LLMResponseError",
    "atomic_write_text",
    "bundle_root",
    "coerce_bool",
    "coerce_int",
    "default_data_root",
    "ensure_dirs",
    "friendly_storage_error",
    "list_available_models",
    "show_error",
    "parse_iso",
    "parse_optional_iso",
    "read_version_info",
    "string_list",
]
