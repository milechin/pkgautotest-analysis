# Pipeline termination-cause taxonomy & signatures

Reference for the `pipeline-failure-analysis` skill. The causes, priority order, and signature
regexes here mirror the `SIGNATURES` table in `scripts/diagnose_pipeline.py` and the `DESC` map in
`scripts/render_pipeline_summary.py`. Use this when adjudicating the `termination_cause` in
`pipeline_verdict.csv` by hand. This is about **why the run/engine stopped**, not why an individual
test failed — that is the `pkgtest-failure-analysis` skill's job.

## Determining the cause "at the engine level"

- Classify from `.nextflow.log` engine events, **not** from task-level exit codes. A
  `TaskHandler[... status: COMPLETED; exit: 254]` is an *individual test* failure PkgAutoTest ignores
  (`errorStrategy 'ignore'`) — never a pipeline termination cause.
- Prefer the **earliest root cause** in a shutdown cascade. An abort triggers a kill sweep and often a
  cache/`collectFile` teardown crash; those are *consequences*. Find what caused the abort first.
- `Execution complete -- Goodbye` prints on abort too — it is **not** sufficient to call a run clean.

## Noise to ignore (consequences of a shutdown, not causes)

- `WARN … Killing running tasks (N)` and the burst of `Unable to kill runTests (X) -- command:
  kill -TERM <pid>; exit: 1` (or `qdel` on sge) — the engine tearing down after an abort.
- `The following nodes are still active:` + `[operator] collectFile`/`view` — dangling operators at
  abort; informational.
- The trailing `ERROR … OperatorImpl - @unknown` + `org.iq80.leveldb`/`SortFileCollector`/
  `CollectFileOp` `NullPointerException` — LevelDB closed mid-`collectFile` by the abort (this is why
  `report_out.csv` is missing). A symptom → `cache_teardown_error`, ranked low so it never masks a
  real cause.
- `peakMemory=0` / `loadMemory=0` on `--executor local` — memory isn't tracked locally; not an OOM.
- `RejectedExecutionException` during the abort's `Handling unexpected condition` — a teardown race,
  not the cause.

## Causes (priority order — first non-excluded match wins)

Genuine early/root causes rank above late symptoms. `signal_abort` outranks `completed_clean` (because
`Goodbye` prints on abort); `cache_teardown_error` ranks just above the residual buckets.

1. **script_compile_error** (high) — the pipeline `.nf` failed to compile/parse; no task ran.
   `MultipleCompilationErrorsException`, `Script compilation error`, `startup failed:`,
   `unable to parse script`, `Unknown method … on`, `No signature of method`.
2. **config_error** (high) — a `nextflow.config` problem. `Unknown config attribute`,
   `Cannot parse config file`, `Config file … not found`.
3. **plugin_or_startup_error** (high) — a plugin could not be fetched/resolved at launch.
   `Failed to (download|resolve|pull) plugin`, `Cannot find plugin`, `Unable to (fetch|load) plugin`.
4. **missing_input** (high) — a required input/channel was missing or empty. `Cannot find any input
   file`, `Missing … input file`, `No such file … csv_input`, `Path … does not exist`,
   `Missing required parameter`.
5. **driver_fork_exhaustion** (high) — the Nextflow **driver JVM** could not fork a process / create a
   thread because the **per-user process limit** (`ulimit -u` / `RLIMIT_NPROC`) was exhausted —
   typically `--executor local` on a login node. `Cannot run program "…": error=11` (EAGAIN),
   `error=11, Resource temporarily unavailable`, `unable to create native thread: possibly out of
   memory or process/resource limits`, `pthread_create … Resource temporarily unavailable`,
   `libgomp: Thread creation failed`. **The pipeline-level twin of the sibling skill's per-test
   `resource_fork_exhaustion` — same cause, same fix, not a package defect.** Re-run on a compute node
   (`sge` executor, `-u unlimited`) or throttle concurrency (`executor.queueSize`/`process.maxForks`).
   Example: run `disturbed_yalow` (23 tasks aborted; the driver could no longer fork `bash`).
6. **executor_error** (high) — the executor could not submit/launch a task. `Failed to submit process
   to grid`, `Error submitting process … for execution`, `Cannot run program`, `qsub: … (error|fail)`.
   Excludes `error=11` (that is `driver_fork_exhaustion`).
7. **process_failed_terminate** (high) — a **non-ignored** process error aborted the whole run
   (distinct from the ignored per-test failures). `Error executing process >`, `Execution aborted due
   to an error`, `Pipeline execution stopped with the following message`.
8. **driver_oom** (high) — the driver JVM ran out of heap. `java.lang.OutOfMemoryError`,
   `GC overhead limit exceeded`. (A task's own OOM is not in the engine log; the OOM-killer killing the
   JVM leaves no clean shutdown → `still_running_or_truncated`.)
9. **disk_error** (high) — driver-level `No space left on device` / `Disk quota exceeded`.
10. **signal_abort** (high) — the run got a termination signal. `Session aborted -- Cause: SIGHUP|
    SIGINT|SIGTERM|SIGKILL`, or `nextflow.Session - Adieu` (SIGINT). **Beats `completed_clean`.**
    Sub-classify: SIGHUP = controlling terminal/login session ended or node rebooted (common for long
    `--executor local` runs on a login node → re-run detached / on a compute node); SIGINT = Ctrl-C
    (user); SIGTERM/SIGKILL = scheduler (`qdel`) or `kill`. Examples: `jovial_picasso` (SIGHUP),
    `exotic_bartik` (SIGINT).
11. **completed_clean** (high) — finished normally. Requires **all of**: `Execution complete --
    Goodbye`, a `WorkflowStats[…]` with `abortedCount=0`, no `Session aborted`, and no driver-level
    `ERROR`. Per-test failures on a clean run are the sibling skill's domain, not a pipeline failure.
12. **cache_teardown_error** (medium) — a cache/`collectFile` teardown crash (`OperatorImpl -
    @unknown`, `org.iq80.leveldb`, `SortFileCollector`, `CollectFileOp`). Usually a **symptom** of an
    abort that killed `collectFile`; ranked low so a real cause upstream wins. Only the primary cause
    if it appears on an otherwise-clean run.
13. **still_running_or_truncated** (medium) — no terminal marker at all (no `Goodbye`, no `Session
    aborted`, no `WorkflowStats`). Either genuinely in progress, or hard-killed (SIGKILL / OOM-killer /
    node crash) with no clean shutdown. Corroborate with `.nextflow/history` (status `-`, duration `-`)
    and the log's mtime.
14. **unknown** — no signature matched and the tail gave no determinable cause; adjudicate from
    `pipeline_excerpts/termination_tail.txt`, and if still undeterminable leave `unknown`.

## Extending

When adjudication reveals a recurring genuine termination pattern with no signature, add a tuple to
`SIGNATURES` in `scripts/diagnose_pipeline.py` at the right priority (specific/genuine above
symptom-like), with a tight `include_regex` and an `exclude_regex` for any noise it would otherwise
catch. **Keep three places in sync:** the `SIGNATURES` table, this file, and the `DESC` map in
`scripts/render_pipeline_summary.py`.
