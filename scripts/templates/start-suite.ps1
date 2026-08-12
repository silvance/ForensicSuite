#Requires -Version 5.1
<#
.SYNOPSIS
    First-run launcher for the air-gapped Inscription suite bundle.

.DESCRIPTION
    Lives at the root of the InscriptionSuite-Airgapped folder. It:
        1. Re-launches itself elevated if the operator started it from a
           non-admin shell, so the spawned Ollama and the apps inherit
           the higher integrity level.
        2. Points Ollama at the bundled models directory.
        3. Starts the bundled Ollama server on a DEDICATED port
           (127.0.0.1:11435 -- not the Ollama default 11434) so it
           never collides with or silently reuses a system-wide Ollama
           install on this machine.
        4. Exports SUITE_LLM_BASE_URL so Inscription / CaseGuide
           connect to our bundled instance, not the default.
        5. Waits until /api/tags answers 200 on the dedicated port.
        6. If more than one model is bundled, asks which one the apps
           should use this session and exports SUITE_LLM_MODEL.
        7. Opens a small picker so the operator can launch
           Inscription, CaseForge, or CaseGuide.

    Quitting the picker stops the Ollama server. Re-run this script to
    bring everything back up -- the model question is asked once per run
    so the operator can switch without a reboot.

    Why elevate? Inscription's UIA resolver can't read the accessibility
    tree of a more-privileged process (Windows blocks lower-IL inspection
    of higher-IL processes by design). Many forensic tools -- AXIOM
    Examine in particular -- run elevated, so an unelevated Inscription
    sees only "Click in <window>" placeholders for every click inside
    them. Re-launching as admin keeps the resolver effective.
#>

# ------------------------------------------------- self-elevate
# UAC prompt fires once at session start; the spawned copy runs the
# rest of this file with administrator rights. Skip the re-launch when
# we're already elevated, otherwise we'd loop forever.

$currentIdentity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
if (-not $currentPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "Re-launching as administrator (so UIA can see elevated apps)..." -ForegroundColor Yellow
    try {
        Start-Process -FilePath "powershell.exe" `
            -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"" `
            -WorkingDirectory $PSScriptRoot `
            -Verb RunAs
    } catch {
        Write-Error "Elevation declined or unavailable. Re-run start-suite.ps1 from an admin shell, or right-click -> Run as administrator."
        exit 1
    }
    exit 0
}

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

# --------------------------------------------------------------- environment

# Bundled Ollama runs on a dedicated, non-default port so we never
# accidentally reuse a system-wide Ollama install (which has its own
# model store and could silently produce different output -- bad for
# reproducibility in a forensic context). 11435 is one above the
# Ollama default; pick something else with -BundledOllamaPort if you
# need to.
$BundledOllamaPort = 11435
$BundledOllamaHost = "127.0.0.1:$BundledOllamaPort"

$env:OLLAMA_MODELS = Join-Path $Root "models"
$env:OLLAMA_HOST   = $BundledOllamaHost
# OLLAMA_KEEP_ALIVE keeps the loaded weights resident in RAM between
# requests so the first AI Rewrite click pays the model-load cost only
# once per session.
$env:OLLAMA_KEEP_ALIVE = "10m"
# Tell the suite apps where our Ollama lives. Inscription and CaseGuide
# both read SUITE_LLM_BASE_URL when their per-user QSettings hasn't
# overridden it (same pattern as SUITE_LLM_MODEL).
$env:SUITE_LLM_BASE_URL = "http://${BundledOllamaHost}/v1"

$ollamaExe = Join-Path $Root "ollama\ollama.exe"
$HaveOllama = Test-Path $ollamaExe
$enableAiScript = Join-Path $Root "enable-ai.ps1"
if (-not $HaveOllama) {
    # Lite bundle: no bundled AI engine. The apps run fully without it
    # (capture, notes, exports, reports); AI rewrite / refine offer a
    # configuration hint when used. Clear the env override so the apps
    # do not point at a server that will never exist.
    Write-Host "No bundled Ollama (lite install) -- AI features disabled unless configured in-app." -ForegroundColor Yellow
    if (Test-Path $enableAiScript) {
        Write-Host "On a machine with internet, pick [A] from the menu below to download them (~6.5 GB, one time)." -ForegroundColor Yellow
    }
    Remove-Item Env:SUITE_LLM_BASE_URL -ErrorAction SilentlyContinue
}

# Already serving on OUR dedicated port? That should only happen if a
# previous start-suite.ps1 in this session is still running, in which
# case reusing it is fine. Probe the dedicated port (11435), not the
# Ollama default (11434), so a system-wide Ollama install on the same
# box doesn't get confused with ours.
function Test-OllamaUp {
    try {
        $r = Invoke-WebRequest -Uri "http://${BundledOllamaHost}/api/tags" -TimeoutSec 1 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

$ourProcess = $null
if (-not $HaveOllama) {
    # Skip server startup + model pick entirely.
} elseif (Test-OllamaUp) {
    Write-Host "Ollama already responding on $BundledOllamaHost (our dedicated port) -- reusing." -ForegroundColor Yellow
} else {
    Write-Host "Starting bundled Ollama server..." -ForegroundColor Cyan
    $ourProcess = Start-Process -FilePath $ollamaExe `
        -ArgumentList "serve" `
        -WindowStyle Hidden `
        -PassThru

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-OllamaUp) { break }
    }
    if (-not (Test-OllamaUp)) {
        Write-Error "Bundled Ollama did not become ready on $BundledOllamaHost within 60s. Check that ollama.exe runs on this machine, and that nothing else is bound to port $BundledOllamaPort."
        if ($ourProcess) { Stop-Process -Id $ourProcess.Id -Force -ErrorAction SilentlyContinue }
        exit 1
    }
    Write-Host "Bundled Ollama ready on $BundledOllamaHost." -ForegroundColor Green
}

# ------------------------------------------------------- pick a model
# Walk the bundled manifest tree to find every model:tag the bundle
# ships. The launcher exports SUITE_LLM_MODEL before launching the
# apps so both Inscription and CaseGuide pick up the operator's choice.

function Get-BundledModels {
    $libRoot = Join-Path $Root "models\manifests\registry.ollama.ai\library"
    if (-not (Test-Path $libRoot)) { return @() }
    $found = @()
    foreach ($nameDir in Get-ChildItem -Directory $libRoot -ErrorAction SilentlyContinue) {
        foreach ($tagFile in Get-ChildItem -File $nameDir.FullName -ErrorAction SilentlyContinue) {
            $found += "$($nameDir.Name):$($tagFile.Name)"
        }
    }
    return $found | Sort-Object
}

$bundledModels = if ($HaveOllama) { Get-BundledModels } else { @() }
if ($bundledModels.Count -eq 0) {
    Write-Host "No bundled models found under .\models -- the apps will fall back to their built-in default." -ForegroundColor Yellow
} elseif ($bundledModels.Count -eq 1) {
    $env:SUITE_LLM_MODEL = $bundledModels[0]
    Write-Host "Using bundled model: $($bundledModels[0])" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "Bundled models" -ForegroundColor Cyan
    Write-Host "==============" -ForegroundColor Cyan
    for ($i = 0; $i -lt $bundledModels.Count; $i++) {
        $marker = if ($i -eq 0) { " (default)" } else { "" }
        Write-Host ("  [{0}] {1}{2}" -f ($i + 1), $bundledModels[$i], $marker)
    }
    Write-Host ""
    $pick = Read-Host "Pick a model (Enter for default)"
    $chosen = $bundledModels[0]
    if ($pick) {
        $idx = 0
        if ([int]::TryParse($pick, [ref]$idx) -and $idx -ge 1 -and $idx -le $bundledModels.Count) {
            $chosen = $bundledModels[$idx - 1]
        } else {
            Write-Host "Unknown selection -- falling back to default." -ForegroundColor Yellow
        }
    }
    $env:SUITE_LLM_MODEL = $chosen
    Write-Host "Using bundled model: $chosen" -ForegroundColor Green
}

# ----------------------------------------------------------------- the menu

$apps = @(
    @{ Key = "1"; Label = "Inscription (capture a workflow)";    Exe = "Inscription\Inscription.exe" }
    @{ Key = "2"; Label = "CaseForge (case intake / report)";    Exe = "CaseForge\CaseForge.exe"     }
    @{ Key = "3"; Label = "CaseGuide (suggestion coach)";        Exe = "CaseGuide\CaseGuide.exe"     }
)

# Offer AI enablement only when it is both absent and installable
# (the enable-ai.ps1 helper ships with lite bundles).
$offerEnableAi = (-not $HaveOllama) -and (Test-Path $enableAiScript)

try {
    while ($true) {
        Write-Host ""
        Write-Host "Inscription suite -- air-gapped" -ForegroundColor Cyan
        Write-Host "==============================" -ForegroundColor Cyan
        foreach ($a in $apps) {
            Write-Host ("  [{0}] {1}" -f $a.Key, $a.Label)
        }
        if ($offerEnableAi) {
            Write-Host "  [A] Enable AI features (downloads Ollama + a model -- needs internet)"
        }
        Write-Host "  [Q] Quit (also stops the bundled Ollama server)"
        Write-Host ""
        $choice = Read-Host "Pick"

        if ($choice -ieq "q") { break }

        if ($offerEnableAi -and $choice -ieq "a") {
            try {
                & $enableAiScript
            } catch {
                Write-Host "Enable AI failed: $_" -ForegroundColor Red
                Write-Host "The apps still work without AI; pick [A] to retry once the issue is fixed." -ForegroundColor Yellow
                continue
            }
            # The server/model environment is set up at launcher start,
            # so hand off to a fresh copy of ourselves rather than
            # duplicating that logic here.
            Write-Host "Restarting the launcher to pick up the new AI components..." -ForegroundColor Green
            Start-Process -FilePath "powershell.exe" `
                -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"" `
                -WorkingDirectory $Root
            exit 0
        }

        $picked = $apps | Where-Object { $_.Key -eq $choice }
        if (-not $picked) {
            Write-Host "Unknown selection: $choice" -ForegroundColor Yellow
            continue
        }
        $exe = Join-Path $Root $picked.Exe
        if (-not (Test-Path $exe)) {
            Write-Host "Missing $exe -- bundle is incomplete." -ForegroundColor Red
            continue
        }
        # SUITE_LLM_MODEL is already in the script's environment; Start-Process
        # inherits it on Windows so each launched .exe sees the operator's choice.
        Start-Process -FilePath $exe
    }
} finally {
    if ($ourProcess -and -not $ourProcess.HasExited) {
        Write-Host "Stopping bundled Ollama server..." -ForegroundColor Cyan
        Stop-Process -Id $ourProcess.Id -Force -ErrorAction SilentlyContinue
    }
}
