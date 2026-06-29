#!/usr/bin/env bash
# ============================================================
#   Build the air-gapped Inscription Suite USB bundle.
#
#   Make this file executable (one-time):
#       chmod +x Build-Bundle.sh
#   Then run with a double-click in your file manager (most
#   GNOME / KDE distros prompt to "Run" or "Run in Terminal"),
#   or from a terminal:
#       ./Build-Bundle.sh
#
#   It will:
#     1. Set up the Python venv if needed (one-time)
#     2. Verify Ollama and the extracted ollama-linux-amd64.tgz
#        are present (link if not)
#     3. Pop a folder picker for the USB drive (zenity if
#        installed; falls back to a terminal prompt)
#     4. Pull models, build apps, write the bundle
#     5. Show a "done" message with the path
#
#   No need to remember --destination / --ollama-bundle flags.
#   Re-run any time the source code or models change to refresh.
# ============================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"
VENV_ACTIVATE="${VENV_DIR}/bin/activate"
SCRIPTS_DIR="${REPO_ROOT}/scripts"
SKIP_SETUP=0
DESTINATION=""
OLLAMA_BUNDLE_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --destination) DESTINATION="$2"; shift 2 ;;
        --ollama-bundle) OLLAMA_BUNDLE_DIR="$2"; shift 2 ;;
        --skip-setup) SKIP_SETUP=1; shift ;;
        -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

write_step() {
    echo
    echo "==> $1"
}

show_info() {
    local title="$1"
    local body="$2"
    if command -v zenity >/dev/null 2>&1; then
        zenity --info --title="$title" --text="$body" --no-wrap 2>/dev/null || true
    else
        echo
        echo "===== $title ====="
        echo "$body"
        echo "==================="
    fi
}

show_error() {
    local title="$1"
    local body="$2"
    if command -v zenity >/dev/null 2>&1; then
        zenity --error --title="$title" --text="$body" --no-wrap 2>/dev/null || true
    fi
    # Always echo so the terminal log captures it too.
    echo
    echo "ERROR: $title" >&2
    echo "$body" >&2
}

# 1. First-run setup: create venv + install [dev] extras. ------------------

if [[ ! -f "$VENV_ACTIVATE" && "$SKIP_SETUP" == "0" ]]; then
    write_step "First-run setup: creating .venv (this only happens once)"

    bootstrap=""
    # Candidate list orders newer minors first so a box with 3.14 +
    # 3.12 picks 3.14. ``python3`` and ``python`` are PATH defaults
    # that may resolve to whichever 3.x is installed; the version
    # check below validates them before use.
    for candidate in python3.14 python3.13 python3.12 python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            ver=$("$candidate" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "")
            if [[ "$ver" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
                # ``major > 3`` so any future 4.x qualifies; equal-major
                # path requires minor >= 12. The old check
                # ``major >= 3 && minor >= 12`` would have rejected
                # 4.0 / 4.1 etc. as "not Python 3.12+".
                if (( BASH_REMATCH[1] > 3 || ( BASH_REMATCH[1] == 3 && BASH_REMATCH[2] >= 12 ) )); then
                    bootstrap="$candidate"
                    echo "  Found Python ${BASH_REMATCH[1]}.${BASH_REMATCH[2]} via: $candidate"
                    break
                fi
            fi
        fi
    done
    if [[ -z "$bootstrap" ]]; then
        show_error "Inscription Suite -- Build Bundle" \
"Python 3.12+ was not found on this machine.

Install it (e.g. on Fedora: sudo dnf install python3.12; on
Debian/Ubuntu: sudo apt install python3.12 python3.12-venv
python3.12-dev), then re-run this script."
        exit 1
    fi
    echo "  Bootstrap interpreter: $bootstrap"

    "$bootstrap" -m venv "$VENV_DIR"

    write_step "Installing the four packages editable + [dev] extras"
    # shellcheck disable=SC1091
    source "$VENV_ACTIVATE"
    python -m pip install --upgrade pip
    python -m pip install \
        -e "${REPO_ROOT}/suite_common" \
        -e "${REPO_ROOT}/inscription[dev]" \
        -e "${REPO_ROOT}/caseforge[dev]" \
        -e "${REPO_ROOT}/caseguide[dev]"
    echo "  .venv ready."
fi

if [[ ! -f "$VENV_ACTIVATE" ]]; then
    show_error "Inscription Suite -- Build Bundle" \
".venv missing at $VENV_DIR, and --skip-setup was passed.

Drop --skip-setup or create the venv manually first (see SETUP.md)."
    exit 1
fi

# shellcheck disable=SC1091
source "$VENV_ACTIVATE"

# 2. Verify Ollama is on PATH. --------------------------------------------

if ! command -v ollama >/dev/null 2>&1; then
    show_error "Inscription Suite -- Build Bundle" \
"Ollama is not installed on this machine.

Install it (e.g. curl -fsSL https://ollama.com/install.sh | sh), then
re-run this script. The bundle pulls its model weights via Ollama, so
it has to be present on the build machine."
    exit 1
fi

# 3. Find the extracted ollama-linux-amd64 tarball directory. -------------
# This is the package-airgapped.sh prerequisite. Look in obvious
# locations or prompt with zenity if missing.

if [[ -z "$OLLAMA_BUNDLE_DIR" ]]; then
    for candidate in \
        "$HOME/Downloads/ollama-linux-amd64" \
        "$HOME/ollama-linux-amd64" \
        "$REPO_ROOT/ollama-linux-amd64"; do
        if [[ -x "$candidate/bin/ollama" ]]; then
            OLLAMA_BUNDLE_DIR="$candidate"
            break
        fi
    done
fi

if [[ -z "$OLLAMA_BUNDLE_DIR" || ! -x "$OLLAMA_BUNDLE_DIR/bin/ollama" ]]; then
    if command -v zenity >/dev/null 2>&1; then
        OLLAMA_BUNDLE_DIR=$(zenity --file-selection --directory \
            --title="Pick the extracted ollama-linux-amd64 folder" \
            --filename="$HOME/Downloads/" 2>/dev/null || echo "")
    fi
    if [[ -z "$OLLAMA_BUNDLE_DIR" || ! -x "$OLLAMA_BUNDLE_DIR/bin/ollama" ]]; then
        show_error "Inscription Suite -- Build Bundle" \
"Couldn't find an extracted ollama-linux-amd64 tarball.

Download ollama-linux-amd64.tgz from
https://github.com/ollama/ollama/releases, extract it:
    tar -xzf ollama-linux-amd64.tgz -C ~/Downloads/

Then re-run this script (or pass --ollama-bundle <path>)."
        exit 1
    fi
fi

# 4. Pick the destination. ------------------------------------------------

if [[ -z "$DESTINATION" ]]; then
    if command -v zenity >/dev/null 2>&1; then
        DESTINATION=$(zenity --file-selection --directory \
            --title="Pick the USB drive (or folder) for the bundle" \
            --filename="/media/" 2>/dev/null || echo "")
    else
        echo
        read -r -p "Destination directory (USB mount point): " DESTINATION
    fi
    if [[ -z "$DESTINATION" ]]; then
        echo "Cancelled." >&2
        exit 0
    fi
fi

# 5. Build the bundle. ----------------------------------------------------
# prepare-bundle.sh handles the FAT32 pre-flight, in-place staging,
# manifest writing, and ollama runtime + model bundling.

write_step "Building bundle to $DESTINATION"
"${SCRIPTS_DIR}/prepare-bundle.sh" \
    --destination "$DESTINATION" \
    --ollama-bundle "$OLLAMA_BUNDLE_DIR"

# 6. Final report. --------------------------------------------------------

bundle_path="${DESTINATION}/InscriptionSuite-Airgapped-Linux"
show_info "Inscription Suite -- Build Bundle" \
"Bundle ready at:

$bundle_path

Unmount the USB and take it to the air-gapped Linux workstation. From
inside the bundle folder, run ./install.sh for a one-step install."
