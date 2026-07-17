# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this directory is

This is the output directory of a [PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest) Nextflow
run (it contains `out.csv`/`work/` and/or a `.nextflow.log`). It is not source code — the task here
is to analyze the run's results and explain what happened.

## Two analysis skills are installed

Both are under `.claude/skills/` (via the `pkgautotest-analysis` repo's `install.sh`). Pick by
question; scripts are Python 3.6+, standard library only, and write to `analysis/`.

### Why did individual module tests fail? — `pkgtest-failure-analysis`

Ask Claude to "analyze this PkgAutoTest run", or:

```bash
SK=.claude/skills/pkgtest-failure-analysis/scripts
python3 "$SK/rebuild_report.py"     # -> analysis/report_out.csv (PASSED/FAILED/INCOMPLETE/NOT_PROCESSED)
python3 "$SK/triage_failures.py"    # -> analysis/failure_analysis.csv + analysis/failure_excerpts/
# (Claude adjudicates the unknown/ambiguous rows using the digests)
python3 "$SK/render_summary.py"     # -> analysis/failure_summary.md
```

### Why did the Nextflow pipeline itself fail/terminate? — `pipeline-failure-analysis`

Use when the run was aborted/killed, produced no `report_out.csv`, the engine crashed, or you want
to know why the *run* (not a test) stopped. Ask Claude "why did this Nextflow pipeline fail?", or:

```bash
PK=.claude/skills/pipeline-failure-analysis/scripts
python3 "$PK/diagnose_pipeline.py"          # -> analysis/pipeline_verdict.csv + pipeline_events.csv + pipeline_excerpts/
# (Claude confirms the termination cause and interprets the log tail)
python3 "$PK/render_pipeline_summary.py"    # -> analysis/pipeline_summary.md
```

Add `--session <uuid>` or `--log <path>` to diagnose a rotated `.nextflow.log.N` (an earlier run).

## References

- `.claude/skills/pkgtest-failure-analysis/references/` — `run-layout.md` (run directory structure),
  `categories.md` (per-test failure taxonomy).
- `.claude/skills/pipeline-failure-analysis/references/` — `nextflow-log-format.md` (`.nextflow.log`
  grammar), `termination-causes.md` (pipeline-termination taxonomy).

A task's non-zero exit is an *individual test* failure (diagnosed by the first skill); the pipeline
skill explains only run-level termination and points back to `rebuild_report.py` when the aggregate
report is missing.
