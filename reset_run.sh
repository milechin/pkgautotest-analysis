#!/usr/bin/env bash
# Reset a PkgAutoTest run directory so the pipeline can be re-run from scratch: remove the
# pipeline-generated artifacts (work/, .nextflow/, logs, out.csv, analysis outputs, ...) while
# preserving the launcher, the installed .claude/ skills, and anything else you have there.
#
# It only ever removes a fixed DENYLIST of known-generated items (kept in sync with .gitignore) at
# the run-dir top level, so it never touches files it doesn't recognize. Dry-run by default: it
# prints what it *would* remove and exits; pass --force to actually delete.
#
# Usage:  ./reset_run.sh [OPTIONS] [RUN_DIR]      (RUN_DIR defaults to the current directory)
#
# Options:
#   -n, --dry-run        Preview only; delete nothing. (default)
#   -f, --force, --yes   Actually delete the listed items.
#       --exclude PATH    Protect a path from removal (repeatable). Matches by run-dir-relative
#                         path or by basename, e.g. --exclude out.csv or --exclude ./analysis.
#   -h, --help           Show this help.
set -euo pipefail

# DENYLIST: known pipeline-generated artifacts, at the run-dir top level only. Keep in sync with
# the "Analysis outputs" / "Pipeline run material" patterns in this repo's .gitignore. Entries are
# literal names or shell globs (expanded within RUN_DIR); everything not listed here is preserved.
DENYLIST=(
  "work"
  ".nextflow"
  "analysis"
  ".nextflow.log*"
  "out.csv"
  "skipped.log"
  "report_*.csv"
)

usage() { awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; }

FORCE=0
EXCLUDES=()
RUN_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--dry-run) FORCE=0 ;;
    -f|--force|--yes) FORCE=1 ;;
    --exclude)
      [[ $# -ge 2 ]] || { echo "error: --exclude needs a value" >&2; exit 2; }
      EXCLUDES+=("${2#./}"); shift ;;
    --exclude=*) ex="${1#*=}"; EXCLUDES+=("${ex#./}") ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "error: unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      [[ -z "$RUN_DIR" ]] || { echo "error: unexpected extra argument: $1" >&2; exit 2; }
      RUN_DIR="$1" ;;
  esac
  shift
done

RUN_DIR="${RUN_DIR:-$PWD}"
if [[ ! -d "$RUN_DIR" ]]; then
  echo "error: run directory not found: $RUN_DIR" >&2
  exit 1
fi
RUN_DIR="$(cd "$RUN_DIR" && pwd)"

# ---- safety guards: refuse obviously-wrong targets --------------------------------------------
if [[ "$RUN_DIR" == "/" || "$RUN_DIR" == "$HOME" ]]; then
  echo "error: refusing to reset $RUN_DIR (root or home directory)" >&2
  exit 1
fi
if [[ -f "$RUN_DIR/install.sh" && -d "$RUN_DIR/skill" ]]; then
  echo "error: $RUN_DIR looks like the pkgautotest-analysis repo, not a run directory." >&2
  echo "       (found install.sh + skill/). Point at an actual run dir." >&2
  exit 1
fi

# ---- build the candidate list: denylist entries that exist, minus --exclude --------------------
is_excluded() {
  local rel="$1" base ex
  base="$(basename "$rel")"
  for ex in "${EXCLUDES[@]:-}"; do
    [[ -n "$ex" ]] || continue
    if [[ "$rel" == "$ex" || "$base" == "$ex" || "$rel" == "${ex%/}" ]]; then
      return 0
    fi
  done
  return 1
}

candidates=()
excluded=()
shopt -s nullglob dotglob
for pattern in "${DENYLIST[@]}"; do
  for path in "$RUN_DIR"/$pattern; do
    [[ -e "$path" ]] || continue
    rel="${path#"$RUN_DIR"/}"
    if is_excluded "$rel"; then
      excluded+=("$rel")
    else
      candidates+=("$path")
    fi
  done
done
shopt -u nullglob dotglob

if [[ ${#excluded[@]} -gt 0 ]]; then
  echo "Protected by --exclude (kept): ${excluded[*]}" >&2
fi

if [[ ${#candidates[@]} -eq 0 ]]; then
  echo "Nothing to remove in $RUN_DIR (no generated artifacts found)." >&2
  exit 0
fi

# ---- report ------------------------------------------------------------------------------------
echo "Run directory: $RUN_DIR" >&2
if [[ $FORCE -eq 0 ]]; then
  echo "[dry-run] would remove:" >&2
else
  echo "Removing:" >&2
fi
for path in "${candidates[@]}"; do
  size="$(du -sh "$path" 2>/dev/null | cut -f1)"
  printf '    %-8s %s\n' "${size:-?}" "${path#"$RUN_DIR"/}" >&2
done
total="$(du -sch "${candidates[@]}" 2>/dev/null | tail -1 | cut -f1)"
echo "    -------- total: ${total:-?}" >&2

# ---- act ---------------------------------------------------------------------------------------
if [[ $FORCE -eq 0 ]]; then
  echo >&2
  echo "Dry-run only; nothing was deleted. Re-run with --force to remove the above." >&2
  echo "Preserved: run_nextflow_tests.sh, .claude/ (installed skills), and everything not listed." >&2
  exit 0
fi

for path in "${candidates[@]}"; do
  rm -rf "$path"
done
echo >&2
echo "Done. $RUN_DIR is reset for a fresh run (e.g. bash run_nextflow_tests.sh)." >&2
