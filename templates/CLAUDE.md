# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this directory is

This is the output directory of a [PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest) Nextflow
run (it contains `out.csv` and `work/`). It is not source code — the task here is to analyze the
run's results and explain what failed and why.

## Analyzing failures

The `pkgtest-failure-analysis` skill is installed under `.claude/skills/` (via the
`pkgautotest-analysis` repo's `install.sh`). To analyze this run, ask Claude to "analyze this
PkgAutoTest run", or follow the skill's workflow directly:

```bash
SK=.claude/skills/pkgtest-failure-analysis/scripts
python3 "$SK/rebuild_report.py"     # -> analysis/report_out.csv (PASSED/FAILED/INCOMPLETE for all tasks)
python3 "$SK/triage_failures.py"    # -> analysis/failure_analysis.csv + analysis/failure_excerpts/
# (Claude adjudicates the unknown/ambiguous rows using the digests)
python3 "$SK/render_summary.py"     # -> analysis/failure_summary.md
```

See `.claude/skills/pkgtest-failure-analysis/references/run-layout.md` for the run directory
structure and `references/categories.md` for the failure taxonomy. Scripts are Python 3.6+,
standard library only.
