# `.nextflow.log` format & run-directory pipeline artifacts

Reference for the `pipeline-failure-analysis` skill: the grammar of a Nextflow engine log and the
records that reveal how a [PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest) run started, ran, and
ended. `.nextflow.log` is the **engine** log — a task's own stdout/stderr lives in its `work/` dir
(see the sibling skill's `run-layout.md`), not here. The regexes in `scripts/diagnose_pipeline.py`
are kept in lockstep with this document.

## Log files & rotation

- `.nextflow.log` — the **active** (most recent) run. On each `nextflow run`, the previous log is
  rotated to `.nextflow.log.1`, `.log.2`, … (one session per file). The active log's header
  `Session UUID` matches the **last** row of `.nextflow/history`.
- Pass `--log <path>` to analyze a specific rotated file, or `--session <uuid>` to select the
  `.nextflow.log*` whose header UUID matches.

## Line grammar

```
MMM-DD HH:MM:SS.mmm [thread] LEVEL  logger.name - message
Jul-10 16:56:45.174 [Task submitter] INFO  nextflow.Session - [05/21d578] Submitted process > runTests (alphafold/2.3.1)
```

- The timestamp has **no year** (`Jul-10`). Anchor the year from `.nextflow/history` if needed; watch
  for a Dec→Jan rollover in long runs.
- `LEVEL` is space-padded to 5 (`DEBUG`, `INFO `, `WARN `, `ERROR`). `[thread]` is bracketed
  (`[main]`, `[Task monitor]`, `[Task submitter]`, `[TaskFinalizer-N]`, `[SIGHUP handler]`, …).
- **Continuation lines** (stack traces, indented blocks like `  [operator] collectFile` or a Java
  trace) have **no timestamp** — a parser must attach them to the preceding timestamped record.

## Header block (first ~25 lines) — run identity

- Launch command: `$> nextflow <script.nf> --csv_input out.csv --executor local`
- `N E X T F L O W  ~  version 24.04.4`
- `Session UUID: <uuid>`, `Run name: <name>`, `Executor pool size: N` (⇒ local executor)
- Banner: `Process: <pid>@<host>`, `CPUs/Mem`
- `Work-dir: <path>` — the **authoritative official (execution-time) run root** (strip trailing
  `/work` to get the run root); used for official→local path remapping.

## Task lifecycle records

- **Submitted:** `[XX/6hex] Submitted process > runTests (mod/ver)` — the `[XX/6hex]` is the short
  work-hash tag (`work/XX/<hash>`, first 6 hex of the hash); the module id is in parentheses.
- **TaskHandler:** `TaskHandler[id: N; name: runTests (mod/ver); status: COMPLETED|RUNNING|ABORTED;
  exit: N; error: -; workDir: <official full-hash path>]`. Logged on completion (`Task completed >`)
  and in other engine contexts. Keep the **last** TaskHandler per task — its `status` is the task's
  final engine state. **`status: COMPLETED` ≠ the test passed** — a failed test is COMPLETED with a
  non-zero `exit` (e.g. 254, 129).
- **Ignored per-test failure** (PkgAutoTest uses `errorStrategy 'ignore'`): a `Handling unexpected
  condition for` block (`  task: name=…; work-dir=…` then `  error [ProcessFailedException]: … an
  error exit status (254)`) followed by `NOTE: Process 'runTests (X)' terminated with an error exit
  status (254) -- Error is ignored`. Expected; not a pipeline failure.

## Termination sequence

- **Normal end:** `> Execution complete -- Goodbye` after a `WorkflowStats[…]` with `abortedCount=0`.
- **Signal abort:** `[SIG* handler] … Session aborted -- Cause: SIGHUP|SIGINT|SIGTERM`; on SIGINT the
  engine may instead print `nextflow.Session - Adieu`. Followed by `The following nodes are still
  active:` (dangling operators like `collectFile`/`view`), `WARN … Killing running tasks (N)`, and a
  burst of `Unable to kill runTests (X) -- command: kill -TERM <pid>; exit: 1` (local) / `qdel`
  (sge). **`Goodbye` still prints after an abort** — never treat it alone as success.
- **Driver fork/process-limit exhaustion:** `Session aborted -- Cause: unable to create native
  thread: possibly out of memory or process/resource limits reached` and/or `Cannot run program
  "bash": error=11, Resource temporarily unavailable` (EAGAIN) — the JVM driver itself hit the
  per-user process limit (`ulimit -u`), common under `--executor local` on a login node.
- **`WorkflowStats[...]`:** `succeededCount, failedCount, ignoredCount, abortedCount, runningCount,
  cachedCount, pendingCount, peakRunning, …` — the engine's own tally (a snapshot at shutdown;
  `runningCount` may under-count the tasks a later kill sweep cut off).
- **Teardown crash:** a trailing `ERROR nextflow.extension.OperatorImpl - @unknown` +
  `java.lang.NullPointerException … org.iq80.leveldb … SortFileCollector … CollectFileOp` stack — the
  LevelDB cache was closed mid-`collectFile` by the abort. A **symptom** of the abort (and why
  `report_out.csv` was never written), not the cause.

## `.nextflow/history`

Tab-separated, one row per run: `timestamp \t duration \t run_name \t status \t session_short_id \t
session_uuid \t command`.
- `status`: `OK` / `ERR` (failed or aborted) / `-` (in progress or never finished). `duration`: `-`
  if the run never finished.
- **Correlate on column 6 (the UUID)** — column 5 (the short session id) can be **identical** across
  reruns of the same script, so it is not a reliable key. The UUID matches the log header and the
  `.nextflow/cache/<uuid>/` directory (a binary LevelDB store — not human-readable).

## local vs. sge executor

Detect from the launch command (`--executor local|sge`) and/or `Executor pool size:` (local) vs. grid
handler classes (`n.executor.GridTaskHandler`, `qsub`/`qdel` in kill/submit lines). On `sge`, task
resource limits (`h_rt`/`h_vmem`) kill individual *tasks*; the pipeline-level event is the **driver**
being killed (e.g. the submit host rebooting). `--executor local` reports `peakMemory=0`/`loadMemory=0`
(memory isn't tracked locally) — never read that 0 as an OOM.
