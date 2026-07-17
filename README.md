# pkgautotest-analysis

Claude Code tooling to analyze the results of a [PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest)
Nextflow run. It ships as **two complementary Claude Code skills** (with bundled, dependency-free
Python scripts) that answer two different questions:

- **`pkgtest-failure-analysis`** — *why did individual module tests fail?* Reconstructs the aggregate
  report and classifies each failing test (missing shared library, module-load failure, permission
  issue, process/`ulimit` exhaustion, missing CPU instruction, network/download failure, conda-env
  problem, killed/timeout, segfault, syntax error, …).
- **`pipeline-failure-analysis`** — *why did the Nextflow pipeline/engine itself fail or terminate?*
  Parses `.nextflow.log` + `.nextflow/history` to classify the run-termination cause (signal abort
  such as SIGHUP/SIGINT, driver fork/process-limit exhaustion, script-compile/config/plugin/executor
  error, driver OOM, disk, cache/`collectFile` teardown crash, still-running/truncated, …) and drills
  into the `work/` task dirs that were aborted or running when the run stopped.

You install them into a run directory; then, inside Claude Code, you ask a question and get outputs
in that run's `analysis/` folder.

Ask *"analyze this PkgAutoTest run"* (test failures) →

- `analysis/report_out.csv` — per-task PASSED / FAILED / INCOMPLETE / NOT_PROCESSED report for
  every input task (reconstructed even if the run crashed before writing its own report).
- `analysis/failure_analysis.csv` — one row per failing task with a root-cause `category`,
  `confidence`, `evidence` line, and full `evidence_path` to the log that justifies it.
- `analysis/failure_summary.md` — the failures grouped by category, human-readable.
- `analysis/failure_excerpts/` — a compact evidence digest per failing task.

Ask *"why did this Nextflow pipeline fail?"* (pipeline termination) →

- `analysis/pipeline_verdict.csv` — one row: run identity + WorkflowStats tallies + the adjudicated
  `termination_cause`, `confidence`, `evidence`, and `interpretation`.
- `analysis/pipeline_events.csv` — one row per task the engine logged, with its `phase`
  (completed / ignored_error / aborted / running-at-termination / …) and official + on-disk workdir.
- `analysis/pipeline_summary.md` — the verdict, lifecycle, WorkflowStats, and suspect tasks,
  human-readable.
- `analysis/pipeline_excerpts/` — `run_header.txt`, `termination_tail.txt`, and a digest per suspect
  task dir.

## How it works

Both skills use the same **deterministic regex first pass + Claude adjudication + render** pattern.

`pkgtest-failure-analysis`: `triage_failures.py` applies a prioritized signature table to each
failing task's logs and assigns a category + evidence; Claude adjudicates the `unknown`/ambiguous
cases by reading the per-failure digests (which include the module's `test.qsub`); `render_summary.py`
renders the grouped summary. The signatures encode hard-won noise-exclusion rules (the pipeline's own
Xvfb kill, `AVX` strings in paths, echoed `conda activate` lines) so warnings are not mistaken for
causes.

`pipeline-failure-analysis`: `diagnose_pipeline.py` streams `.nextflow.log`, classifies the
run-termination cause via its own signature table, tabulates every task the engine saw, and writes
drill-down digests for the aborted/running tasks; Claude confirms the cause and interprets the log
tail (e.g. distinguishing a genuine abort from its teardown-crash symptom); `render_pipeline_summary.py`
renders `pipeline_summary.md`. It never re-classifies individual test failures (those are the other
skill's job) — a `terminated with an error exit status (254) -- Error is ignored` task is counted, not
blamed.

## Requirements

- **Python 3.6+**, standard library only — **no `pip install`, no third-party packages.**
  Runs on the SCC default `python3` (no `module load` required).
- Claude Code, to drive the skill (the scripts can also be run standalone).

## Install (project-level, per run)

Clone once:

```bash
git clone <repo-url> ~/pkgautotest-analysis
```

After a run finishes, install the skill into that run's directory (the one containing `out.csv`
and `work/`):

```bash
~/pkgautotest-analysis/install.sh /path/to/run    # or run with no arg from inside the run dir
```

This copies **both** skills (with their bundled scripts) to `<run>/.claude/skills/`, so the analysis
travels with the run directory and any colleague who opens it in Claude Code gets them. (Optionally
also `cp ~/pkgautotest-analysis/templates/CLAUDE.md /path/to/run/CLAUDE.md` to give the run dir a
short guide.)

## Use

```bash
cd /path/to/run
claude          # then ask: "analyze this PkgAutoTest run"      (test failures)
                #        or: "why did this Nextflow pipeline fail?" (pipeline termination)
```

Or run the scripts directly from the run directory (they auto-detect the run root):

```bash
# Why individual tests failed:
SK=.claude/skills/pkgtest-failure-analysis/scripts
python3 "$SK/rebuild_report.py"
python3 "$SK/triage_failures.py"
python3 "$SK/render_summary.py"

# Why the pipeline itself terminated:
PK=.claude/skills/pipeline-failure-analysis/scripts
python3 "$PK/diagnose_pipeline.py"          # --session <uuid> / --log <path> for a rotated log
python3 "$PK/render_pipeline_summary.py"
```

## Repo layout

```
skill/pkgtest-failure-analysis/       # skill 1: why individual tests failed
  SKILL.md
  references/categories.md            # failure taxonomy, signatures, noise-exclusion rules
  references/run-layout.md            # PkgAutoTest run directory structure
  scripts/rebuild_report.py           # rebuild report_out.csv
  scripts/triage_failures.py          # regex first-pass classifier + digest generator
  scripts/render_summary.py           # failure_analysis.csv -> failure_summary.md
skill/pipeline-failure-analysis/      # skill 2: why the Nextflow pipeline terminated
  SKILL.md
  references/nextflow-log-format.md   # .nextflow.log grammar + record types + history
  references/termination-causes.md    # termination taxonomy, signatures, noise rules
  scripts/diagnose_pipeline.py        # .nextflow.log parser + termination classifier + digests
  scripts/render_pipeline_summary.py  # pipeline_verdict.csv + pipeline_events.csv -> summary.md
install.sh                            # install BOTH skills into a run directory
templates/CLAUDE.md                   # optional guide to drop into a run directory
```

## Notes

- `resource_fork_exhaustion` failures (`fork`/`pthread_create` → `Resource temporarily unavailable`)
  are the **per-user process limit** (`ulimit -u` / `RLIMIT_NPROC`) being exhausted — typically
  from running many tests concurrently with `nextflow --executor local` on a **login node**
  (compute nodes have `-u unlimited`). They are not package defects: re-run on a compute node
  (`sge` executor) or throttle concurrency (`executor.queueSize` / `process.maxForks`,
  `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`).
  The **same exhaustion hitting the Nextflow driver itself** (`Cannot run program "bash": error=11`,
  `unable to create native thread`) is a pipeline-level termination, classified by
  `pipeline-failure-analysis` as `driver_fork_exhaustion` — same cause, same fix.
- `INCOMPLETE` tasks never wrote a result (aborted before finishing) — they are re-runs, not
  diagnosable failures. When a run was aborted (e.g. SIGHUP on a login node), the pipeline skill
  explains *why* it stopped and why `report_out.csv` is missing; the test skill's `rebuild_report.py`
  then reconstructs the report.
- Re-running `triage_failures.py` / `diagnose_pipeline.py` overwrites its CSV, reverting Claude's
  adjudications; do the judgment pass after the final parse, then render the summary.
