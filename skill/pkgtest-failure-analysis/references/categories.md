# Failure category taxonomy & signatures

Reference for the `pkgtest-failure-analysis` skill. The categories, priority order, and
signature/noise regexes here mirror the `SIGNATURES` table in `scripts/triage_failures.py`. Use this
when adjudicating `unknown`/low-confidence/mixed rows by hand. Its companion,
`harness-contract.md`, covers what the harness requires of a `test.qsub` — read that when the logs
look clean but the test still failed, or when a task never ran at all.

## How test.qsub encodes pass/fail (read this first)

A module's `test.qsub` (permanent source: `/share/pkg.8/<mod>/<ver>/test/test.qsub`, = `out.csv`
`test_path`, also embedded in each digest and in `.command.sh`):
1. `module purge` then `module load <deps> <module>`; echoes `Passed`/`Error` on the load result.
2. Runs a series of commands, each followed by `if [ $? -eq 0 ]; then echo Passed; else echo Error`.
   Detailed output is appended to `$LOG_FILE` (→ `log.txt`); the `Passed`/`Error` words go to
   stdout (→ `results.txt`).
3. Some test.qsub files use a check helper that prints `Executable failed to run` /
   `Keyword check failed` / `File existence check failed` / `File content check failed`
   (`test #N of M`) to stderr (→ `.command.err`); captured as `harness_verdict`.

Consequence: **the Nth `Error` line in `results.txt` = the Nth command block in `test.qsub`.** To
find the cause, locate the first failing command in test.qsub, then read the corresponding output
in `log.txt`. If `module load` itself failed (first line is `Error`), it is a `module_load_failed`.

**But an `Error` is not the only way to fail.** The pipeline requires *all three* of: at least one
`Passed`, **zero other lines on stdout** (`results.txt` is the job's stdout), and exit code 0. So a
test can be FAILED with no `Error` at all — see `stray_stdout` below — and `tests_failed=0` on a
FAILED row is the signature of that. Full contract, including the `doc/` assertion helpers behind
each `harness_verdict` phrase and the scheduler-level failures (`Eqw`, "no suitable queues",
exit 137, the reapers): **`harness-contract.md`**.

## Determining the root cause "per test.qsub logic"

- Only count what makes the test.qsub emit `Error` (or a non-zero exit). Ignore anything the test
  merely prints as info/warning.
- A `No such file` for a path the test.qsub *creates or expects as its own input* is not a
  dependency failure — read the script to tell the difference (→ often `missing_file_or_input` or a
  genuine upstream failure that prevented the file from being produced).
- Prefer the **first / root** error in a cascade (e.g. a missing `.so` that then causes a Python
  `ModuleNotFoundError` → classify as `missing_shared_library`, not `python_exception`).

## Noise to ignore (verified false positives)

- `... Killed ... Xvfb` — the pipeline appends an Xvfb kill (issue #18); never a failure.
- The token `AVX`/`avx2` in paths, echoed `QSUB_FILE=test.nonavx2.qsub`, and TensorFlow oneDNN
  "optimized to use ... AVX2" info lines — **not** a CPU fault. Only `Illegal instruction`/`SIGILL`
  is.
- Bare `conda activate <env>` (an echoed usage instruction) — not a conda error.
- Recurring `Permission denied` on `/scratch/.../etc/pki/...` and `/boot/lost+found` — shared
  cleanup noise, unrelated to the module.
- JVM `unable to create native thread: possibly out of memory or process/resource limits reached`
  — this is the fork/resource-exhaustion crash artifact → `resource_fork_exhaustion`, not
  `out_of_memory`.
- `grep: log.txt: No such file or directory` — the pipeline greps `$LOG_FILE` unconditionally, but
  a test.qsub that delegates everything to a sibling script (e.g. `./bash_tests.sh`) never writes
  `log.txt`. Excluded from `missing_file_or_input`; when it is the *only* thing in `.command.err`
  the row will come through as `unknown` — read the delegated script to find the real cause
  (the digest only carries test.qsub itself). Example: `R/4.3.1`.
- Any line containing `warning` (case-insensitive) or a toil `[I]`/`[W]` level marker.

## Categories (priority order — first match wins)

Genuine, specific causes rank above `resource_fork_exhaustion`, which ranks above symptom-like
categories the crash can induce (oom/killed/segfault/generic-file), so a real cause is preferred
and crash symptoms are not mislabeled.

1. **test_template_not_implemented** (high) — **no test has been written for this package**:
   `test.qsub` is still the unmodified skeleton, containing `module load ... xyz/0.0.0` and/or the
   placeholder `xyz_command`. Checked from test.qsub itself **before** the signature table, because
   the resulting `module(s) are unknown` in `.command.err` otherwise reads as
   `module_load_failed` — i.e. reports a broken modulefile for a package that simply has no test.
   Not a package defect; the action is "write the test". Examples: `afni/26.2.03-omp`,
   `tesmall/2.0.8`, `wine/11.11`.
2. **module_load_failed** (high) — `module load` failed / a dependency module is missing or
   conflicts. `Lmod has detected the following error`, `Unable to load module because of error`,
   `module(s) are unknown`, `Cannot load module X because these module(s) are loaded`.
   Example: `cellranger/7.2.0`.
3. **missing_shared_library** (high) — a runtime `.so` cannot be found/resolved.
   `error while loading shared libraries`, `cannot open shared object file`,
   `ImportError: <lib>.so...:`, `undefined symbol:`. Examples: `gdal/3.10.2`
   (`liblber-2.4.so.2`), `lammps/2Aug2023` (`libnetcdf.so.15`).
4. **linker_compile_failure** (high) — a test that compiles/links a program fails at build.
   `/usr/bin/ld:`, `undefined reference to`, `collect2: error`, `cannot find -l`,
   `fatal error: <hdr>.h: No such file`. Example: `fftw/3.3.11`.
5. **conda_env_error** (high) — conda/env activation or resolution error (NOT a bare
   `conda activate`). `CondaError`, `EnvironmentNameNotFound`, `Could not find conda environment`,
   `PackagesNotFoundError`, `CommandNotFoundError...conda`, `conda: command not found`.
6. **network_download_error** (high) — could not reach/download from the network.
   `Could not resolve host`, `Connection refused/timed out`, `Failed to connect`,
   `Temporary failure in name resolution`, `Network is unreachable`, `urlopen error`, `SSLError`,
   `curl: (N)`, `wget: ... unable/failed`. Example: `nodejs/20.12.2`.
7. **cpu_illegal_instruction** (high) — binary used an instruction the CPU lacks.
   `Illegal instruction`, `illegal hardware instruction`, `SIGILL`. Never triggered by the word
   `AVX` alone.
8. **mpi_interconnect_error** (high) — an InfiniBand/UCX **fabric fault** aborted the MPI job.
   `Transport retry count exceeded`, `ib_mlx5_log.c:<N>`, `ibv_<fn> failed`, `UCX ERROR`. Must
   outrank `segfault_crash`, which would otherwise catch the resulting `Aborted`/signal 6 and label
   an infrastructure fault as a program crash. Same "re-run, not a defect" family as
   `resource_fork_exhaustion`. **Fix: re-run; if it recurs on the same node, report that node to
   the fabric admins.** Example: `openmpi/4.1.5` (`synd 0x15 vend 0x81` on `scc-ze2`).
9. **resource_fork_exhaustion** (medium) — process/thread creation failed with `Resource
   temporarily unavailable` (EAGAIN) because the **per-user process limit** (`ulimit -u` /
   `RLIMIT_NPROC`) was exhausted. This happens when many tests run concurrently under
   `--executor local` on a **login node** (e.g. scc6 has `-u 1024`; compute nodes have
   `-u unlimited`) — each test spawns many procs/threads (MPI ranks, JVM GC threads, OpenBLAS
   `pthread_create ... for thread N of 24`, etc.), collectively blowing past the cap. **Not a
   package defect.** Signatures: `fork: [retry:] Resource temporarily unavailable`,
   `pthread_create failed: Resource temporarily unavailable`, `libgomp: Thread creation failed`,
   JVM `unable to create native thread`/`Cannot create worker thread`/`Out of system resources`,
   and lmod `TermWidth.lua ... attempt to index a nil` (a downstream symptom). Example:
   `chemdoodle/12.9.0`. **Fix: re-run on a compute node (`sge` executor, `-u unlimited`) or
   throttle concurrency (`executor.queueSize`/`process.maxForks`, `OMP_NUM_THREADS`,
   `OPENBLAS_NUM_THREADS`).**
10. **out_of_memory** (medium) — genuine OOM. `Out of memory`, `std::bad_alloc`,
   `Cannot allocate memory`, `MemoryError`, `OutOfMemoryError` (excluding the native-thread string
   above).
11. **killed_or_timeout** (medium) — process killed or hit a time limit (exclude Xvfb kills).
   `<pid> Killed`/`Terminated`, `DUE TO TIME LIMIT`, `timeout: sending`. Example: `matlab/2024b`.
12. **segfault_crash** (medium) — `Segmentation fault`, `SIGSEGV`, `core dumped`,
    `Trace/breakpoint trap`, `Aborted (core dumped)`.
13. **permission_denied** (medium) — a real permission error (exclude `/etc/pki`,
    `/boot/lost+found` noise). Example: `hisat-3n/2.2.1` (`/usr/bin/env: 'python': Permission
    denied`).
14. **command_not_found** (medium) — `command not found`, `/usr/bin/env: <x>: No such file`,
    `: not found`.
15. **python_exception** (medium) — a Python-level failure not already caught as a missing `.so`.
    `ModuleNotFoundError`, `Traceback (most recent call last)`, `ImportError`.
16. **syntax_error** (medium) — a script/source syntax error in the test or program.
    `syntax error`, `SyntaxError`, `unexpected end of file/token`, `parse error near`.
17. **disk_quota** (medium) — `No space left on device`, `Disk quota exceeded`.
18. **missing_file_or_input** (low) — generic missing/stale file when nothing more specific
    matched. `No such file or directory`, `does not exist`, `cannot stat`,
    `cannot create directory ... File exists` (stale leftover), and a program's own
    `Failed to open file/input` / `unable to open file` (which names the path but no errno — this
    include exists so the `transient_io_error` downgrade below can re-stat it). Verify against
    test.qsub whether the file is the test's own expected input or a produced artifact.
    Excludes `grep: log.txt: No such file` (harness noise — see above).
19. **transient_io_error** (medium) — not a signature but a **post-check downgrade** of
    `missing_file_or_input`: if the evidence line names a path under `/share/pkg*` that is present
    and readable **at analysis time**, the file was never missing (the package tree is read-only
    and the test does not create it), so the open failed transiently — a filesystem hiccup, or a
    container started without the right `--bind`. The evidence is annotated
    `[file present and readable at analysis time]`. **Fix: re-run.** Example: `astra-sim/2.2.0`
    (a Singularity run failing to open an `.et` file that exists, mode 644, unchanged for months).
20. **stray_stdout** (high) — the test failed **only because something other than `Passed`/`Error`
   reached stdout**, breaking rule 3 of the harness contract. There is no `Error` line at all:
   `results.txt` holds ≥1 `Passed` plus at least one other line, and `tests_failed=0` on a FAILED
   row. A **test-harness defect**, not a package defect: the script's own messages belong on stderr
   or in `$LOG_FILE`. Read from `results.txt` rather than the logs, but deliberately a **fallback
   after every log signature above, not a pre-check** — a crash can *be* the stray line (a program
   writing to stdout instead of `$LOG_FILE`), so a genuine cause in the logs must win; only when
   nothing matched is the stray line itself the whole story. Also skipped when an `Error` exists (the
   failing check is then the cause) and for `INCOMPLETE` tasks (never got a verdict; `results.txt`
   was cut off mid-run). See `harness-contract.md` for the three-part pass criterion.
21. **unknown** — no signature matched; must be adjudicated from the digest (test.qsub + logs). If
    still undeterminable, leave `unknown` and list under "Needs review".

## Extending — propose, don't edit

When adjudication reveals a genuine pattern with no signature, **do not add it to `SIGNATURES`
yourself.** The installed skill is a read-only copy; edit it and the change is invisible to the
maintainer, lost on the next `install.sh`, and this run stops being comparable to every other run.

Instead write the proposal to `analysis/skill_improvement_suggestions.md` (format: see "Proposing
skill changes" in `SKILL.md`), giving the category name, its priority position relative to the
entries above and why, a tight `include_regex`, an `exclude_regex` for any noise it would otherwise
catch, the evidence from this run, and the three files a maintainer must keep in sync:
`SIGNATURES` in `scripts/triage_failures.py`, this file, and the `DESC` map in
`scripts/render_summary.py`. Classify the current row by judgment (`method=claude`) and move on.
