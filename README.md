# Inscription Suite

Three forensic-exam desktop apps + a shared LLM helper library.

| Package | What it does |
|---|---|
| **suite_common** | Shared LLM client + JSON-tolerant coercion helpers (no UI; depended on by all three apps) |
| **Inscription** | Capture a Windows workflow, generate an annotated step guide |
| **CaseForge** | Case intake, chain-of-custody, and report generation |
| **CaseGuide** | LLM-assisted exam coach that suggests next actions |
| *(optional)* **Whisper / Whispr** | Local speech-to-text for evidence audio — `pip install git+https://github.com/silvance/whisper.py.git`. Powers Inscription's "Transcribe audio evidence" and CaseForge's "Launch Whispr" |

---

## Pick your path

```
                       Are you the operator who will USE the suite?
                                       │
                  ┌────────────────────┼─────────────────────────┐
                  │                    │                         │
            yes, online             yes, offline             no — I'm a dev
                  │                    │                         │
                  ▼                    ▼                         ▼
        Install Ollama on the  Receive a USB bundle      Run the apps from a
        workstation, follow    from the build operator,  source checkout for
        SETUP.md.              double-click              development / tests.
                               Install-Suite.cmd        See SETUP.md.
                               (Windows) or run
                               ./install.sh (Linux).
                               See AIR_GAPPED.md.
                                       ▲
                                       │
                       Are you the BUILD OPERATOR producing the USB bundle?
                                       │
                                       ▼
                       Double-click Build-Bundle.bat (Windows) or
                       ./Build-Bundle.sh (Linux) at the repo root.
                       See AIR_GAPPED.md for the underlying scripts and flags.
```

Detailed docs:

- **[SETUP.md](SETUP.md)** — dev environment: shared venv, editable installs, running each app, tests.
- **[AIR_GAPPED.md](AIR_GAPPED.md)** — building, transferring, and installing the offline USB bundle.
- Per-app: [inscription/README.md](inscription/README.md), [caseforge/README.md](caseforge/README.md), [caseguide/README.md](caseguide/README.md).

---

## Scripts at a glance

Two top-level pipelines: **build the apps** and **build the air-gapped bundle**. They nest — `Build-Bundle.*` calls `prepare-bundle.*` calls `package-airgapped.*` calls `build.*`. Use the highest level that does what you need.

### Build the apps (developer)

| Script | What it does | When to use |
|---|---|---|
| `build.sh` / `build.ps1` | Runs PyInstaller for each app; drops one-folder bundles into `<app>/dist/`. **Does not** bundle Ollama or models. | You want a runnable `.exe` / ELF for local testing, no air-gap concerns. |
| `scripts/run-all-tests.sh` / `.ps1` | Runs `pytest` against all four packages in sequence. | Anytime you want a quick green/red across the whole repo. |

### Build the air-gapped bundle (build operator)

| Script | What it does | When to use |
|---|---|---|
| `Build-Bundle.bat` (Win) / `Build-Bundle.sh` (Linux) | Double-clickable wrapper. Sets up `.venv` on first run, verifies Ollama, pops a folder picker for the USB drive, then runs the full pipeline. | You don't want to remember flags. Default path. |
| `scripts/prepare-bundle.ps1` / `.sh` | Orchestrator the wrappers call. Pulls model weights, calls `package-airgapped`, optionally stages onto the USB. | You want flag control: `--include-70b`, `--models`, `--skip-pull`, `--destination`. |
| `scripts/package-airgapped.ps1` / `.sh` | Lower layer. Calls `build.*` to PyInstall the apps, copies the Ollama runtime, walks model manifests and copies just the referenced blobs. Does **not** pull models. | You've already pulled models, just need to (re)assemble the bundle. |

### Run on the air-gapped workstation (operator)

These ship inside the bundle and aren't invoked from a source checkout. Templates live in `scripts/templates/` for reference.

| Script | What it does |
|---|---|
| `Install-Suite.cmd` (Win) | Double-click shim that runs `install.ps1` with `-ExecutionPolicy Bypass` so no `Set-ExecutionPolicy` prompt is needed. |
| `install.ps1` / `install.sh` | Verifies the SHA-256 manifest, atomic-swaps the bundle into a permanent location (`%LOCALAPPDATA%\Programs\InscriptionSuite\` or `~/.local/share/InscriptionSuite/`), creates a Start Menu / `.desktop` entry. |
| `start-suite.ps1` / `start-suite.sh` | Daily launcher. Spawns the bundled Ollama on port 11435, asks which model to use this session, sets env vars, then lets the operator launch any of the three apps. |

---

## TL;DR

```bash
# Developer (Linux / macOS dev box)
python -m venv .venv && source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e suite_common -e inscription[dev] -e caseforge[dev] -e caseguide[dev]
python -m inscription   # or: caseforge / caseguide

# Build operator (offline workstation target, Linux build box)
./Build-Bundle.sh       # double-click in file manager also works

# Operator (offline workstation, after copying the USB bundle)
./install.sh            # then launch via the desktop app menu
```
