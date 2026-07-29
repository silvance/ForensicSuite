# Changelog

All notable changes to CaseForge will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Launch Whispr.** Case menu + Settings path field for the Whispr
  audio-transcription GUI (silvance/whisper.py — local Whisper with
  speaker diarization). Resolves like the other sibling tools:
  explicit path → PATH → air-gapped bundle sibling (`Whispr\Whispr.exe`)
  → `python -m whispr`. Launched without a `--case-dir` argument
  (Whispr picks files in its own UI).

- **First-run onboarding dialog.** Greets a fresh install with a guided
  step through workspace root, examiner identity defaults, and
  launcher paths. Skippable if you'd rather configure via
  `Edit → Settings`. The choice is remembered so you don't see it
  again on the next launch.
- **`--case-dir` CLI flag.** `caseforge --case-dir <path>` opens the
  named case directly, mirroring Inscription's symmetric flag. Used
  by integrations that want to deep-link into a specific case.

### Changed

- **Atomic case.json writes share the suite's fsync-backed helper.**
  Previously CaseForge had its own temp+rename pattern; it now goes
  through `suite_common.atomic.atomic_write_text`, which adds an
  `fsync` pair so a power loss during a save can't leave a truncated
  `case.json` on disk.
- **Recents list deduplicates cosmetic path variants.** Same case
  opened via `C:\cases\foo`, `C:\cases\foo\`, and `C:\cases\.\foo`
  used to produce three separate entries in the case browser; now
  they collapse to one via `os.path.normcase(os.path.normpath(p))`.
- **`case.json` reader rejects future schema versions explicitly.**
  Reading a `case.json` written by a newer CaseForge build with a
  bumped schema now raises rather than silently best-effort loading
  with current-version defaults (which would lose any new fields on
  the next round-trip).
- **DOCX report templates no longer get HTML-escaped.** The Jinja2
  environment was configured with `autoescape=True`, which mangled
  every `&` `<` `>` `'` `"` in case data into HTML entity refs
  visible in the rendered docx. docxtpl handles XML escaping itself,
  so autoescape is now off.

### Fixed

- **`recent_case_paths` survives paths containing `;`.** The recents
  list used `;`-joined storage, but `;` is a legal Windows
  file/directory character — a case at `C:\cases\jan;feb-2026` would
  silently vanish after the next save/reopen. Now JSON-encoded; legacy
  `;`-separated values are read via a fallback for one upgrade cycle.
- **Custody timestamps no longer drift by the local UTC offset.** The
  custody panel was branding a naive Qt-dialog datetime as UTC
  directly via `replace(tzinfo=UTC)`. Now `astimezone(UTC)` so the
  examiner's local wall-clock entry is correctly converted to UTC
  for storage.

## [0.1.0] — 2026-04 (initial)

### Added

- **Three-pane case workspace.** Scope / examiner / custody panels
  driven off a single `case.json` artefact in the case directory.
  Saves are atomic. The same `case.json` is the contract for the
  rest of the suite — Inscription, CaseGuide, and the report
  builder all read it directly.
- **Report rendering — Phase 1 (CLI).** `caseforge.report` generates
  a DOCX report by walking the case's Inscription sessions and the
  `evidentiary` flag on each draft step. UI integration arrives in a
  later release; for now the CLI takes the case directory.
- **Inscription launcher.** `Launch Inscription` opens the configured
  Inscription executable against the open case directory so steps
  land in the right session folder. Path is configurable in
  `Edit → Settings`; defaults to `python -m inscription` when unset
  (handy in dev).
- **CaseGuide launcher.** Same shape as the Inscription launcher.
- **Case browser.** Lists every case under the workspace root plus
  out-of-workspace recents. Archive moves a case into
  `<workspace>/_archive/`; Delete removes it after confirmation.
- **First-class examiner identity defaults.** Name, organisation,
  and badge id default into every new case via
  `Edit → Settings → Examiner`.
