# Failure category taxonomy & signatures

Reference for the `pkgtest-failure-analysis` skill. The categories, priority order, and
signature/noise regexes here mirror the `SIGNATURES` table in `scripts/triage_failures.py`. Use this
when adjudicating `unknown`/low-confidence/mixed rows by hand.

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
- Any line containing `warning` (case-insensitive) or a toil `[I]`/`[W]` level marker.

## Categories (priority order — first match wins)

Genuine, specific causes rank above `resource_fork_exhaustion`, which ranks above symptom-like
categories the crash can induce (oom/killed/segfault/generic-file), so a real cause is preferred
and crash symptoms are not mislabeled.

1. **module_load_failed** (high) — `module load` failed / a dependency module is missing or
   conflicts. `Lmod has detected the following error`, `Unable to load module because of error`,
   `module(s) are unknown`, `Cannot load module X because these module(s) are loaded`.
   Example: `cellranger/7.2.0`.
2. **missing_shared_library** (high) — a runtime `.so` cannot be found/resolved.
   `error while loading shared libraries`, `cannot open shared object file`,
   `ImportError: <lib>.so...:`, `undefined symbol:`. Examples: `gdal/3.10.2`
   (`liblber-2.4.so.2`), `lammps/2Aug2023` (`libnetcdf.so.15`).
3. **linker_compile_failure** (high) — a test that compiles/links a program fails at build.
   `/usr/bin/ld:`, `undefined reference to`, `collect2: error`, `cannot find -l`,
   `fatal error: <hdr>.h: No such file`. Example: `fftw/3.3.11`.
4. **conda_env_error** (high) — conda/env activation or resolution error (NOT a bare
   `conda activate`). `CondaError`, `EnvironmentNameNotFound`, `Could not find conda environment`,
   `PackagesNotFoundError`, `CommandNotFoundError...conda`, `conda: command not found`.
5. **network_download_error** (high) — could not reach/download from the network.
   `Could not resolve host`, `Connection refused/timed out`, `Failed to connect`,
   `Temporary failure in name resolution`, `Network is unreachable`, `urlopen error`, `SSLError`,
   `curl: (N)`, `wget: ... unable/failed`. Example: `nodejs/20.12.2`.
6. **cpu_illegal_instruction** (high) — binary used an instruction the CPU lacks.
   `Illegal instruction`, `illegal hardware instruction`, `SIGILL`. Never triggered by the word
   `AVX` alone.
7. **resource_fork_exhaustion** (medium) — process/thread creation failed with `Resource
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
8. **out_of_memory** (medium) — genuine OOM. `Out of memory`, `std::bad_alloc`,
   `Cannot allocate memory`, `MemoryError`, `OutOfMemoryError` (excluding the native-thread string
   above).
9. **killed_or_timeout** (medium) — process killed or hit a time limit (exclude Xvfb kills).
   `<pid> Killed`/`Terminated`, `DUE TO TIME LIMIT`, `timeout: sending`. Example: `matlab/2024b`.
10. **segfault_crash** (medium) — `Segmentation fault`, `SIGSEGV`, `core dumped`,
    `Trace/breakpoint trap`, `Aborted (core dumped)`.
11. **permission_denied** (medium) — a real permission error (exclude `/etc/pki`,
    `/boot/lost+found` noise). Example: `hisat-3n/2.2.1` (`/usr/bin/env: 'python': Permission
    denied`).
12. **command_not_found** (medium) — `command not found`, `/usr/bin/env: <x>: No such file`,
    `: not found`.
13. **python_exception** (medium) — a Python-level failure not already caught as a missing `.so`.
    `ModuleNotFoundError`, `Traceback (most recent call last)`, `ImportError`.
14. **syntax_error** (medium) — a script/source syntax error in the test or program.
    `syntax error`, `SyntaxError`, `unexpected end of file/token`, `parse error near`.
15. **disk_quota** (medium) — `No space left on device`, `Disk quota exceeded`.
16. **missing_file_or_input** (low) — generic missing/stale file when nothing more specific
    matched. `No such file or directory`, `does not exist`, `cannot stat`,
    `cannot create directory ... File exists` (stale leftover). Verify against test.qsub whether
    the file is the test's own expected input or a produced artifact.
17. **unknown** — no signature matched; must be adjudicated from the digest (test.qsub + logs). If
    still undeterminable, leave `unknown` and list under "Needs review".

## Extending

When adjudication reveals a recurring genuine pattern with no signature, add a tuple to
`SIGNATURES` in `scripts/triage_failures.py` at the right priority (specific/genuine above
symptom-like), with a tight `include_regex` and an `exclude_regex` for any noise it would otherwise
catch, then re-run and re-apply judgment.
