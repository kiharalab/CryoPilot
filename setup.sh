#!/usr/bin/env bash
# Set up CryoPilot from source.
#
#   ./setup.sh [--skip-vesper] [--skip-frontend]
#
# Creates two Python environments with uv (https://docs.astral.sh/uv/):
#   .venv/                 reconstruction agent and web server (Python 3.11)
#   modeling_agent/.venv/  modeling agent (pinned by modeling_agent/uv.lock)
# then builds VESPER and the web frontend, and reports which external
# programs (PyMOL, ChimeraX, Phenix, Rosetta, ripgrep) it can find.
# Running it again updates what is already there.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKIP_VESPER=0
SKIP_FRONTEND=0
for arg in "$@"; do
    case "$arg" in
        --skip-vesper) SKIP_VESPER=1 ;;
        --skip-frontend) SKIP_FRONTEND=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg" >&2; exit 2 ;;
    esac
done

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
WARNINGS=0

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required. Install it with:" >&2
    echo "    curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "then open a new shell and run ./setup.sh again." >&2
    exit 1
fi

step "Reconstruction agent and web server (.venv, Python 3.11)"
cd "$ROOT"
if [ ! -x .venv/bin/python ]; then
    uv venv --python 3.11 .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt -r app/requirements.txt

step "Modeling agent (modeling_agent/.venv)"
(cd "$ROOT/modeling_agent" && uv sync)

if [ "$SKIP_VESPER" -eq 0 ]; then
    step "VESPER"
    if [ -x "$ROOT/modeling_agent/.deps/vesper/VESPER" ]; then
        echo "Already built: modeling_agent/.deps/vesper/VESPER"
    else
        ready=1
        if [ "$(uname -s)" = "Darwin" ]; then
            if command -v brew >/dev/null 2>&1; then
                for formula in fftw libomp; do
                    brew list --versions "$formula" >/dev/null 2>&1 || brew install "$formula"
                done
            else
                warn "VESPER needs Homebrew's fftw and libomp. Install Homebrew, then run ./setup.sh again."
                ready=0
            fi
        else
            if ! command -v gcc >/dev/null 2>&1; then
                warn "VESPER needs gcc. Install it (for example: sudo apt install build-essential), then run ./setup.sh again."
                ready=0
            elif ! printf '#include <fftw3.h>\n' | gcc -E -x c - >/dev/null 2>&1; then
                warn "VESPER needs the FFTW development files (for example: sudo apt install libfftw3-dev, or sudo dnf install fftw-devel), then run ./setup.sh again."
                ready=0
            fi
        fi
        if [ "$ready" -eq 1 ]; then
            (cd "$ROOT/modeling_agent" && uv run python setup_vesper.py)
        else
            WARNINGS=$((WARNINGS + 1))
        fi
    fi
fi

if [ "$SKIP_FRONTEND" -eq 0 ]; then
    step "Web frontend"
    # Vite 7 needs Node.js 20.19+ or 22.12+.
    node_ok="$(node -e 'const [a, b] = process.versions.node.split(".").map(Number); console.log((a === 20 && b >= 19) || (a === 22 && b >= 12) || a >= 23 ? 1 : 0)' 2>/dev/null || echo 0)"
    if command -v npm >/dev/null 2>&1 && [ "$node_ok" = "1" ]; then
        (cd "$ROOT/app/frontend" && npm ci --no-audit --no-fund && npm run build)
    else
        warn "Node.js 20.19+ or 22.12+ was not found, so the web frontend was not built. Install Node.js and run ./setup.sh again."
        WARNINGS=$((WARNINGS + 1))
    fi
fi

step "External programs for the modeling agent"
# Resolve each program the way the modeling agent's own tools do.
report="$(cd "$ROOT/modeling_agent" && .venv/bin/python - <<'EOF' 2>&1
import shutil
import tools, vesper_align, refinement

def ripgrep():
    path = shutil.which("rg")
    if not path:
        raise RuntimeError("rg not found on PATH")
    return path

checks = [
    ("PyMOL", tools._pymol_executable),
    ("ChimeraX", tools._chimerax_executable),
    ("VESPER", vesper_align.executable),
    ("Phenix", lambda: refinement.phenix_executable("real_space_refine")),
    ("Rosetta", refinement.rosetta_scripts_executable),
    ("Rosetta database", refinement.rosetta_database),
    ("ripgrep", ripgrep),
]
missing = 0
for name, resolve in checks:
    try:
        print(f"  found    {name:<17} {resolve()}")
    except Exception as exc:
        missing += 1
        print(f"  MISSING  {name:<17} {exc}")
print(missing)
EOF
)" || { printf '%s\n' "$report"; warn "Could not check the external programs."; report="?"; }
printf '%s\n' "$report" | sed '$d'
missing_count="$(printf '%s\n' "$report" | tail -n 1)"

step "Done"
cat <<EOF
Start the web application:
    .venv/bin/python app/serve.py
Or run the agents from the terminal:
    .venv/bin/python harness.py
    cd modeling_agent && uv run python modeller.py "..."
Both agents need OPENAI_API_KEY in the environment.
EOF
if [ "$(uname -s)" = "Darwin" ]; then
    cat <<EOF

On macOS you can use the modeling agent. The reconstruction agent must run on
the Linux machine where CryoSPARC is installed, because it reads and writes
the CryoSPARC project files directly.
EOF
fi
if [ "$missing_count" != "0" ]; then
    echo
    echo "Install the missing programs above, or point CryoPilot to them with"
    echo "PYMOL_EXECUTABLE, CHIMERAX_EXECUTABLE, VESPER_EXECUTABLE, PHENIX_BIN,"
    echo "ROSETTA_BIN, and ROSETTA3_DB. The setup script does not install them."
fi
if [ "$WARNINGS" -gt 0 ]; then
    echo
    echo "Setup finished with $WARNINGS warning(s); see above."
fi
