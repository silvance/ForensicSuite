"""Execute the shipped PowerShell templates' logic under real PowerShell.

The installer/launcher templates only ever ran on operator machines,
and two bugs reached the field that any execution would have caught.
The worst: ``$x = if ($c) { @(Get-Models) }`` unrolls a one-element
array (the ``if`` statement re-enumerates its output), so a
single-model bundle exported ``SUITE_LLM_MODEL="q"`` -- the first
character of the model name -- through two "fixes" reasoned about but
never executed.

These tests lift the real function definitions and statements out of
the template files via the PowerShell parser (no copies that can
drift), then run them against fake bundle trees. They need ``pwsh``;
GitHub's ubuntu runners ship it, and locally the module skips when it
is absent.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

PWSH = shutil.which("pwsh")
if PWSH is None and os.environ.get("CI"):
    # GitHub runners ship pwsh; if it ever disappears, fail loudly rather
    # than let the only execution coverage the templates have skip silently.
    pytest.fail("pwsh not found on the CI runner", pytrace=False)
pytestmark = pytest.mark.skipif(PWSH is None, reason="PowerShell (pwsh) not installed")

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "scripts" / "templates"

#: PowerShell prelude: Import-TemplateCode pulls named functions and
#: the assignment to a named variable out of a script's AST and returns
#: their source, which the caller Invoke-Expression's at top-level scope
#: (dot-sourcing inside a helper function would scope the definitions
#: away again).
_PRELUDE = r"""
$ErrorActionPreference = 'Stop'
function Get-TemplateCode([string]$Path, [string[]]$Functions, [string]$AssignTo) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) { throw "parse errors in ${Path}: $($errors[0].Message)" }
    $parts = @()
    foreach ($name in $Functions) {
        $fn = $ast.Find({ param($n)
            $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name
        }, $true)
        if (-not $fn) { throw "function $name not found in $Path" }
        $parts += $fn.Extent.Text
    }
    if ($AssignTo) {
        $assign = $ast.Find({ param($n)
            $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
            $n.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $n.Left.VariablePath.UserPath -eq $AssignTo
        }, $true)
        if (-not $assign) { throw "assignment to `$$AssignTo not found in $Path" }
        $parts += $assign.Extent.Text
    }
    return ($parts -join "`n")
}
"""


def _pwsh(body: str, **env: str) -> str:
    assert PWSH is not None
    proc = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", _PRELUDE + body],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **env},
        check=False,
    )
    assert proc.returncode == 0, f"pwsh failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    return proc.stdout


# ---------------------------------------------------------------- syntax


def _all_ps1() -> list[Path]:
    skip = {".venv", "dist", "build", "node_modules", ".git"}
    return sorted(p for p in REPO.rglob("*.ps1") if not skip & set(p.relative_to(REPO).parts))


@pytest.mark.parametrize("script", _all_ps1(), ids=lambda p: str(p.relative_to(REPO)))
def test_script_parses(script: Path) -> None:
    out = _pwsh(
        r"""
$tokens = $null; $errors = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($env:SCRIPT, [ref]$tokens, [ref]$errors)
$errors | ForEach-Object { "{0}:{1}: {2}" -f $_.Extent.StartLineNumber, $_.Extent.StartColumnNumber, $_.Message }
""",
        SCRIPT=str(script),
    )
    assert out.strip() == "", f"PowerShell parse errors:\n{out}"


# ------------------------------------------------- launcher model selection


def _fake_bundle(root: Path, models: list[str]) -> Path:
    lib = root / "models" / "manifests" / "registry.ollama.ai" / "library"
    lib.mkdir(parents=True)
    for model in models:
        name, tag = model.split(":", 1)
        (lib / name).mkdir(exist_ok=True)
        (lib / name / tag).write_text("{}", encoding="utf-8")
    return root


def _bundled_models(root: Path, *, have_ollama: bool = True) -> list[str]:
    """Run start-suite.ps1's real Get-BundledModels + the real
    ``$bundledModels = ...`` statement; report what [0]/Count see."""
    out = _pwsh(
        r"""
$Root = $env:BUNDLE_ROOT
$HaveOllama = [bool]::Parse($env:HAVE_OLLAMA)
Invoke-Expression (Get-TemplateCode $env:TPL -Functions 'Get-BundledModels' -AssignTo 'bundledModels')
@{ count = $bundledModels.Count; first = if ($bundledModels.Count) { $bundledModels[0] } else { $null };
   all = @($bundledModels) } | ConvertTo-Json -Compress
""",
        TPL=str(TEMPLATES / "start-suite.ps1"),
        BUNDLE_ROOT=str(root),
        HAVE_OLLAMA=str(have_ollama),
    )
    result = json.loads(out)
    if result["count"]:
        # [0] is exactly what the launcher exports as SUITE_LLM_MODEL.
        assert result["first"] == result["all"][0]
    return result["all"] if result["count"] else []


def test_single_model_exports_the_full_name_not_its_first_character(tmp_path) -> None:
    root = _fake_bundle(tmp_path, ["qwen2.5:7b-instruct-q5_K_M"])
    assert _bundled_models(root) == ["qwen2.5:7b-instruct-q5_K_M"]


def test_multiple_models_are_listed_sorted(tmp_path) -> None:
    root = _fake_bundle(tmp_path, ["qwen2.5:14b-instruct-q4_K_M", "granite4:tiny-h"])
    assert _bundled_models(root) == ["granite4:tiny-h", "qwen2.5:14b-instruct-q4_K_M"]


def test_no_models_dir_yields_empty(tmp_path) -> None:
    assert _bundled_models(tmp_path) == []


def test_lite_bundle_without_ollama_yields_empty(tmp_path) -> None:
    root = _fake_bundle(tmp_path, ["qwen2.5:7b-instruct-q5_K_M"])
    assert _bundled_models(root, have_ollama=False) == []


# ------------------------------------------- installer manifest verification


def _manifest_failures(root: Path, manifest_files: dict[str, str], *ignore: str) -> list[str]:
    out = _pwsh(
        r"""
Invoke-Expression (Get-TemplateCode $env:TPL -Functions 'Get-ManifestVerificationFailures')
$files = $env:MANIFEST | ConvertFrom-Json
$ignore = @($env:IGNORE -split ',' | Where-Object { $_ })
$bad = @(Get-ManifestVerificationFailures -ManifestFiles $files -Root $env:TREE -IgnoreTopDirs $ignore)
ConvertTo-Json -InputObject @($bad | ForEach-Object { $_.Trim() }) -Compress
""",
        TPL=str(TEMPLATES / "install.ps1"),
        TREE=str(root),
        MANIFEST=json.dumps(manifest_files),
        IGNORE=",".join(ignore),
    )
    return json.loads(out)


def _sha(data: bytes) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tree(root: Path) -> dict[str, str]:
    files = {
        "start-suite.ps1": b"launcher",
        "Inscription/Inscription.exe": b"MZ-app",
        "Inscription/_internal/python312.dll": b"MZ-dll",
    }
    for rel, data in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
    (root / "manifest.json").write_text("{}", encoding="utf-8")  # exempt by design
    return {rel: _sha(data) for rel, data in files.items()}


def test_manifest_verify_accepts_an_exact_tree(tmp_path) -> None:
    manifest = _tree(tmp_path)
    assert _manifest_failures(tmp_path, manifest) == []


def test_manifest_verify_flags_stale_missing_and_unexpected_files(tmp_path) -> None:
    """The post-install check exists to catch an install that 'succeeded'
    over old files -- a stale binary must be reported, not waved through."""
    manifest = _tree(tmp_path)
    (tmp_path / "Inscription" / "Inscription.exe").write_bytes(b"MZ-OLD-app")
    (tmp_path / "start-suite.ps1").unlink()
    (tmp_path / "stray.txt").write_text("x", encoding="utf-8")

    bad = _manifest_failures(tmp_path, manifest)

    assert "hash mismatch: Inscription/Inscription.exe" in bad
    assert "missing: start-suite.ps1" in bad
    assert "unexpected file (not in manifest): stray.txt" in bad
    assert len(bad) == 3


def test_manifest_verify_ignores_preserved_ai_dirs(tmp_path) -> None:
    """Downloaded ollama/ + models/ survive lite upgrades and are not in a
    lite manifest; the post-install check must not call them tampering."""
    manifest = _tree(tmp_path)
    (tmp_path / "ollama").mkdir()
    (tmp_path / "ollama" / "ollama.exe").write_bytes(b"MZ-ollama")
    (tmp_path / "models" / "blobs").mkdir(parents=True)
    (tmp_path / "models" / "blobs" / "sha256-abc").write_bytes(b"weights")

    assert _manifest_failures(tmp_path, manifest, "ollama", "models") == []
    assert len(_manifest_failures(tmp_path, manifest)) == 2  # without the exemption
