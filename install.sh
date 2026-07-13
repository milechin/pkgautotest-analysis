#!/usr/bin/env bash
# Install the pkgtest-failure-analysis skill (with its bundled scripts) into a PkgAutoTest run
# directory, project-level, so Claude Code discovers it when opened in that run dir.
#
# Usage:  ./install.sh [RUN_DIR]      (RUN_DIR defaults to the current directory)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_SRC="$REPO_DIR/skill/pkgtest-failure-analysis"

RUN_DIR="${1:-$PWD}"
if [[ ! -d "$RUN_DIR" ]]; then
  echo "error: run directory not found: $RUN_DIR" >&2
  exit 1
fi
RUN_DIR="$(cd "$RUN_DIR" && pwd)"

if [[ ! -f "$RUN_DIR/out.csv" || ! -d "$RUN_DIR/work" ]]; then
  echo "WARNING: $RUN_DIR does not look like a PkgAutoTest run" >&2
  echo "         (expected out.csv and work/). Installing anyway." >&2
fi

DEST="$RUN_DIR/.claude/skills/pkgtest-failure-analysis"
mkdir -p "$RUN_DIR/.claude/skills"
rm -rf "$DEST"
cp -r "$SKILL_SRC" "$DEST"

echo "Installed skill -> $DEST"
echo
echo "Next steps:"
echo "  cd \"$RUN_DIR\""
echo "  claude          # then ask: \"analyze this PkgAutoTest run\""
echo
echo "Outputs will be written to $RUN_DIR/analysis/"
