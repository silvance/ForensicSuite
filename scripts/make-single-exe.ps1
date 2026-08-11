#Requires -Version 5.1
<#
.SYNOPSIS
    Pack the staged air-gapped bundle into one InscriptionSuite-Setup.exe.

.DESCRIPTION
    Takes the InscriptionSuite-Airgapped\ folder that prepare-bundle.ps1
    staged, zips it (ZIP64 -- payloads exceed 4 GB), builds the tiny
    tkinter stub (scripts\setup_stub.py) with PyInstaller --onefile,
    and concatenates stub + zip into a single exe. ZIP's central
    directory sits at the end of the file, so Python's zipfile opens
    the exe directly -- the stub reads its own payload with no custom
    container format.

    Model blobs (Ollama layers, Whisper weights) are already
    compressed; they are STORED rather than deflated so packing a
    multi-GB bundle takes seconds, not tens of minutes.

    On the air-gapped target the exe is both installer and launcher:
    first run extracts + runs the bundle's own Install-Suite.cmd;
    every later run detects the installed version and starts the
    suite directly.

.PARAMETER StagedBundle
    Path to the staged InscriptionSuite-Airgapped folder.

.PARAMETER Output
    Path of the exe to write. Default: sibling of the staged folder,
    named InscriptionSuite-Setup.exe.

.NOTES
    FAT32 cannot hold files over 4 GB -- use an exFAT or NTFS USB
    stick for the single-exe artifact. Unsigned multi-GB exes will
    trip SmartScreen on first run ("More info" -> "Run anyway");
    sign the exe with your organisation's certificate if that
    friction matters.
#>
param(
    [Parameter(Mandatory = $true)][string]$StagedBundle,
    [string]$Output = "",
    # Also copy the intermediate payload zip here (the release
    # workflow publishes it as its own asset for the online installer
    # to download). Empty = discard with the work dir as before.
    [string]$KeepPayloadZip = ""
)

$ErrorActionPreference = "Stop"

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

$staged = Resolve-Path $StagedBundle
if (-not (Test-Path (Join-Path $staged "Install-Suite.cmd"))) {
    throw "$staged does not look like a staged bundle (no Install-Suite.cmd)."
}
if (-not $Output) {
    $Output = Join-Path (Split-Path -Parent $staged) "InscriptionSuite-Setup.exe"
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$work = Join-Path ([IO.Path]::GetTempPath()) "inscription-single-exe"
if (Test-Path $work) { Remove-Item -Recurse -Force $work }
New-Item -ItemType Directory -Path $work | Out-Null

# 1. Payload zip. python zipfile CLI-free: drive it with a here-script so
#    we control per-extension compression (store already-compressed model
#    blobs; deflate everything else) and force ZIP64.
Write-Step "Zipping staged bundle (store-level for model blobs)"
$zipPath = Join-Path $work "payload.zip"
$py = @"
import sys, zipfile
from pathlib import Path
staged = Path(sys.argv[1]); zip_path = Path(sys.argv[2])
STORE_SUFFIXES = {'.bin', '.gguf', '.pt', '.onnx', '.zip', '.7z', '.msi'}
root_name = staged.name
with zipfile.ZipFile(zip_path, 'w', allowZip64=True) as zf:
    for path in sorted(staged.rglob('*')):
        if path.is_dir():
            continue
        arcname = f"{root_name}/{path.relative_to(staged).as_posix()}"
        method = (zipfile.ZIP_STORED
                  if path.suffix.lower() in STORE_SUFFIXES or 'blobs' in path.parts
                  else zipfile.ZIP_DEFLATED)
        zf.write(path, arcname, compress_type=method)
print('payload ok')
"@
& python -c $py $staged $zipPath
if ($LASTEXITCODE -ne 0) { throw "Payload zip failed." }

# 2. Stub exe. suite_common must be importable (editable install in the
#    build venv); --hidden-import pins it for PyInstaller's analysis.
Write-Step "Building stub with PyInstaller"
& python -m PyInstaller --noconfirm --onefile --windowed `
    --name InscriptionSuite-Setup `
    --distpath (Join-Path $work "dist") `
    --workpath (Join-Path $work "build") `
    --specpath $work `
    --hidden-import suite_common.selfextract `
    (Join-Path $repoRoot "scripts\setup_stub.py")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller stub build failed." }
$stub = Join-Path $work "dist\InscriptionSuite-Setup.exe"

# 3. Concatenate stub + payload.
Write-Step "Concatenating stub + payload -> $Output"
$outParent = Split-Path -Parent $Output
if ($outParent) { New-Item -ItemType Directory -Force -Path $outParent | Out-Null }
$out = [IO.File]::Create($Output)
try {
    foreach ($part in @($stub, $zipPath)) {
        $in = [IO.File]::OpenRead($part)
        try { $in.CopyTo($out) } finally { $in.Dispose() }
    }
} finally { $out.Dispose() }

# 4. Verify the concatenated exe opens as a zip and carries the bundle.
Write-Step "Verifying payload readable from the final exe"
$verify = @"
import sys, zipfile
zf = zipfile.ZipFile(sys.argv[1])
names = zf.namelist()
assert any(n.endswith('Install-Suite.cmd') for n in names), 'installer missing'
print(f'verified: {len(names)} members')
"@
& python -c $verify $Output
if ($LASTEXITCODE -ne 0) { throw "Verification failed -- do not ship this exe." }

if ($KeepPayloadZip) {
    Write-Step "Keeping payload zip at $KeepPayloadZip"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $KeepPayloadZip) | Out-Null
    Copy-Item $zipPath $KeepPayloadZip -Force
}

$sizeGB = [math]::Round((Get-Item $Output).Length / 1GB, 2)
Write-Host ""
Write-Host "Single-file bundle ready: $Output ($sizeGB GB)" -ForegroundColor Green
Write-Host "Reminder: FAT32 sticks cannot hold files over 4 GB (use exFAT/NTFS)."
