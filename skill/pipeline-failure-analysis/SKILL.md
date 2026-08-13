---
name: pipeline-failure-analysis
description: >-
  Diagnose why a PkgAutoTest *Nextflow pipeline run* itself failed, terminated, or was aborted —
  as opposed to why individual module tests failed. Parses .nextflow.log (+ .nextflow/history) to
  classify the run-termination cause (SIGHUP/SIGINT/SIGTERM signal abort, driver fork/process-limit
  exhaustion, script-compile/config/plugin/executor error, driver OOM, disk, cache/collectFile
  teardown crash, still-running/truncated, ...) and drills into the specific work/ task dirs that
  were aborted or running at termination. Use when the run died, was killed/aborted, produced no
  report_out.csv, the engine crashed, or the user asks "why did the pipeline (not a test) stop?".
  Hybrid: deterministic regex first pass + Claude judgment on the cause and the log tail.
---

# PkgAutoTest pipeline failure analysis

Explains why the **Nextflow pipeline/engine itself** ended the way it did — the run-level
counterpart to the `pkgtest-failure-analysis` skill (which explains why individual *tests* failed).
It reads `.nextflow.log` and `.nextflow/history`, classifies the run-termination cause, and drills
into the `work/XX/<hash>/` task dirs that were aborted/running when the run stopped. Outputs go to an
**`analysis/`** folder in the run directory: `analysis/pipeline_verdict.csv` (one adjudicated row),
`analysis/pipeline_events.csv` (per-task engine view), `analysis/pipeline_summary.md` (human-readable),
and `analysis/pipeline_excerpts/` (evidence digests). See `references/nextflow-log-format.md` for the
log grammar and record types, and `references/termination-causes.md` for the taxonomy, signatures, and
noise rules.

Run the commands below **from the run directory**. `$SK` is this skill's `scripts/` dir; for a
project-level install that is `.claude/skills/pipeline-failure-analysis/scripts` (adjust if installed
elsewhere). All scripts are Python 3.6+, standard library only (`module load python3` first only if
`python3` is unavailable).

## Scope — this skill vs. `pkgtest-failure-analysis` (read first)

- **This skill** answers *why the run/engine terminated* (session-level): aborted by a signal, the
  driver ran out of processes/heap, a config/script error, etc.
- **`pkgtest-failure-analysis`** answers *why an individual test failed* (task-level): missing `.so`,
  module-load failure, etc.
- A task-level non-zero exit in the log (`status: COMPLETED; exit: 254`) is an **ignored per-test
  failure** — PkgAutoTest runs `errorStrategy 'ignore'`, so those never stop the pipeline. This skill
  **counts** them (as `ignored_error`) but never treats them as a termination cause; defer to the
  sibling skill for them.
- A missing `report_out.csv` is usually explained here (the run was aborted before the final
  `collectFile`). Reconstruct the report with the sibling's `rebuild_report.py`.

## How a Nextflow run's lifecycle appears in the log (essential context)

`.nextflow.log` is the **engine** log (a task's own stdout/stderr lives in its `work/` dir, not here).
Its header names the run (`Run name:`, `Session UUID:`, launch command, executor, `Work-dir:`). Each
task is `Submitted process > runTests (mod/ver)` and later logged as a `TaskHandler[... status:
COMPLETED|RUNNING|ABORTED; exit: N; workDir: ...]` — **status COMPLETED means the engine ran it, not
that the test passed** (a failed test is COMPLETED with a non-zero `exit`). A run that ends normally
prints `Execution complete -- Goodbye`; a run that is interrupted also prints a `Session aborted --
Cause: <signal>` (or `Adieu` on SIGINT) — **so `Goodbye` alone does not mean success**. The final
`WorkflowStats[...]` line carries the succeeded/failed/ignored/aborted/running tallies. Tasks that were
RUNNING at an abort leave a `work/` dir with no `test_metrics.csv` (INCOMPLETE) — those are the
drill-down suspects.

## Workflow

1. **First-pass diagnosis.** Run:
   ```bash
   python3 "$SK/diagnose_pipeline.py"
   ```
   This finds the active `.nextflow.log` (use `--session <uuid>` or `--log <path>` for a rotated
   `.nextflow.log.N`), classifies the termination cause via the signature table, and writes
   `analysis/pipeline_verdict.csv` (one row; `method=regex`), `analysis/pipeline_events.csv` (one row
   per task with its `phase` and official+local workdir), and digests in `analysis/pipeline_excerpts/`
   (`run_header.txt`, `termination_tail.txt`, and one per suspect task dir). It prints the cause, the
   WorkflowStats, and a task-phase tally.

2. **Adjudicate the cause (Claude judgment).** Read `analysis/pipeline_excerpts/run_header.txt` and
   `termination_tail.txt`. Using `references/termination-causes.md`:
   - Confirm or correct `termination_cause`/`confidence`, ignoring the documented noise (the abort's
     own kill sweep, the trailing LevelDB/`collectFile` teardown NPE, etc. — these are *consequences*,
     not causes; prefer the earliest root cause).
   - Interpret any trailing stack trace in plain language.
   - Edit that row in `analysis/pipeline_verdict.csv` in place: set `termination_cause`, `confidence`,
     `method=claude`, and fill `interpretation` (a sentence or two on what happened and what to do).
   - If a genuine, recurring termination pattern has no matching signature, add it to `SIGNATURES` in
     `scripts/diagnose_pipeline.py` (keep priority order: specific/genuine above symptom-like) and to
     `references/termination-causes.md`.

3. **Drill into suspect tasks (optional).** For the aborted/running-at-termination tasks listed in
   `pipeline_events.csv`, open their digest in `analysis/pipeline_excerpts/<module>__<tag>.txt`
   (`.command.err/.log` tails, `.exitcode`, INCOMPLETE flag) to confirm they were simply cut off (a
   re-run) rather than failing on their own.

4. **Render the summary.** Run `python3 "$SK/render_pipeline_summary.py"` to (re)generate
   `analysis/pipeline_summary.md` from the (now-adjudicated) CSVs: verdict + interpretation, run
   identity/lifecycle, WorkflowStats, tasks-by-phase, the suspect list, and — if `report_out.csv` is
   absent — a pointer to the sibling's `rebuild_report.py`.

## Notes
- Re-running `diagnose_pipeline.py` overwrites `pipeline_verdict.csv` (reverting the step-2 edits) and
  regenerates digests. Do the adjudication after the final parse, then render.
- The log records **official** (execution-time) paths; if the run was relocated, the scripts map them
  back to the on-disk location to open work dirs. `--official-prefix PATH` sets the recorded root
  explicitly; `--local-paths` disables the rewrite. This is the mirror image of the sibling skill,
  which rewrites local→official for *display*.
- `driver_fork_exhaustion` (engine `Cannot run program "bash": error=11` / `unable to create native
  thread … process/resource limits`) is the **per-user process limit** (`ulimit -u`) hit by the
  Nextflow driver itself — the pipeline-level twin of the sibling's per-test `resource_fork_exhaustion`.
  Same cause, same fix: re-run on a compute node (`sge` executor) or throttle concurrency
  (`executor.queueSize`/`process.maxForks`). Not a package defect.
- `signal_abort` with SIGHUP typically means the controlling terminal/login session ended or the node
  rebooted under a long `--executor local` run — re-run detached (`nohup`/`tmux`) or on a compute node.
- If the log has no terminal marker at all (`still_running_or_truncated`), the run may still be in
  progress, or was hard-killed (SIGKILL/OOM-killer/node crash) with no clean shutdown — corroborate
  with `.nextflow/history` (duration `-`) and the log's mtime.
