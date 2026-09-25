#!/bin/bash
# Install the pinned dev tools so tests and lint run in Claude Code on the web.
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# The pins live in pyproject.toml's `dev` dependency group; read them from there
# rather than repeating them, so this hook cannot drift from the gate.
mapfile -t dev_pins < <(python3 -c '
import tomllib
with open("pyproject.toml", "rb") as f:
    print("\n".join(tomllib.load(f)["dependency-groups"]["dev"]))
')

# Direct use: `python3 -m pytest`, `ruff check .`.
python3 -m pip install --quiet --disable-pip-version-check --root-user-action=ignore "${dev_pins[@]}"

# The gate commands in verification.toml run through uv, pinned as in CI.
python3 -m pip install --quiet --disable-pip-version-check --root-user-action=ignore uv==0.11.29

# The image preinstalls other uv and ruff versions earlier on PATH; put pip's
# scripts directory first so the pinned ones win for the session.
scripts_dir=$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts"))')
export PATH="$scripts_dir:$PATH"
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "export PATH=\"$scripts_dir:\$PATH\"" >> "$CLAUDE_ENV_FILE"
fi

# Warm uv's cache so `uv run --with <pin>` needs no download mid-session.
with_args=()
for pin in "${dev_pins[@]}"; do with_args+=(--with "$pin"); done
uv run --quiet "${with_args[@]}" python -c pass
