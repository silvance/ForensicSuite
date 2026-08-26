#Requires -Version 5.1
<#
.SYNOPSIS
    Install the air-gapped Inscription suite bundle to a permanent location
    on this workstation.

.DESCRIPTION
    Run from inside the bundle directory (e.g. E:\InscriptionSuite-Airgapped\)
    on the offline workstation. Copies the whole bundle to a stable path,
    creates a Start Menu entry that launches start-suite.ps1, and
    (optionally) installs the bundled PowerShell 7 MSI.

    No admin required for the default per-user install. Pointing
    -InstallRoot at C:\Program Files\InscriptionSuite\ requires admin --
    right-click install.ps1 -> Run as administrator first.

    User configuration / saved cases are NOT touched -- those live
    under %LOCALAPPDATA%\Inscription\, %LOCALAPPDATA%\CaseGuide\,
    %LOCALAPPDATA%\CaseForge\, and wherever the operator chose to keep
    case folders. Re-running the installer with -Force overwrites the
    binaries but preserves all of that.

.PARAMETER InstallRoot
    Where to install. Default: %LOCALAPPDATA%\Programs\InscriptionSuite.
    Use C:\InscriptionSuite or C:\Program Files\InscriptionSuite for a
    multi-user install (those need admin).

.PARAMETER Force
    Wipe an existing install at $InstallRoot without prompting.

.PARAMETER DesktopShortcut
    Also drop a Desktop shortcut. Start Menu shortcut is always created.

.PARAMETER InstallPowerShell7
    If a PowerShell-*-win-x64.msi file is present in the bundle (added
    at bundle time via prepare-bundle.ps1 -IncludePowerShell7), launch
    its installer with /qb (basic UI). Skipped silently when no MSI
    is present.

.PARAMETER SkipVerify
    Skip the SHA-256 manifest check. Verifying ~15 GB takes 30-60s on
    a typical workstation; the verify pass guards against a bad copy
    off USB so it's worth running on first install. Subsequent
    re-runs against the same bundle can use this to save time.

.EXAMPLE
    .\install.ps1
    Default per-user install with a Start Menu shortcut.

.EXAMPLE
    .\install.ps1 -DesktopShortcut -InstallPowerShell7
    Per-user install plus desktop shortcut and PS7 (if the MSI was
    bundled in via -IncludePowerShell7 on the build side).

.EXAMPLE
    .\install.ps1 -InstallRoot "C:\InscriptionSuite" -Force
    System-wide install. Right-click -> Run as administrator first.
#>
param(
    [string]$InstallRoot = "$env:LOCALAPPDATA\Programs\InscriptionSuite",
    [switch]$Force,
    [switch]$DesktopShortcut,
    [switch]$InstallPowerShell7,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"
$BundleSrc = $PSScriptRoot

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

# Normalise InstallRoot so relative paths and trailing slashes don't bite
# the source/destination overlap check below. GetFullPath without a base
# resolves relative to the process's current directory.
$InstallRoot = [System.IO.Path]::GetFullPath($InstallRoot)

# 1. Verify we are inside a real bundle --------------------------------------

Write-Step "Checking bundle integrity"
# ollama\ and models\ are OPTIONAL: the lite (GitHub release) bundle
# ships apps-only, and the suite treats local AI as an optional
# engine. Only the apps + launcher are load-bearing.
$expected = @("Inscription", "CaseForge", "CaseGuide", "start-suite.ps1")
foreach ($item in $expected) {
    if (-not (Test-Path (Join-Path $BundleSrc $item))) {
        throw "install.ps1 must run from inside the bundle directory. Missing: $item. Are you inside InscriptionSuite-Airgapped\?"
    }
}
if (-not (Test-Path (Join-Path $BundleSrc "ollama"))) {
    Write-Host "  (lite bundle: no bundled Ollama/models -- AI features optional)" -ForegroundColor Yellow
}
Write-Host "  Bundle source: $BundleSrc"

# 1a. Refuse same/overlapping source and destination ------------------------
# Stops "right-click install.ps1 from inside an existing install" from
# wiping the bundle out from under itself.
$bundleResolved = (Resolve-Path -LiteralPath $BundleSrc).Path.TrimEnd('\')
$installNormalised = $InstallRoot.TrimEnd('\')
if ($bundleResolved -ieq $installNormalised) {
    throw "Source ($BundleSrc) and -InstallRoot ($InstallRoot) are the same path. Re-run install.ps1 from the original bundle (e.g. on USB), or pass a different -InstallRoot."
}
if ($installNormalised -like ($bundleResolved + '\*') -or $bundleResolved -like ($installNormalised + '\*')) {
    throw "Source ($BundleSrc) and -InstallRoot ($InstallRoot) overlap. Pick a destination that is not a subdirectory of the bundle (and vice versa)."
}

function Get-ManifestVerificationFailures {
    <#
    Compare a directory tree against the manifest's files map.
    Pass 1: every manifest entry present with the right SHA-256.
    Pass 2: every on-disk file accounted for (manifest.json itself is
    exempt by design; $IgnoreTopDirs skips trees the manifest is not
    expected to cover, e.g. AI components preserved across a lite
    upgrade). Used both on the bundle BEFORE copying and on the
    installed tree AFTER -- the latter catches antivirus silently
    blocking or rolling back the file replacement.
    #>
    param(
        [Parameter(Mandatory = $true)]$ManifestFiles,
        [Parameter(Mandatory = $true)][string]$Root,
        [string[]]$IgnoreTopDirs = @()
    )
    $bad = @()
    $expectedPaths = New-Object System.Collections.Generic.HashSet[string]
    foreach ($entry in $ManifestFiles.PSObject.Properties) {
        $relPath = $entry.Name
        [void]$expectedPaths.Add($relPath.ToLower())
        $expected = $entry.Value -replace '^sha256:', ''
        $absPath = Join-Path $Root ($relPath -replace '/', '\')
        if (-not (Test-Path -LiteralPath $absPath)) {
            $bad += "  missing: $relPath"
            continue
        }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $absPath).Hash.ToLower()
        if ($actual -ne $expected.ToLower()) {
            $bad += "  hash mismatch: $relPath"
        }
    }
    $rootLength = $Root.TrimEnd('\').Length + 1
    foreach ($file in Get-ChildItem -LiteralPath $Root -Recurse -File) {
        $rel = $file.FullName.Substring($rootLength).Replace('\', '/')
        if ($rel -ieq "manifest.json") { continue }
        $top = ($rel -split '/', 2)[0]
        if ($IgnoreTopDirs -contains $top) { continue }
        if (-not $expectedPaths.Contains($rel.ToLower())) {
            $bad += "  unexpected file (not in manifest): $rel"
        }
    }
    return $bad
}

# 1b. Verify SHA-256 manifest -----------------------------------------------
# A USB transfer can occasionally truncate or corrupt a file; the
# bundle ships with a manifest.json (sha256 of every file as written
# by prepare-bundle.ps1) so we can detect that before copying onto
# the target machine. Older bundles built before this feature have no
# manifest -- fall through with a warning rather than a hard error.

$manifestPath = Join-Path $BundleSrc "manifest.json"
$versionPath  = Join-Path $BundleSrc "version.json"
#: files map of the verified manifest; the post-install check reuses it.
$VerifiedManifestFiles = $null

if ($SkipVerify) {
    Write-Step "Skipping bundle integrity check (per -SkipVerify)"
} elseif (-not (Test-Path $manifestPath)) {
    Write-Step "No manifest.json in bundle -- skipping integrity check"
    Write-Host "  (Bundles built before manifest support went in. Rebuild with prepare-bundle.ps1 to get one.)" -ForegroundColor Yellow
} else {
    Write-Step "Verifying bundle integrity (SHA-256)"
    try {
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    } catch {
        throw "manifest.json is present but unreadable: $_"
    }
    # Guard the schema before walking it: a manifest without a
    # populated ``files`` map (wrong tool version, hand-edited) would
    # otherwise yield an empty known-file set and mislabel every real
    # file "unexpected" -- a confusing wall of errors for what is
    # really a malformed manifest.
    if (-not $manifest.files -or -not ($manifest.files.PSObject.Properties | Select-Object -First 1)) {
        throw "manifest.json has no 'files' map -- it was written by an incompatible bundling tool, not corrupted in transfer. Rebuild the bundle with the current scripts."
    }
    $bad = @(Get-ManifestVerificationFailures -ManifestFiles $manifest.files -Root $BundleSrc)
    if ($bad.Count -gt 0) {
        Write-Host ""
        Write-Host "Bundle integrity check failed:" -ForegroundColor Red
        $bad | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        throw "Bundle is corrupt or tampered -- $($bad.Count) file(s) failed verification. Rebuild and re-copy onto the USB."
    }
    $count = @($manifest.files.PSObject.Properties).Count
    Write-Host "  OK ($count files verified, no unexpected files)"
    $VerifiedManifestFiles = $manifest.files
}

# 1c. Surface bundle version -------------------------------------------------
if (Test-Path $versionPath) {
    try {
        $version = Get-Content -Raw -LiteralPath $versionPath | ConvertFrom-Json
        $sha = if ($version.git_sha) { $version.git_sha.Substring(0, [Math]::Min(8, $version.git_sha.Length)) } else { "unknown" }
        $built = if ($version.build_timestamp) { $version.build_timestamp } else { "unknown" }
        Write-Host "  Bundle version: $sha (built $built)" -ForegroundColor DarkGray
    } catch {
        Write-Host "  version.json present but unreadable; continuing." -ForegroundColor Yellow
    }
}

# 2. Confirm + clear destination ---------------------------------------------

if (Test-Path $InstallRoot) {
    if (-not $Force) {
        Write-Host ""
        Write-Host "$InstallRoot already exists." -ForegroundColor Yellow
        $reply = Read-Host "Overwrite? (y/N)"
        if ($reply -notmatch '^(y|Y)') {
            Write-Host "Cancelled. Existing install left untouched." -ForegroundColor Yellow
            exit 0
        }
    }
    # NOTE: don't Remove-Item $InstallRoot here -- that's a destroy-before-
    # copy ordering, and a copy failure mid-stream loses the working
    # install. The atomic stage-then-swap below runs the new copy to a
    # sibling directory, verifies it, then renames the old aside and the
    # new in. Worst case if the rename fails, the previous install is
    # still intact.
}

# 3. Stage the new copy to a sibling directory, then atomic swap ------------
# install.ps1 used to wipe $InstallRoot before copying, which lost the
# working install if the copy failed halfway (USB unplugged, disk full,
# AV interference). The two-phase pattern below keeps the previous
# install intact until the new one is fully landed.

$stagingRoot = "$InstallRoot.new"
$rollbackRoot = "$InstallRoot.old"
if (Test-Path $stagingRoot) {
    Write-Step "Removing leftover staging dir from a prior aborted install"
    Remove-Item -Recurse -Force $stagingRoot
}
if (Test-Path $rollbackRoot) {
    Write-Step "Removing leftover rollback dir from a prior aborted install"
    Remove-Item -Recurse -Force $rollbackRoot
}

Write-Step "Staging new install to $stagingRoot"
$parent = Split-Path -Parent $InstallRoot
if ($parent -and -not (Test-Path $parent)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}
try {
    Copy-Item -Recurse -Force -Path $BundleSrc -Destination $stagingRoot
} catch {
    if (Test-Path $stagingRoot) {
        Remove-Item -Recurse -Force $stagingRoot -ErrorAction SilentlyContinue
    }
    throw "Copy to staging dir failed: $_. The previous install at $InstallRoot is untouched."
}
$totalBytes = (Get-ChildItem -Recurse -File $stagingRoot | Measure-Object -Property Length -Sum).Sum
$totalGB = [math]::Round($totalBytes / 1GB, 2)
Write-Host "  Staged $totalGB GB."

# 3a. Stop suite processes still running from the old install ---------------
# Renaming a directory fails when ANY file inside is open. The usual
# invisible culprit is our own ollama.exe: the launcher's spawned
# server survives the console being closed with the X button, and a
# "reused" server from an earlier session is never stopped at all.
# The suite apps themselves linger the same way. All of these run
# from inside $InstallRoot, so they're detectable by executable path.

# Process names the suite can leave running from the install folder.
# Needed because the launcher SELF-ELEVATES: apps it starts run as
# administrator, and an unelevated installer gets access-denied trying
# to read an elevated process's Path or Modules -- making path-based
# detection silently blind to exactly the processes most likely to be
# holding the folder. Name matching still works unelevated.
$SuiteProcessNames = @("Inscription", "CaseForge", "CaseGuide", "ollama", "Whispr")

function Get-InstallRootProcesses {
    param([string]$Root)
    $prefix = ($Root.TrimEnd('\') + '\').ToLower()
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $holds = $false
        $pathReadable = $false
        try {
            if ($_.Path) {
                $pathReadable = $true
                if ($_.Path.ToLower().StartsWith($prefix)) { $holds = $true }
            }
        } catch { }  # access denied on elevated/protected processes
        if (-not $holds -and $pathReadable) {
            # A process can also pin the tree via a DLL it loaded from
            # inside it, without its exe living there.
            try {
                foreach ($m in $_.Modules) {
                    if ($m.FileName -and $m.FileName.ToLower().StartsWith($prefix)) {
                        $holds = $true
                        break
                    }
                }
            } catch { }  # access denied / cross-bitness module enumeration
        }
        if (-not $holds -and -not $pathReadable -and ($SuiteProcessNames -contains $_.ProcessName)) {
            # Path unreadable (almost certainly elevated) + a suite
            # process name: assume it's ours. Worst case we ask before
            # stopping a same-named process from elsewhere.
            $holds = $true
        }
        $holds
    })
}

if (Test-Path $InstallRoot) {
    # @() at the call site: PowerShell unrolls one-element function
    # results into a scalar (same bug class as the launcher's model
    # name export -- see start-suite.ps1).
    $running = @(Get-InstallRootProcesses -Root $InstallRoot)
    if ($running.Count -gt 0) {
        Write-Host ""
        Write-Host "Still running from the existing install:" -ForegroundColor Yellow
        $running | ForEach-Object { Write-Host ("  {0} (PID {1})" -f $_.ProcessName, $_.Id) }
        if (-not $Force) {
            $reply = Read-Host "Close them now so the upgrade can proceed? (y/N)"
            if ($reply -notmatch '^(y|Y)') {
                Remove-Item -Recurse -Force $stagingRoot -ErrorAction SilentlyContinue
                throw "Upgrade needs those programs closed. Close them (Quit the suite launcher too) and re-run the installer."
            }
        }
        $needElevation = @()
        foreach ($proc in $running) {
            Write-Host "  Stopping $($proc.ProcessName) (PID $($proc.Id))..."
            try {
                Stop-Process -Id $proc.Id -Force -ErrorAction Stop
            } catch {
                # Access denied: the process is elevated (the launcher
                # self-elevates the apps it starts) and this installer
                # isn't. Collect and stop them via one elevated helper.
                $needElevation += $proc.Id
            }
        }
        if ($needElevation.Count -gt 0) {
            Write-Host "  Those run elevated; stopping them needs one UAC approval..." -ForegroundColor Yellow
            try {
                Start-Process -FilePath "powershell.exe" -Verb RunAs -Wait -WindowStyle Hidden `
                    -ArgumentList "-NoProfile", "-Command", "Stop-Process -Id $($needElevation -join ',') -Force -ErrorAction SilentlyContinue"
            } catch {
                Write-Host "  Elevation declined -- continuing; the swap may fail while they run." -ForegroundColor Yellow
            }
        }
        # Give Windows a beat to release the file handles. Poll by PID:
        # HasExited/WaitForExit throw access-denied on elevated targets.
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            $still = @($running | Where-Object { Get-Process -Id $_.Id -ErrorAction SilentlyContinue })
            if ($still.Count -eq 0) { break }
            Start-Sleep -Milliseconds 500
        }
    }
}

# 3aa. Invalidate the installed version stamp -------------------------------
# Must happen BEFORE anything modifies the install: a partial upgrade
# that leaves a version.json matching the payload makes the setup exe's
# "already installed -> just launch" fast-path skip reinstalling over a
# broken tree forever. (Field bug: a robocopy fallback died halfway but
# had already copied version.json; every later run of the same exe just
# launched the old, broken launcher and nothing the operator did --
# reinstalls, reboots -- appeared to change anything.) The stamp comes
# back only with a fully landed install.
$installedStamp = Join-Path $InstallRoot "version.json"
if (Test-Path -LiteralPath $installedStamp) {
    Remove-Item -LiteralPath $installedStamp -Force -ErrorAction SilentlyContinue
}

# 3b. Wait out the antivirus scan of the staged files -----------------------
# Staging just wrote ~700 brand-new PE binaries with hashes the AV has
# never seen. Real-time protection (Defender's block-at-first-sight
# cloud check in particular) takes EXCLUSIVE holds on such files while
# it scans, which blocks both the directory rename and per-file
# overwrites -- field logs showed exactly the .exe/.dll/.pyd set locked
# with ERROR 32 on every install attempt, across reboots, because each
# re-stage re-triggers the scan. Probe every staged binary with an
# exclusive open and only proceed once the scanner has let go.

Write-Step "Waiting for antivirus/indexer to release freshly staged files"
$pending = @(Get-ChildItem -Recurse -File -LiteralPath $stagingRoot |
    Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd") })
$settleDeadline = (Get-Date).AddSeconds(180)
$lastReport = 0
while ($true) {
    $stillLocked = @()
    foreach ($f in $pending) {
        try {
            $fs = [System.IO.File]::Open($f.FullName, "Open", "Read", "None")
            $fs.Close()
        } catch {
            $stillLocked += $f
        }
    }
    $pending = $stillLocked
    if ($pending.Count -eq 0) {
        Write-Host "  All staged binaries released."
        break
    }
    if ((Get-Date) -ge $settleDeadline) {
        Write-Host "  $($pending.Count) file(s) still held after 180s (e.g. $($pending[0].Name)) -- proceeding anyway; the swap below has its own retries." -ForegroundColor Yellow
        break
    }
    if ($pending.Count -ne $lastReport) {
        Write-Host "  $($pending.Count) file(s) still being scanned; waiting..."
        $lastReport = $pending.Count
    }
    Start-Sleep -Seconds 3
}

Write-Step "Swapping new install in"
# Backoff absorbs the transient cases: AV re-touching files, handle
# teardown from processes stopped above, indexer passes.
$swapWaits = @(2, 4, 8, 15)
$maxAttempts = $swapWaits.Count + 1
for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
    try {
        if (Test-Path $InstallRoot) {
            # Move-aside the old install rather than delete, so we can roll
            # back if the rename of the new one fails (race with AV scanner,
            # file handles still open, etc.).
            Rename-Item -LiteralPath $InstallRoot -NewName (Split-Path -Leaf $rollbackRoot) -ErrorAction Stop
        }
        Rename-Item -LiteralPath $stagingRoot -NewName (Split-Path -Leaf $InstallRoot) -ErrorAction Stop
        break
    } catch {
        if ($attempt -lt $maxAttempts) {
            $wait = $swapWaits[$attempt - 1]
            Write-Host "  Swap attempt $attempt failed ($($_.Exception.Message.Trim())); retrying in ${wait}s..." -ForegroundColor Yellow
            Start-Sleep -Seconds $wait
            continue
        }
        # Best-effort rollback: put the old install back.
        if (-not (Test-Path $InstallRoot) -and (Test-Path $rollbackRoot)) {
            Rename-Item -LiteralPath $rollbackRoot -NewName (Split-Path -Leaf $InstallRoot) -ErrorAction SilentlyContinue
        }

        # Rename-swap is unavailable. A directory RENAME needs zero open
        # handles anywhere in the tree -- an Explorer window merely
        # browsing the folder (or the search indexer touching it) is
        # enough to block it, and such holders show up in no process
        # list. Overwriting the individual FILES only needs those files
        # closed, so fall back to mirroring the staged tree into place
        # with robocopy. Not atomic, but the alternative is a dead end,
        # and the staged source was verified before we got here.
        Write-Host "  Rename swap blocked; updating files in place with robocopy instead..." -ForegroundColor Yellow
        $xd = @()
        foreach ($aiDir in @("ollama", "models")) {
            # Same AI-component preservation rule as the salvage below:
            # a lite bundle must not /MIR away downloaded runtime/models.
            if ((Test-Path (Join-Path $InstallRoot $aiDir)) -and -not (Test-Path (Join-Path $stagingRoot $aiDir))) {
                $xd += (Join-Path $InstallRoot $aiDir)
            }
        }
        # /R:10 /W:5 = up to ~50s of per-file patience -- enough to
        # outlast an AV cloud-verdict hold on an individual binary.
        # /XF version.json: the version stamp lands LAST, by explicit
        # copy after everything else succeeded, so a partial mirror can
        # never claim to be a completed install (see 3aa).
        $roboArgs = @($stagingRoot, $InstallRoot, "/MIR", "/R:10", "/W:5", "/NFL", "/NDL", "/NJH", "/NP", "/XF", "version.json")
        if ($xd.Count -gt 0) { $roboArgs += "/XD"; $roboArgs += $xd }
        & robocopy @roboArgs
        # Robocopy exit codes 0-7 are success grades; 8+ means files failed.
        if ($LASTEXITCODE -ge 8) {
            throw @"
Swap failed after $maxAttempts rename attempts AND the in-place file
update could not complete (robocopy exit $LASTEXITCODE). The previous
install should still be substantially intact at $InstallRoot.

Something is holding files in that folder. Common holders:
  - Antivirus real-time protection scanning the freshly written
    binaries (the usual culprit): add an exclusion for
    $InstallRoot and its '.new' sibling in Windows Security ->
    Virus & threat protection -> Exclusions (or your AV's
    equivalent), re-run the installer, then remove the exclusion.
  - The suite launcher window (its working directory is the install
    folder -- Quit it, and close any PowerShell/cmd window sitting
    in that folder)
  - An Explorer window open inside the install folder
Close the holder and re-run the installer -- or reboot and re-run,
which clears every orphaned handle.
"@
        }
        # Everything else landed -- NOW stamp the version (see 3aa/XF).
        $stagedStamp = Join-Path $stagingRoot "version.json"
        if (Test-Path -LiteralPath $stagedStamp) {
            Copy-Item -Force -LiteralPath $stagedStamp -Destination (Join-Path $InstallRoot "version.json")
        }
        Write-Host "  In-place update complete." -ForegroundColor Green
        Remove-Item -Recurse -Force $stagingRoot -ErrorAction SilentlyContinue
        break
    }
}
if (Test-Path $rollbackRoot) {
    # Preserve downloaded AI components across upgrades: enable-ai.ps1
    # may have added ollama\ / models\ to the previous install, and a
    # lite bundle carries neither -- deleting the old install unmodified
    # would silently throw away gigabytes of runtime + model weights.
    # Salvage only AFTER the swap has succeeded, so a failed swap still
    # rolls back to a complete previous install.
    foreach ($aiDir in @("ollama", "models")) {
        $salvage = Join-Path $rollbackRoot $aiDir
        $landed = Join-Path $InstallRoot $aiDir
        if ((Test-Path $salvage) -and -not (Test-Path $landed)) {
            Write-Host "  Preserving downloaded $aiDir\ from the previous install."
            Move-Item -LiteralPath $salvage -Destination $landed
        }
    }
    Remove-Item -Recurse -Force $rollbackRoot -ErrorAction SilentlyContinue
}

# 3c. Verify the INSTALLED tree against the manifest -------------------------
# "Install reported success but the old files are still on disk"
# happened in the field: some antivirus products (Bitdefender's
# ransomware remediation in particular) treat a mass rewrite of ~700
# program binaries -- exactly what an upgrade is -- as an attack, and
# silently block or ROLL BACK the changes. Re-hash what actually
# landed so interference is a loud, named error instead of a mystery
# where every reinstall "succeeds" and nothing ever changes.
# ollama\ / models\ are exempt from the unexpected-file pass: a lite
# manifest doesn't cover AI components preserved across the upgrade.

if (-not $SkipVerify -and $null -ne $VerifiedManifestFiles) {
    Write-Step "Verifying installed files (SHA-256)"
    $bad = @(Get-ManifestVerificationFailures -ManifestFiles $VerifiedManifestFiles -Root $InstallRoot -IgnoreTopDirs @("ollama", "models"))
    if ($bad.Count -gt 0) {
        Write-Host ""
        Write-Host "Installed files do NOT match the bundle:" -ForegroundColor Red
        $bad | Select-Object -First 15 | ForEach-Object { Write-Host $_ -ForegroundColor Red }
        if ($bad.Count -gt 15) { Write-Host "  ... and $($bad.Count - 15) more" -ForegroundColor Red }
        throw @"
The install finished, but $($bad.Count) file(s) on disk do not match what
the bundle shipped -- the old files are still there. Antivirus
interference is the usual cause: some products silently block or roll
back a mass rewrite of program files, which is exactly what an upgrade
looks like to a behavioural engine.

Add an exclusion for this folder in your antivirus, then re-run:
    $InstallRoot
Bitdefender: Protection -> Antivirus -> Settings -> Manage Exceptions
(and check Protection -> Ransomware Remediation). Windows Defender:
Virus & threat protection -> Manage settings -> Exclusions.
"@
    }
    Write-Host "  OK -- installed files match the bundle."
}

# 4. Create Start Menu shortcut ----------------------------------------------

Write-Step "Creating Start Menu shortcut"
$startMenuParent = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
$startMenuDir = Join-Path $startMenuParent "InscriptionSuite"
if (-not (Test-Path $startMenuDir)) {
    New-Item -ItemType Directory -Path $startMenuDir | Out-Null
}
$startShortcut = Join-Path $startMenuDir "Inscription Suite.lnk"

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($startShortcut)
$lnk.TargetPath = "powershell.exe"
$lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$InstallRoot\start-suite.ps1`""
$lnk.WorkingDirectory = $InstallRoot
$lnk.Description = "Inscription Suite air-gapped launcher (Inscription / CaseForge / CaseGuide)"
$icon = Join-Path $InstallRoot "Inscription\Inscription.exe"
if (Test-Path $icon) {
    $lnk.IconLocation = "$icon,0"
}
$lnk.Save()
Write-Host "  $startShortcut"

# 5. Optional desktop shortcut -----------------------------------------------

if ($DesktopShortcut) {
    Write-Step "Creating Desktop shortcut"
    $desktop = [Environment]::GetFolderPath("Desktop")
    $desktopShortcut = Join-Path $desktop "Inscription Suite.lnk"
    Copy-Item -Force $startShortcut $desktopShortcut
    Write-Host "  $desktopShortcut"
}

# 6. Optional PowerShell 7 install -------------------------------------------

if ($InstallPowerShell7) {
    $msi = Get-ChildItem -Path $InstallRoot -Filter "PowerShell-*-win-x64.msi" -File -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($msi) {
        Write-Step "Installing PowerShell 7 from $($msi.Name)"
        # /qb gives a minimal progress UI rather than a fully silent
        # install; air-gapped admins generally want to see "this is
        # actually running" feedback.
        $proc = Start-Process -FilePath "msiexec.exe" `
            -ArgumentList "/i", "`"$($msi.FullName)`"", "/qb" `
            -Wait -PassThru
        if ($proc.ExitCode -ne 0) {
            Write-Host "  msiexec returned exit code $($proc.ExitCode); see %WINDIR%\Logs\WindowsUpdate or the Windows installer log." -ForegroundColor Yellow
        } else {
            Write-Host "  PowerShell 7 installed." -ForegroundColor Green
        }
    } else {
        Write-Host "No PowerShell 7 MSI found in the bundle -- skipping." -ForegroundColor Yellow
        Write-Host "  (Re-build with prepare-bundle.ps1 -IncludePowerShell7 if you want it bundled.)"
    }
}

# 7. Final report ------------------------------------------------------------

Write-Host ""
Write-Host "Inscription Suite installed." -ForegroundColor Green
Write-Host "  Location:       $InstallRoot"
Write-Host "  Start Menu:     Start -> InscriptionSuite -> 'Inscription Suite'"
if ($DesktopShortcut) {
    Write-Host "  Desktop icon:   'Inscription Suite' on your desktop"
}
Write-Host ""
Write-Host "First launch fires a UAC prompt -- start-suite.ps1 self-elevates so"
Write-Host "Inscription's UI-automation can read elevated forensic tools (AXIOM,"
Write-Host "X-Ways, etc.). Accept the prompt to continue."
