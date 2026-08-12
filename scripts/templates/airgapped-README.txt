Inscription suite -- air-gapped bundle
=====================================

This folder is a self-contained install of three forensic tools plus the
local LLM runtime they use. It runs entirely offline; no internet
connection is needed at any point after copying the folder onto this
machine.

Contents
--------

  Inscription\        Capture a Windows workflow, generate a step guide.
  CaseForge\          Case intake, chain-of-custody, report generation.
  CaseGuide\          LLM-assisted exam coach (next-action suggestions).
  ollama\             Local LLM runtime (Ollama).
  models\             Pre-staged model weights and manifests.
  install.ps1         One-shot installer (verifies integrity, copies the
                      bundle, creates a Start Menu shortcut).
  start-suite.ps1     Daily launcher (the Start Menu shortcut runs this).
  version.json        Build provenance: git SHA, build timestamp, bundled
                      model list. Useful for "which build is this?" later.
  manifest.json       SHA-256 of every file. install.ps1 reads this and
                      verifies the bundle before installing -- a bad USB
                      transfer fails loudly instead of producing a silently
                      corrupt install.

To start
--------

  First time on this workstation:
    Double-click  Install-Suite.cmd  inside this folder.
    The installer copies the bundle to
        %LOCALAPPDATA%\Programs\InscriptionSuite\
    and creates a Start Menu shortcut under
        InscriptionSuite \ Inscription Suite.

    (Install-Suite.cmd is just a one-line shim that runs install.ps1
    with the right ExecutionPolicy bypass; if you'd rather drive the
    installer manually, right-click install.ps1 -> Run with PowerShell.)

  Daily use after that:
    Start Menu -> InscriptionSuite -> "Inscription Suite".

  Quick test before installing (optional):
    Right-click  start-suite.ps1  and pick  "Run with PowerShell".
    Runs the launcher straight from this folder without copying
    anything to the workstation. Useful when you just want to verify
    the bundle is intact.

The launcher will:
  1. Re-launch itself elevated (one UAC prompt) so the UI-automation
     resolver can read elevated forensic tools like AXIOM Examine.
  2. Point Ollama at the .\models directory inside this folder.
  3. Start a local Ollama server on 127.0.0.1:11435 (a dedicated port,
     so a system-wide Ollama install is never touched).
  4. Wait until the server reports ready.
  5. If more than one model is bundled, ask which one the apps should
     use this session.
  6. Show a small picker -- pick the app you want to open.

Closing the picker stops the bundled Ollama server. Re-run start-suite.ps1
any time to bring it back up; the model question is asked again so you
can switch without rebooting.

Models bundled
--------------

  gemma4:latest    Shared default for both Inscription's AI rewrite step
                   and CaseGuide's suggestion refinement (~10 GB).
  granite4:tiny-h  Smaller fallback (~4 GB) for workstations that can't
                   keep gemma4 resident in memory.

Both are cached under .\models. The launcher asks which to use; the
apps read the choice from the SUITE_LLM_MODEL env var. You can still
override per app from Settings -- the Model field is an editable
dropdown populated from whatever Ollama lists.

Lite bundles (GitHub release downloads) ship WITHOUT ollama\ and
models\ -- release assets cap at 2 GB. The apps all work; AI rewrite /
refine / suggestions stay off until enabled. On a machine with
internet, pick [A] Enable AI features from the launcher menu (or run
enable-ai.ps1) to download the official Ollama runtime plus a model
(~6.5 GB, one time) into the install. After that, AI runs fully
offline like the full bundle.

Troubleshooting
---------------

  "Ollama did not become ready within 60s"
      Another Ollama install on this machine may already be holding the
      port. Stop that one (Task Manager -> ollama.exe -> End task) and
      re-run start-suite.ps1.

  "Cannot run scripts because running scripts is disabled"
      Open PowerShell as Administrator on this machine and run:
          Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
      Then re-run start-suite.ps1.

  An app fails to launch
      Try running the .exe directly from inside the matching folder
      (e.g. .\Inscription\Inscription.exe). If that works but the
      launcher doesn't, the issue is with the launcher's environment;
      open a support ticket with the message text.
