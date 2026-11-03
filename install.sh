#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Plain 'pip install --user' is refused on most current distributions
# (PEP 668, "externally-managed-environment"), so only isolated installers
# are used, in the order the README lists them.
if command -v uv &>/dev/null; then
    uv tool install --force "$SCRIPT_DIR"
    echo "Installed ccdock via uv"
    path_hint="uv tool update-shell"
elif command -v pipx &>/dev/null; then
    pipx install "$SCRIPT_DIR" --force
    echo "Installed ccdock via pipx"
    path_hint="pipx ensurepath"
else
    echo "error: ccdock installs with uv or pipx; install one of them first:" >&2
    echo "       curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    echo "       or: sudo apt install pipx (brew install pipx on macOS)" >&2
    exit 1
fi

if command -v ccdock &>/dev/null; then
    echo "Run 'ccdock' to start"
else
    echo "ccdock is not on your PATH yet: run '$path_hint', then open a new terminal"
fi
