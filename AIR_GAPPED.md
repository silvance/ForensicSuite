# Air-gapped deployment

This page is for the operator who **builds** the air-gapped bundle on a
connected Windows machine and then **carries** it to the offline
workstation. The bundle is fully self-contained: three apps, the Ollama
runtime, and the pre-pulled model weights.

For day-to-day developer setup, see `SETUP.md` instead.

---

## Single-file option: InscriptionSuite-Setup.exe

Prefer one file over a folder? Build with the switch:

```
.\Build-Bundle.bat -SingleExe
```

That packs the entire staged bundle into **one
`InscriptionSuite-Setup.exe`** next to the folder. On the air-gapped
workstation it is both installer and launcher:

- **First double-click:** extracts, runs the bundle's own
  `Install-Suite.cmd` (same install path, Start Menu shortcut, and
  behaviour as the folder flow), cleans up its cache, and starts the
  suite.
- **Every later double-click:** detects the installed version and
  starts the suite directly. Keep the exe on the desktop as the
  launcher, or use the Start Menu shortcut -- both work.
- **A newer exe** (different bundle version stamp) reinstalls over
  the old version automatically.

Constraints worth knowing:

- The exe exceeds 4 GB with models included -- **FAT32 USB sticks
  can't hold it**; format the stick exFAT or NTFS.
- Windows SmartScreen flags large unsigned executables on first run
  ("More info" → "Run anyway"). Sign the exe with your
  organisation's code-signing certificate to remove that friction.
- Disk peak during first install is ~2× the bundle size (extraction
  cache + installed copy); the cache is removed automatically.

## What gets shipped

```
InscriptionSuite-Airgapped\
├── Inscription\          ~150 MB    capture + step generation
├── CaseForge\            ~150 MB    case intake + reporting
├── CaseGuide\            ~150 MB    LLM coach
├── ollama\               ~150 MB    bundled Ollama Windows runtime
├── models\               ~14 GB     two model snapshots
│   ├── blobs\            (sha256-* weight files)
│   └── manifests\        (one per model:tag pulled)
├── Install-Suite.cmd                double-clickable shim around
│                                    install.ps1 (no right-click, no
│                                    Set-ExecutionPolicy needed)
├── install.ps1                      one-shot installer (run on first
│                                    setup -- copies the bundle to a
│                                    permanent location and creates a
│                                    Start Menu shortcut)
├── start-suite.ps1                  daily launcher (called by the
│                                    Start Menu shortcut after install,
│                                    asks which model to use this session)
├── version.json                     build provenance (git SHA, build
│                                    timestamp, bundled model list)
├── manifest.json                    SHA-256 of every file in the
│                                    bundle -- install.ps1 verifies
│                                    this before copying onto the
│                                    workstation
└── README.txt                       operator notes
```

Total: **~15 GB**. Use a 32 GB USB drive or larger, formatted **exFAT or NTFS** (Linux: exFAT or ext4). FAT32 is the default Windows format on smaller USB drives but its 4 GB single-file limit blocks the model blobs (qwen 7B is ~5.4 GB, qwen 14B is ~9 GB); the build script detects this up front and refuses with a reformat hint.

---

## Build-machine prerequisites

A Windows 10/11 box with internet access during the build only.

1. Clone this repo and follow `SETUP.md` to get the venv working with
   all four packages installed editable.
2. Install Ollama for Windows from <https://ollama.com/download/windows>.
3. From any PowerShell window:
   ```powershell
   ollama pull gemma4:latest
   ollama pull granite4:tiny-h
   ```
   `gemma4:latest` is the shared default both Inscription and CaseGuide
   point at (~10 GB). `granite4:tiny-h` ships as a smaller (~4 GB)
   fallback the operator can switch to via `start-suite.ps1` on
   workstations that can't fit gemma4 in memory.

---

## Building the bundle

### Double-click (simplest)

`Build-Bundle.bat` (Windows) and `Build-Bundle.sh` (Linux) at the repo root are double-clickable wrappers around the bundle pipeline. They:

1. Set up the Python venv on first run (creates `.venv`, installs the four packages editable with `[dev]` extras).
2. Verify Ollama is installed; surface a download link if not.
3. Pop a graphical folder picker for the USB drive (Windows Forms on Windows, `zenity` on Linux; falls back to a terminal prompt if `zenity` isn't installed).
4. Pull the default models, build the apps, write the bundle straight onto the USB.
5. Show a "bundle ready at <path>" dialog.

If you don't want the picker, pass a destination directly: `Build-Bundle.bat -Destination E:\` (or `./Build-Bundle.sh --destination /media/usb`).

### One-shot wrapper (the underlying script)

`scripts\prepare-bundle.ps1` is the actual pipeline; the `.bat`/`.sh` above just calls it with sensible defaults. Use it directly when you need flag control. Pulls the chosen models, runs `package-airgapped.ps1`, optionally fetches the latest PowerShell 7 MSI alongside it, and (with `-Destination`) copies the finished bundle to an external drive in one go. Typical use from the repo root with the venv activated:

```powershell
.venv\Scripts\Activate.ps1
.\scripts\prepare-bundle.ps1 -Destination E:\
```

That stages everything into `E:\InscriptionSuite-Airgapped\` ready to carry. Useful flags:

```powershell
# Add the 70B fallback for the operator who's willing to wait on heavier rewrites.
.\scripts\prepare-bundle.ps1 -Destination F:\ -Include70B

# Override the model set entirely.
.\scripts\prepare-bundle.ps1 -Models gemma4:latest,granite4:tiny-h -Destination E:\

# Drop a PowerShell 7 MSI in the bundle for offline install on the workstation.
.\scripts\prepare-bundle.ps1 -Destination E:\ -IncludePowerShell7

# Already pulled the models and just want to rebuild + restage onto a new drive.
.\scripts\prepare-bundle.ps1 -SkipPull -Destination E:\
```

Defaults target an RTX 3070 (8 GB VRAM) plus a Xeon / 128 GB RAM workstation: a fully GPU-resident `qwen2.5:7b-instruct-q5_K_M` paired with a partial-offload `qwen2.5:14b-instruct-q4_K_M`. Total bundle is ~15 GB; with `-Include70B` it grows to ~57 GB (plan for a 64 GB+ drive).

When `-Destination` is set with a fresh build (no `-SkipBuild`), the bundle is staged directly at the destination — no second copy. That keeps the build-drive disk requirement to roughly the size of the repo + venv + PyInstaller intermediates (~2 GB) rather than ~30 GB. Used with `-SkipBuild`, the script keeps the original semantics: assume the bundle is already in `dist\` and copy it to the destination.

### Manual flow

If you want finer control, the underlying script is `scripts\package-airgapped.ps1`:

```powershell
.venv\Scripts\Activate.ps1
.\scripts\package-airgapped.ps1
```

The script will:

1. Run `build.ps1` to produce the three PyInstaller one-folder bundles.
2. Copy the bundled Ollama runtime into the staging folder.
3. Read each requested model's manifest, follow it to the referenced
   blobs, and copy *only those* into `models\` -- your other locally
   pulled models stay out of the bundle, keeping size predictable.
4. Drop in `start-suite.ps1` and `README.txt`.
5. Print the bundle path and total size when done.

Output lands at:

```
dist\InscriptionSuite-Airgapped\
```

### Useful flags

```powershell
# Already built? Skip the long PyInstaller pass.
.\scripts\package-airgapped.ps1 -SkipBuild

# Different model set (only when you've changed the apps' DEFAULT_LLM_MODEL).
.\scripts\package-airgapped.ps1 -Models gemma4:latest,granite4:tiny-h

# Stage straight to the USB drive instead of dist\ -- avoids needing
# ~15 GB free on the build drive.
.\scripts\package-airgapped.ps1 -OutputRoot E:\

# Non-standard Ollama install.
.\scripts\package-airgapped.ps1 `
    -OllamaRoot "D:\Tools\Ollama" `
    -OllamaModelsRoot "D:\Tools\Ollama\.ollama\models"
```

---

## Transferring to the air-gapped workstation

1. Plug the USB drive into the offline workstation.
2. From inside `<USB>\InscriptionSuite-Airgapped\`, **double-click** `Install-Suite.cmd`. (One-line shim that runs `install.ps1` with the right `-ExecutionPolicy Bypass`, so no right-click and no `Set-ExecutionPolicy` needed on a fresh workstation.)
3. The installer copies the bundle to `%LOCALAPPDATA%\Programs\InscriptionSuite\` and creates a Start Menu shortcut under `InscriptionSuite \ Inscription Suite`.
4. From here on, launch via Start Menu → **Inscription Suite**. (The shortcut runs the installed copy of `start-suite.ps1`, which self-elevates for UIA visibility into elevated forensic tools.)

If you'd rather drive `install.ps1` manually, right-click it → **Run with PowerShell**. If the workstation refuses to run the script with `UnauthorizedAccess`, run this once as the local user:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

This is per-user, doesn't need administrator, and only affects this account.

### Installer flags

```powershell
# Default: per-user install at %LOCALAPPDATA%\Programs\InscriptionSuite,
# Start Menu shortcut, no admin needed.
.\install.ps1

# Add a Desktop shortcut alongside the Start Menu one.
.\install.ps1 -DesktopShortcut

# Replace an existing install without prompting.
.\install.ps1 -Force

# System-wide install (right-click -> Run as administrator first).
.\install.ps1 -InstallRoot "C:\Program Files\InscriptionSuite"

# If the bundle was built with -IncludePowerShell7, also install PS 7.
.\install.ps1 -InstallPowerShell7
```

Re-running `install.ps1` only replaces the binaries. User configuration
(`%LOCALAPPDATA%\Inscription\`, `%LOCALAPPDATA%\CaseForge\`,
`%LOCALAPPDATA%\CaseGuide\`) and saved cases are kept intact.

---

## How the runtime stitches together

The launcher sets two environment variables before starting Ollama:

```powershell
$env:OLLAMA_MODELS      = "<bundle>\models"
$env:OLLAMA_HOST        = "127.0.0.1:11435"
$env:SUITE_LLM_BASE_URL = "http://127.0.0.1:11435/v1"
```

The launcher uses port **11435** rather than the Ollama default of
11434 so the bundled instance never collides with or silently reuses
a system-wide Ollama install on the same workstation -- in a forensic
context, "did this rewrite come from our bundled model store?" needs
to be unambiguous.

Then it spawns `<bundle>\ollama\ollama.exe serve` in the background and
polls `http://127.0.0.1:11435/api/tags` until it answers 200. The apps
read `SUITE_LLM_BASE_URL` from the launcher's environment, so they
target the bundled instance without any per-user config change. A
user who's pointed Settings at a different endpoint keeps that choice
(env var only fills the unset default).

When more than one model is bundled, the launcher walks
`models\manifests\registry.ollama.ai\library\` to enumerate them, asks
the operator which to use this session, and exports
`SUITE_LLM_MODEL=<chosen>` before launching any app. Inscription and
CaseGuide both honour that variable as their default — the user can
still override per-app via Settings, where the model field is now an
editable dropdown populated from `/v1/models`.

Closing the launcher window stops the spawned Ollama process. The apps
continue to run if launched, but their AI features will fail until you
re-run `start-suite.ps1`.

---

## Linux air-gapped deployment

The same bundle layout, model-blob deduplication, integrity manifest, and atomic-swap install pattern are available for Linux workstations. Two caveats up front:

- **Inscription on Linux ships in degraded form.** Case management, step rewriting, and exports work; **automated UIA capture does not** (`pywinauto` is Windows-only and there's no Linux equivalent for the Windows UI Automation API). CaseForge and CaseGuide are fully functional.
- **Glibc x86_64 only.** Tested on Ubuntu 22.04+ / Debian 12+ / Fedora / RHEL 8+. Skip musl distros (Alpine).

### Build-machine prerequisites

- Linux x86_64 with internet access during the build only.
- Clone this repo and follow `SETUP.md` to get the venv working with all four packages installed editable.
- Ollama installed locally with the desired models pulled (`ollama pull gemma4:latest` etc.). The Linux install script puts Ollama at `/usr/local/bin/ollama` with models at `~/.ollama/models/`.
- Download `ollama-linux-amd64.tgz` from <https://github.com/ollama/ollama/releases> and extract it to a directory; pass that directory as `--ollama-bundle` to the build script. The bundle layout requires the standard `bin/ollama` + `lib/ollama/` tree, which is what the tarball provides.

### Building the bundle

```bash
source .venv/bin/activate

# One-shot: pull models, build apps, assemble bundle, write manifest,
# stage to USB.
./scripts/prepare-bundle.sh \
    --ollama-bundle ~/Downloads/ollama-linux-amd64 \
    --destination /media/usb/

# Useful flags:
./scripts/prepare-bundle.sh --ollama-bundle <path> --include-70b
./scripts/prepare-bundle.sh --ollama-bundle <path> \
    --models gemma4:latest,granite4:tiny-h --destination /media/usb/
./scripts/prepare-bundle.sh --ollama-bundle <path> --skip-pull --destination /media/usb/
```

`--destination` with a fresh build stages directly at the USB (no intermediate copy), same disk-space win as the Windows pipeline. Output lands at `<destination>/InscriptionSuite-Airgapped-Linux/`.

### Transferring to the air-gapped Linux workstation

```bash
# From inside <USB>/InscriptionSuite-Airgapped-Linux/
./install.sh                              # default per-user
./install.sh --desktop-shortcut           # also drop a desktop file
./install.sh --install-root /opt/InscriptionSuite  # system-wide (run with sudo)
```

`install.sh` verifies the SHA-256 manifest before copying, atomic-swaps the new install in (so a copy failure mid-stream doesn't lose the working install), and writes a `.desktop` entry to `~/.local/share/applications/inscription-suite.desktop`. From then on, launch via the desktop environment's app menu.

User configuration / saved cases live under `~/.local/share/Inscription/`, `~/.local/share/CaseForge/`, `~/.local/share/CaseGuide/` (or wherever the operator chose to keep case folders); re-running `install.sh` only replaces the binaries.

### Runtime stitching (Linux)

The launcher sets the same env vars as the Windows version, plus `LD_LIBRARY_PATH` so the bundled Ollama runner libraries are found:

```bash
export OLLAMA_MODELS="<bundle>/models"
export OLLAMA_HOST="127.0.0.1:11435"
export SUITE_LLM_BASE_URL="http://127.0.0.1:11435/v1"
export LD_LIBRARY_PATH="<bundle>/ollama/lib/ollama:$LD_LIBRARY_PATH"
```

Port 11435 (not the Ollama default 11434) keeps the bundled instance from colliding with a system-wide Ollama install. The launcher spawns `<bundle>/ollama/bin/ollama serve`, polls `/api/tags` until 200, and presents the same model picker as the Windows version. Closing the picker (`Q` then Enter) terminates the spawned Ollama process via a bash `trap`.

---

## What we don't ship

- **Antivirus exemption.** Some AV products quarantine PyInstaller
  bundles by default. Have the workstation's AV admin allow-list the
  three `.exe` paths inside the bundle before first run.
- **Driver / runtime prerequisites for the host OS.** PyInstaller
  bundles include the Visual C++ runtime PySide6 needs; nothing else is
  required for the bundled software itself.
- **Model updates.** A new bundle is the upgrade path — the workstation
  has no path to pull a newer model on its own.
