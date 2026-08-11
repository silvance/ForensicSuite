#Requires -Version 5.1
<#
.SYNOPSIS
    Stage a LITE InscriptionSuite-Airgapped folder: apps only, no models.

.DESCRIPTION
    CI-friendly staging for GitHub releases (asset cap: 2 GB). Takes
    the three PyInstaller app bundles that build.ps1 produced, adds
    the installer/launcher templates, and stamps version.json +
    manifest.json exactly like prepare-bundle.ps1 does -- minus the
    Ollama runtime and model blobs, which don't fit a release asset
    and are optional to the suite anyway. install.ps1 and
    start-suite.ps1 detect the lite layout and degrade gracefully
    (AI features off until configured in-app).

.PARAMETER Destination
    Directory to create InscriptionSuite-Airgapped\ inside.

.PARAMETER Tag
    Release tag stamped into version.json (e.g. v0.2.0).
#>
param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$Tag = ""
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
$BundleSrc = Join-Path $Destination "InscriptionSuite-Airgapped"

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$apps = @(
    @{ Name = "Inscription"; Dist = "inscription\dist\Inscription" },
    @{ Name = "CaseForge";   Dist = "caseforge\dist\CaseForge" },
    @{ Name = "CaseGuide";   Dist = "caseguide\dist\CaseGuide" }
)
foreach ($app in $apps) {
    $src = Join-Path $RepoRoot $app.Dist
    if (-not (Test-Path (Join-Path $src "$($app.Name).exe"))) {
        throw "Missing built app at $src -- run build.ps1 first."
    }
}

Write-Step "Staging lite bundle at $BundleSrc"
if (Test-Path $BundleSrc) { Remove-Item -Recurse -Force $BundleSrc }
New-Item -ItemType Directory -Path $BundleSrc | Out-Null

foreach ($app in $apps) {
    Copy-Item -Recurse (Join-Path $RepoRoot $app.Dist) (Join-Path $BundleSrc $app.Name)
}
$templates = Join-Path $PSScriptRoot "templates"
foreach ($t in @("Install-Suite.cmd", "install.ps1", "start-suite.ps1", "airgapped-README.txt")) {
    Copy-Item (Join-Path $templates $t) (Join-Path $BundleSrc $t)
}

Write-Step "Stamping version.json + manifest.json"
$gitSha = ""
try { $gitSha = (& git -C $RepoRoot rev-parse HEAD 2>$null).Trim() } catch {}
if (-not $gitSha) { $gitSha = "unknown" }
$version = [ordered]@{
    bundle_format_version = 1
    variant               = "lite"
    release_tag           = $Tag
    build_timestamp       = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    git_sha               = $gitSha
    models                = @()
}
$version | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath (Join-Path $BundleSrc "version.json") -Encoding utf8

# manifest.json MUST match the schema prepare-bundle.ps1 writes --
# install.ps1 parses ``$manifest.files`` and treats every on-disk file
# missing from that map as "unexpected", so a flat {path: hash} dict
# fails verification for the entire bundle.
$manifestPath = Join-Path $BundleSrc "manifest.json"
$entries = [ordered]@{}
$bundleRootLength = $BundleSrc.TrimEnd('\').Length + 1
Get-ChildItem -LiteralPath $BundleSrc -Recurse -File |
    Where-Object { $_.FullName -ne $manifestPath } |
    Sort-Object FullName |
    ForEach-Object {
        $rel = $_.FullName.Substring($bundleRootLength).Replace('\', '/')
        $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLower()
        $entries[$rel] = "sha256:$hash"
    }
$manifestPayload = [ordered]@{
    manifest_version = 1
    git_sha          = $gitSha
    created_at       = $version.build_timestamp
    files            = $entries
}
$manifestPayload | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath $manifestPath -Encoding utf8

Write-Host "Lite bundle staged: $BundleSrc" -ForegroundColor Green
