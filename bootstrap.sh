#!/usr/bin/env bash
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
#   ./bootstrap.sh                # default: creates .venv at repo root
#   ./bootstrap.sh /path/to/venv  # custom venv location
#
# After this finishes, activate the venv:
#   source .venv/bin/activate
#
# See SETUP.md for the full per-platform setup walkthrough this
# script automates the POSIX half of.

set -euo pipefail

# --------------------------------------------------------------- config

readonly MIN_PYTHON_MAJOR=3
readonly MIN_PYTHON_MINOR=12
readonly REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly VENV_DIR="${1:-${REPO_ROOT}/.venv}"

# Install order matters: suite_common is a dep of the other three, so
# its editable install must be visible on sys.path before the others
# resolve their pyproject.toml dependencies. The [dev] extras pull
# in pytest / ruff / etc. for the three apps.
readonly PACKAGES=(
    "${REPO_ROOT}/suite_common"
    "${REPO_ROOT}/inscription[dev]"
    "${REPO_ROOT}/caseforge[dev]"
    "${REPO_ROOT}/caseguide[dev]"
)

# --------------------------------------------------------------- helpers

note()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m!!!\033[0m %s\n' "$*" >&2; }
fail()  { printf '\033[1;31m!!!\033[0m %s\n' "$*" >&2; exit 1; }

# --------------------------------------------------------------- 1. Python version check

pick_python() {
    # Prefer python3.12 explicitly; fall back to python3 if it's new
    # enough. Distro python (3.10 / 3.11) is the most common reason
    # this script is invoked on the wrong interpreter -- catch it
    # here rather than letting suite_common's pyproject.toml refuse
    # the install with a confusing "requires >=3.12" error.
    for candidate in python3.12 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            local version
            version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            local major minor
            major="${version%.*}"
            minor="${version#*.}"
            if [ "$major" -gt "$MIN_PYTHON_MAJOR" ] || {
                [ "$major" -eq "$MIN_PYTHON_MAJOR" ] && [ "$minor" -ge "$MIN_PYTHON_MINOR" ]
            }; then
                echo "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

note "Looking for Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+"
if ! PYTHON="$(pick_python)"; then
    fail "No Python ${MIN_PYTHON_MAJOR}.${MIN_PYTHON_MINOR}+ found. Install python3.12 (or a newer 3.x) and rerun."
fi
note "Using $PYTHON"

# --------------------------------------------------------------- 2. venv

if [ ! -d "$VENV_DIR" ]; then
    note "Creating venv at $VENV_DIR"
    "$PYTHON" -m venv "$VENV_DIR"
else
    note "Reusing existing venv at $VENV_DIR"
fi

readonly VENV_PY="${VENV_DIR}/bin/python"
readonly VENV_PIP="${VENV_DIR}/bin/pip"
[ -x "$VENV_PY" ] || fail "venv looks incomplete: $VENV_PY is missing or not executable"

# --------------------------------------------------------------- 3. pip

note "Upgrading pip"
"$VENV_PIP" install --upgrade --quiet pip

# --------------------------------------------------------------- 4. editable installs

note "Installing all four packages in editable mode"
"$VENV_PIP" install --quiet -e "${PACKAGES[@]}"

# --------------------------------------------------------------- 5. sanity check

note "Verifying entry points"
"$VENV_PY" - <<'PYTHON'
import importlib
import sys

failures = []
for name in ("suite_common", "inscription", "caseforge", "caseguide"):
    try:
        importlib.import_module(name)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  - {name}: {type(exc).__name__}: {exc}")

if failures:
    print("Some packages failed to import:", file=sys.stderr)
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
PYTHON

# --------------------------------------------------------------- done

printf '\n\033[1;32mBootstrap complete.\033[0m\n\n'
cat <<EOF
Activate the venv:
  source ${VENV_DIR}/bin/activate

Then run any of the apps:
  python -m inscription
  caseforge
  caseguide

Run the full test suite across all four packages:
  ./scripts/run-all-tests.sh
EOF
