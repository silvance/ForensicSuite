# Changelog

All notable changes to CaseGuide will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Schema-version refuse-on-future.** Loading a `suggestions.json`
  written by a newer CaseGuide build now raises explicitly rather
  than best-effort parsing with current-version defaults. CaseGuide
  is the writer of the file, so silently round-tripping a future
  version would truncate the future-only fields on the next save.

### Changed

- **LLM JSON parse helpers moved to `suite_common`.** The lenient
  parse / brace-extract / code-fence-strip helpers used by Refine
  were duplicated verbatim between Inscription and CaseGuide. Both
  apps now share one implementation via
  `suite_common.parse_json_lenient` so a fix in one place lands in
  both. Practically: small / weakly instruction-tuned local models
  that prepend `"Sure! Here is the JSON:"` to their reply now
  recover reliably in CaseGuide too.
- **Suggestions writes go through the shared atomic helper.** Same
  fsync-backed `suite_common.atomic.atomic_write_text` as CaseForge
  now -- a power loss during a save can't leave a truncated
  `suggestions.json` on disk.
- **Recents list deduplicates cosmetic path variants.** Same case
  opened via different path representations (trailing slash, `./`
  components, Windows drive-letter case) no longer accumulates
  duplicates in the picker.
- **`recent_case_paths` survives `;` in paths.** Stored as a
  JSON-encoded list now; legacy `;`-separated values are read via a
  fallback for one upgrade cycle.

### Fixed

- **Playbook matcher is no longer over-eager on substring matches.**
  Bidirectional substring matching in the matcher meant a scope
  value of `"i"` matched a rule of `"ios"`, firing playbooks against
  partial / incomplete scope tokens. Now forward-containment only:
  rule `"iphone"` still matches scope `"iphone-13"`, but partial
  scope fragments don't fire unrelated playbooks.

## [0.1.0] — 2026-04 (initial)

### Added

- **Two-pane exam-coach workspace.** Scope on the left (read from the
  case's `case.json`), suggestions on the right. Suggestions are
  produced deterministically by `caseguide.generator` from matched
  playbooks, then optionally refined by a local LLM via
  `Refine with AI` -- the LLM may reword, drop, reorder, or add a
  small number of scope-specific entries on top of the deterministic
  draft.
- **Playbook library.** Procedural exam playbooks bundled with the
  app, scoped by exam type (CI / CSAM / ICAC / etc.), primary tool
  (Magnet AXIOM, X-Ways, FTK, Autopsy, Cellebrite, …), and free-form
  device-class / evidence-item / keyword rules. Matcher walks the
  scope and surfaces the ones that apply.
- **Two more Autopsy playbooks — Timeline + Keyword & Tagging.**
  Coverage for the two most common Autopsy ingest-then-analyse
  flows.
- **Persistent completion state.** Each suggestion has a `completed`
  checkbox and a `completed_at` timestamp; saved into
  `suggestions.json` so the operator's progress survives close /
  reopen.
- **Markdown checklist export.** `Copy checklist to clipboard` and
  `File → Export checklist as Markdown` both render the suggestions
  list as a GitHub-flavoured markdown checklist, suitable for
  pasting into a case file, ticket, or the forensic notes.
- **Per-app icon.** Distinct teal-gradient icon (Inscription gets
  amber, CaseForge gets purple).

### Changed

- **Operator can choose between bundled models.** Settings dialog
  shows the bundled Ollama's `/api/tags` so the operator can pick a
  smaller or larger model without editing the config file by hand.
- **Default LLM timeout raised to 600s.** Local models on modest
  hardware can take well over the previous 120s default for the
  Refine step on a large suggestions list; the new default cleans
  up the most common "the model timed out" support ticket.
- **Bundled Ollama listens on port 11435.** The air-gapped bundle's
  Ollama instance lives on a non-default port (with the
  `SUITE_LLM_BASE_URL` env var exported by the launcher) so it
  doesn't collide with a system-wide Ollama on 11434.

### Security

- **Prompt-injection delimiters carry a per-call nonce.** The user
  prompt wraps the case scope + draft suggestions in
  `<case_data:NONCE>...</case_data:NONCE>` where NONCE is a fresh
  96-bit hex token per call. Earlier static delimiters were
  vulnerable to a hostile scope summary containing
  `</case_data>` verbatim because `json.dumps` doesn't escape
  `<` `>` `/`; the nonce closes that hole.
