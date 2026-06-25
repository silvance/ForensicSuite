# Suite setup — Windows (PowerShell)

> Building a portable bundle for an offline / air-gapped workstation?
> See **`AIR_GAPPED.md`** instead.

This repo contains three tools that share a tiny common package:

| Package | What it does |
|---|---|
| **suite_common** | Shared LLM client + JSON-tolerant coercion helpers (no UI; depended on by all three apps) |
| **Inscription** | Capture a Windows workflow; generate an annotated step guide |
| **CaseForge** | Case intake, chain-of-custody, and report generation |
| **CaseGuide** | LLM-assisted exam coach that suggests next actions |

---

## Quick start (one shared venv)

Two paths -- the bootstrap script if you want it done for you, or the
manual commands if you want to understand each step.

### Option A: bootstrap script (recommended)

Open the repo root in a PowerShell terminal and run:

```powershell
.\bootstrap.ps1
.venv\Scripts\Activate.ps1
```

That's it. The script checks Python 3.12+, creates `.venv`, installs
all four packages in the right order, and imports each one to confirm
the install worked. Re-run any time to refresh the editable installs.

POSIX equivalent (Linux / macOS / WSL):

```bash
./bootstrap.sh
source .venv/bin/activate
```

### Option B: manual

Open **InscriptionClaude.code-workspace** in VS Code, then run these
commands from the **repo root** in a PowerShell terminal:

```powershell
# 1. Create and activate the shared virtual environment
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. Upgrade pip, then install everything in editable mode.
#    suite_common comes first so it's on PYTHONPATH when the others
#    resolve their dependencies.
python -m pip install --upgrade pip
python -m pip install -e suite_common -e inscription[dev] -e caseforge[dev] -e caseguide[dev]

# 3. Verify all three entry points are reachable
python -m inscription --help
caseforge --help
caseguide --help
```

> **Why `.venv\Scripts\Activate.ps1` and not `source`?**
> `source` is a bash built-in; it does not exist in PowerShell.
> Always use the `.ps1` form on Windows.

---

## Running each tool

```powershell
# Must activate the venv first in every new terminal session
.venv\Scripts\Activate.ps1

python -m inscription          # Inscription capture studio
caseforge                      # CaseForge case manager
caseguide                      # CaseGuide exam coach
```

VS Code picks up the venv automatically once you open the workspace —
the Python extension reads `.venv\` from the workspace root.

---

## Running the test suite

```powershell
.venv\Scripts\Activate.ps1

# Each suite — pytest test discovery doesn't span sibling packages, so
# run them independently:
pushd suite_common; pytest tests -v; popd
pushd inscription; pytest tests -v; popd
pushd caseforge; pytest tests -v; popd
pushd caseguide; pytest tests -v; popd
```

---

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| `source: command not found` | Running bash syntax in PowerShell | Use `.venv\Scripts\Activate.ps1` |
| `caseforge` not recognized | venv not activated, or installed to system Python | Activate first, then `pip install -e caseforge` |
| Packages install to `AppData\Local\…` | venv not active when running pip | Check prompt shows `(.venv)`, then reinstall |
| `ModuleNotFoundError: inscription` | Installed to wrong env | `pip show inscription` — confirm `Location` is inside `.venv` |

---

## LLM setup (Inscription AI rewrite)

The step generator always runs without AI. The AI rewrite (File → Rewrite with AI)
is optional and needs a running OpenAI-compatible endpoint. Fastest path:

```powershell
# Install Ollama, then:
ollama pull granite3.3:8b
```

Ollama exposes `http://localhost:11434/v1` — that is Inscription's default.
Change model/endpoint in `%LOCALAPPDATA%\Inscription\config.ini`.
