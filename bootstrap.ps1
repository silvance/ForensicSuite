# Bootstrap a development environment for the Inscription suite.
#
# What this does, in order:
#   1. Verify Python 3.12+ is available
#   2. Create .venv at the repo root (skipped if it already exists)
#   3. Upgrade pip inside the venv
#   4. Install all four packages in editable mode in the correct
#      order -- suite_common first so the other three's editable
#      installs resolve their cross-package dependencies cleanly
#   5. Sanity-check each app's entry point reaches the import layer
#
# Idempotent: re-running just refreshes the editable installs.
#
# Usage:
#   .\bootstrap.ps1                 # default: creates .venv at repo root
#   .\bootstrap.ps1 -VenvPath C:\v  # custom venv location
#
# After this finishes, activate the venv:
#   .venv\Scripts\Activate.ps1
#
# See SETUP.md for the full per-platform setup walkthrough this
# script automates the Windows half of.

[CmdletBinding()]
param(
    [string]$VenvPath = ''
)

$ErrorActionPreference = 'Stop'

# --------------------------------------------------------------- config

$MinPythonMajor = 3
$MinPythonMinor = 12
$RepoRoot       = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $VenvPath) { $VenvPath = Join-Path $RepoRoot '.venv' }

# Install order matters: suite_common is a dep of the other three, so
# its editable install must be visible on sys.path before the others
# resolve their pyproject.toml dependencies. The [dev] extras pull
# in pytest / ruff / etc. for the three apps.
$Packages = @(
    (Join-Path $RepoRoot 'suite_common'),
    (Join-Path $RepoRoot 'inscription') + '[dev]',
    (Join-Path $RepoRoot 'caseforge') + '[dev]',
    (Join-Path $RepoRoot 'caseguide') + '[dev]'
)

# --------------------------------------------------------------- helpers

function Note  { param($Msg) Write-Host "==> $Msg" -ForegroundColor Cyan }
function Warn  { param($Msg) Write-Host "!!! $Msg" -ForegroundColor Yellow }
function Fail  { param($Msg) Write-Host "!!! $Msg" -ForegroundColor Red; exit 1 }

# --------------------------------------------------------------- 1. Python version check

function Get-PythonExecutable {
    # py launcher (Windows-specific) understands version pinning.
    # Prefer it over a bare ``python`` so a system Python 2 or an
    # older Python 3 from PATH doesn't get picked.
    foreach ($candidate in @('py -3.12', 'py -3.13', 'py -3', 'python', 'python3')) {
        $cmd = $candidate.Split(' ')[0]
        $args = if ($candidate.Contains(' ')) { $candidate.Substring($cmd.Length + 1) } else { '' }
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            try {
                $checkCommand = if ($args) { "$cmd $args -c `"import sys; print(sys.version_info.major, sys.version_info.minor)`"" }
                                else       { "$cmd -c `"import sys; print(sys.version_info.major, sys.version_info.minor)`"" }
                $version = (Invoke-Expression $checkCommand) -split ' '
                $major = [int]$version[0]
                $minor = [int]$version[1]
                if ($major -gt $MinPythonMajor -or ($major -eq $MinPythonMajor -and $minor -ge $MinPythonMinor)) {
                    return $candidate
                }
            } catch {
                # Probe failed -- move on to the next candidate.
                continue
            }
        }
    }
    return $null
}

Note "Looking for Python $MinPythonMajor.$MinPythonMinor+"
$Python = Get-PythonExecutable
if (-not $Python) {
    Fail "No Python $MinPythonMajor.$MinPythonMinor+ found. Install python3.12 from python.org or via the py launcher and rerun."
}
Note "Using $Python"

# --------------------------------------------------------------- 2. venv

if (-not (Test-Path $VenvPath)) {
    Note "Creating venv at $VenvPath"
    Invoke-Expression "$Python -m venv `"$VenvPath`""
} else {
    Note "Reusing existing venv at $VenvPath"
}

$VenvPy  = Join-Path $VenvPath 'Scripts\python.exe'
$VenvPip = Join-Path $VenvPath 'Scripts\pip.exe'
if (-not (Test-Path $VenvPy)) {
    Fail "venv looks incomplete: $VenvPy is missing"
}

# --------------------------------------------------------------- 3. pip

Note "Upgrading pip"
& $VenvPip install --upgrade --quiet pip

# --------------------------------------------------------------- 4. editable installs

Note "Installing all four packages in editable mode"
& $VenvPip install --quiet -e $Packages[0] -e $Packages[1] -e $Packages[2] -e $Packages[3]

# --------------------------------------------------------------- 5. sanity check

Note "Verifying entry points"
$probe = @"
import importlib
import sys

failures = []
for name in ("suite_common", "inscription", "caseforge", "caseguide"):
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures.append(f"  - {name}: {type(exc).__name__}: {exc}")

if failures:
    print("Some packages failed to import:", file=sys.stderr)
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
"@
$probe | & $VenvPy -

if ($LASTEXITCODE -ne 0) {
    Fail "Import probe failed -- see errors above."
}

# --------------------------------------------------------------- done

Write-Host ''
Write-Host 'Bootstrap complete.' -ForegroundColor Green
Write-Host ''
Write-Host 'Activate the venv:'
Write-Host "  $VenvPath\Scripts\Activate.ps1"
Write-Host ''
Write-Host 'Then run any of the apps:'
Write-Host '  python -m inscription'
Write-Host '  caseforge'
Write-Host '  caseguide'
Write-Host ''
Write-Host 'Run the full test suite across all four packages:'
Write-Host '  .\scripts\run-all-tests.ps1'
Write-Host ''
