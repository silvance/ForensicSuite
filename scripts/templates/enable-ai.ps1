#Requires -Version 5.1
<#
.SYNOPSIS
    Add local AI (the Ollama runtime + a model) to a lite install.

.DESCRIPTION
    The GitHub-release ("lite") bundle ships the three apps with no
    AI engine -- release assets cap at 2 GB and a single model blob
    is bigger than that. This script closes the gap ON A MACHINE WITH
    INTERNET by downloading, into the install folder it lives in:

        1. The official Ollama Windows runtime (latest release zip
           from github.com/ollama/ollama) into  .\ollama\
        2. Model weights via 'ollama pull' into  .\models\

    which is exactly the layout the full USB bundle ships and the
    layout start-suite.ps1 already looks for. After it finishes,
    re-run the launcher and AI rewrite / refine / suggestions work
    offline from then on -- no further internet needed.

    Everything is fetched over HTTPS from official sources (GitHub
    for the runtime, registry.ollama.ai for weights -- Ollama itself
    digest-verifies every blob it pulls). Expect ~1 GB for the
    runtime plus ~5.4 GB for the default model.

    Run it from the Start Menu launcher's menu ([A] Enable AI
    features) or directly:  right-click -> Run with PowerShell.

.PARAMETER Models
    Model tags to pull. Default matches the smaller of the two models
    the full USB bundle ships, sized for 8 GB-VRAM workstations.
    Re-run with -Models to add more later, e.g.:
        .\enable-ai.ps1 -Models qwen2.5:14b-instruct-q4_K_M

.PARAMETER Port
    Port for the temporary pull server. Matches start-suite.ps1's
    dedicated port so a running launcher session can be reused.
#>
param(
    [string[]]$Models = @("qwen2.5:7b-instruct-q5_K_M"),
    [int]$Port = 11435
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot

# Old .NET defaults to TLS 1.0, which GitHub no longer accepts.
[Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

$ollamaDir = Join-Path $Root "ollama"
$ollamaExe = Join-Path $ollamaDir "ollama.exe"
$modelsDir = Join-Path $Root "models"

# 1. Download the Ollama runtime --------------------------------------------
# Same trust model as the PowerShell 7 MSI fetch in prepare-bundle.ps1:
# HTTPS to the official GitHub release of the project. The zip carries
# ollama.exe + its lib\ directory at the archive root, so extracting
# into .\ollama\ reproduces the full bundle's layout exactly.

if (Test-Path $ollamaExe) {
    Write-Step "Ollama runtime already present at $ollamaDir -- skipping download"
} else {
    Write-Step "Downloading the Ollama runtime (official Windows release, ~1 GB)"
    # Invoke-WebRequest's progress bar on PS 5.1 slows large downloads
    # by an order of magnitude; suppress it for the fetch.
    $previousProgress = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    $zip = Join-Path $env:TEMP "ollama-windows-amd64.zip"
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/ollama/ollama/releases/latest" -UseBasicParsing
        $asset = $release.assets | Where-Object { $_.name -eq "ollama-windows-amd64.zip" } | Select-Object -First 1
        if (-not $asset) {
            throw "Could not find ollama-windows-amd64.zip in the latest Ollama release. Install Ollama manually from https://ollama.com/download/windows instead."
        }
        $sizeMB = [math]::Round($asset.size / 1MB, 0)
        Write-Host "  $($release.tag_name) ($sizeMB MB) from github.com/ollama/ollama"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
        Write-Step "Extracting runtime to $ollamaDir"
        Expand-Archive -Path $zip -DestinationPath $ollamaDir -Force
    } finally {
        $ProgressPreference = $previousProgress
        if (Test-Path $zip) { Remove-Item $zip -ErrorAction SilentlyContinue }
    }
    if (-not (Test-Path $ollamaExe)) {
        throw "Extraction finished but $ollamaExe is missing -- the release layout may have changed. Install Ollama manually from https://ollama.com/download/windows."
    }
}

# 2. Pull model weights into the bundle's store -----------------------------
# OLLAMA_MODELS points the pull at .\models\ (the store the launcher
# serves from) instead of the user-wide ~\.ollama store; OLLAMA_HOST
# keeps both the temporary server and the pull client on the suite's
# dedicated port so a system-wide Ollama on 11434 is never touched.

$env:OLLAMA_MODELS = $modelsDir
$env:OLLAMA_HOST = "127.0.0.1:$Port"
if (-not (Test-Path $modelsDir)) {
    New-Item -ItemType Directory -Path $modelsDir | Out-Null
}

function Test-OllamaUp {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/tags" -TimeoutSec 1 -UseBasicParsing
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

$pullServer = $null
try {
    if (Test-OllamaUp) {
        Write-Step "Reusing the Ollama server already on port $Port"
    } else {
        Write-Step "Starting a temporary Ollama server for the pull"
        $pullServer = Start-Process -FilePath $ollamaExe -ArgumentList "serve" `
            -WindowStyle Hidden -PassThru
        $deadline = (Get-Date).AddSeconds(60)
        while ((Get-Date) -lt $deadline -and -not (Test-OllamaUp)) {
            Start-Sleep -Milliseconds 500
        }
        if (-not (Test-OllamaUp)) {
            throw "Ollama server did not become ready on port $Port within 60s."
        }
    }

    foreach ($m in $Models) {
        Write-Step "Pulling $m (Ollama verifies every blob's digest as it lands)"
        & $ollamaExe pull $m
        if ($LASTEXITCODE -ne 0) {
            throw "ollama pull $m failed (exit $LASTEXITCODE). Check the connection and re-run enable-ai.ps1 -- completed layers are kept, so it resumes."
        }
    }
} finally {
    if ($pullServer -and -not $pullServer.HasExited) {
        Stop-Process -Id $pullServer.Id -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "AI components installed." -ForegroundColor Green
Write-Host "  Runtime: $ollamaDir"
Write-Host "  Models:  $modelsDir  ($($Models -join ', '))"
Write-Host ""
Write-Host "Re-run the launcher (Start Menu -> InscriptionSuite) to use them."
Write-Host "Everything now runs offline -- no further internet needed."
