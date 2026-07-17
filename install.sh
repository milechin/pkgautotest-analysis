#!/usr/bin/env bash
# Install the PkgAutoTest analysis skills (with their bundled scripts) into a run directory,
# project-level, so Claude Code discovers them when opened in that run dir. Every skill under
# skill/*/ is installed:
#   - pkgtest-failure-analysis   (why individual module tests failed)
#   - pipeline-failure-analysis  (why the Nextflow pipeline itself failed/terminated)
#
# Usage:  ./install.sh [RUN_DIR]      (RUN_DIR defaults to the current directory)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RUN_DIR="${1:-$PWD}"
if [[ ! -d "$RUN_DIR" ]]; then
  echo "error: run directory not found: $RUN_DIR" >&2
  exit 1
fi
RUN_DIR="$(cd "$RUN_DIR" && pwd)"

# Sanity check: does this look like a PkgAutoTest / Nextflow run? (informational, not fatal)
if [[ ! -f "$RUN_DIR/out.csv" || ! -d "$RUN_DIR/work" ]] && \
   ! compgen -G "$RUN_DIR/.nextflow.log*" > /dev/null && [[ ! -d "$RUN_DIR/.nextflow" ]]; then
  echo "WARNING: $RUN_DIR does not look like a PkgAutoTest/Nextflow run" >&2
  echo "         (expected out.csv + work/, or a .nextflow.log). Installing anyway." >&2
fi

mkdir -p "$RUN_DIR/.claude/skills"
for SKILL_SRC in "$REPO_DIR"/skill/*/; do
  [[ -d "$SKILL_SRC" ]] || continue
  name="$(basename "$SKILL_SRC")"
  DEST="$RUN_DIR/.claude/skills/$name"
  rm -rf "$DEST"
  cp -r "$SKILL_SRC" "$DEST"
  echo "Installed skill -> $DEST"
done

echo
echo "Next steps:"
echo "  cd \"$RUN_DIR\""
echo "  claude          # then ask: \"analyze this PkgAutoTest run\""
echo "                  #        or: \"why did this Nextflow pipeline fail?\""
echo
echo "Outputs will be written to $RUN_DIR/analysis/"
