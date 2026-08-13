# The test.qsub ↔ PkgAutoTest contract

Reference for the `pkgtest-failure-analysis` skill: **what the harness requires of a `test.qsub`,
and therefore what "failed" actually means.** Read this before adjudicating a row whose cause is not
obvious from the logs — most surprising failures are a rule below being broken, not the software
being broken.

Distilled from the RCS agent docs in the private `bu-rcs/scc-agents` repo (branch
`install_software_v2`), chiefly
`.claude/skills/scc-install-from-source/references/testing-and-promotion.md` and
`.claude/references/troubleshooting.md`, which in turn cite
[PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest)'s own README and the RCS
`Writing-Software-Tests` procedure. Those are upstream: if a fact here decides a call, re-verify it
there (`module load gh`, then `gh api ...`) rather than trusting this copy.

## The pass criterion (three parts, not one)

A test is `PASSED` only if **all three** hold — from `pkgtest.nf`:

```bash
grep -c Passed results.txt    > 0      # at least one "Passed"
grep -c -v Passed results.txt == 0     # and NO other lines at all
EXIT_CODE == 0                         # test.qsub's own exit status
```

Consequences that matter for triage:

- `results.txt` **is** the job's stdout. **Any extra line on stdout fails the test**, even a helpful
  one, and even alongside a `Passed`. A test can therefore be `FAILED` with **zero `Error` lines** —
  that is the `stray_stdout` category, and the only evidence is `results.txt` itself.
- Output on **stderr does not fail a test**. That is why a well-written `test.qsub` sends its own
  diagnostics to stderr (→ `.command.err`) and the software's output to `$LOG_FILE` (→ `log.txt`).
- `log_error_count` counts the word `error` in the log file and is **explicitly not used** for the
  verdict. Never treat the word "error" in `log.txt` as the failure — a program that prints it while
  succeeding is not failed for it.
- `tests_passed`/`tests_failed` in `test_metrics.csv` are literal **counts** of the words `Passed`
  and `Error`. A `FAILED` row with `tests_failed=0` therefore failed on part 2 (stray stdout) or
  part 3 (exit code), not on a check.

## `template_test.qsub` — the skeleton, verbatim

`newpkg` seeds every `<pkg>/<version>/test/` with this file (plus a `doc` symlink to newpkg's
helper scripts). A `test.qsub` still in this state means **no test has been written**, which is
what `test_template_not_implemented` matches:

```bash
#!/bin/bash -l

# N Logfile name ? first argument
LOG_FILE=$1
rm -f $LOG_FILE

module purge
module load xyz/0.0.0

# Run the executable as a minimal test.
xyz_command  &>> $LOG_FILE

# Check if command ran successfully
if [ $? -eq 0 ]; then
   echo "Passed"
else
   echo "Error"
fi

# Implement additional tests as appropriate.
# See doc/ for examples.
```

Three structural facts the harness depends on: `#!/bin/bash -l` is a **login** shell (required for
`module` to be defined at all); the log path is **`$1`**; and the literal word `Passed` — not the
exit code alone — is the success signal.

## The five rules a test.qsub must satisfy

From the RCS `Writing-Software-Tests` procedure. Each maps to a failure shape when broken:

| Rule | Broken → what you see |
|---|---|
| 1. Accept the log-file path as `$1` (`LOG_FILE=$1`) | writes to a stray path or an empty `$LOG_FILE`; `log.txt` absent or empty |
| 2. Send all software console output to `$LOG_FILE` with `&>>` | evidence lands in `.command.out`/`.command.err` instead of `log.txt`, or (rule 3) fails the test outright |
| 3. **Only `Passed`/`Error` on stdout**; the script's own messages go to stderr | → `stray_stdout`: FAILED with no `Error` at all |
| 4. Request the resources the module needs (`-pe omp N`, MPI PE, GPU, memory) | under-request → reaped or OOM-killed; impossible request → the job never runs (see below) |
| 5. **Write nothing into the test directory** — use `$TMPDIR` | Nextflow copies the whole `test/` dir per run, so leftovers get copied every time; an unreadable leftover fails *staging* before the test starts (uv/0.7.3: a mode-600 `.ruff_cache` file owned by another user → `cp: cannot open ... Permission denied`, task `INCOMPLETE`) |

## The `doc/` assertion helpers — what each failure phrase came from

`newpkg` symlinks its helper snippets into every package's `test/doc/`. Installers copy the body of
one into `test.qsub`, which is why the same few phrases recur in `.command.err` (they are what
`harness_verdict` captures). Knowing the helper tells you what the check actually asserted:

| Helper | Asserts | Failure phrase / shape |
|---|---|---|
| `shell_error.sh` | the executable runs at all — proves it is on `PATH` and its `.so`s resolve. The default for a binary | `Executable failed to run` |
| `string_in_file.sh` | a keyword appears in an output file (`grep`, `-w` for whole word) | `Keyword check failed` — **and the classic false failure**: a bare `grep -wq <word>` also matches the word inside an unrelated warning, failing a test whose real checks all passed |
| `compare_files.sh` | an output matches a reference byte-for-byte (`cmp`) | `File content check failed` — a diff against a stored `*.save`/reference output, not a crash |
| `do_files_exist.sh` | an expected output file/dir was created | `File existence check failed` |
| `min_file_size.sh` | an output is ≥ N bytes — catches a 0-byte file from a program that opened it then died | `File existence check failed` / size check |
| `compare_dirs.sh` | two dirs hold the same filenames and structure | content/existence check |
| `file_type.sh` | an output has the expected type/extension | content check |
| `open_virtual_gui.sh` | starts an Xvfb on a random `$DISPLAY` for headless GUI rendering | the trailing `Killed ... Xvfb` line — **documented noise**, never the failure |

When a check's own phrasing and the log disagree, the helper's semantics decide: a `cmp` mismatch
means the program *ran* (its exit was fine) and only its output differed, whereas
`Executable failed to run` means it never produced anything.

## Where the run's metadata comes from

`find_qsub.py` walks `module avail`, locates each `<pkg_dir>/<version>/test/test.qsub`, and reads
`INSTALLER`, `INSTALLED` and the category **out of the module's `notes.txt`**. Its CSV columns are
`module_name, version, module_name_version, module_pkg_dir, module_installer,
module_install_date, module_category, module_prereqs, test_path, qsub_options`.

- A **blank `installer`** has two possible causes: the module's `notes.txt` has an empty
  `INSTALLER:` field (a real data gap, reported as-is), or the analysis never loaded the input CSV
  (see `run-layout.md` — the input CSV is not always named `out.csv`).
- `--skip`/`--exclusion` are what produce `skipped.log`; those modules never become tasks.
- `qsub_options` from the CSV is appended to the job's `clusterOptions`
  (`-P <project> -N nf_<module>_<version> <qsub_options>`) — so a bad or unsatisfiable option in a
  module's own test dir breaks *submission* of that one task.

## When a test never ran, or was killed from outside

These produce `INCOMPLETE` tasks and empty logs, so they are diagnosed from the scheduler, not the
task dir:

- **`Eqw` (queued in error, never starts).** Documented causes for a hand-written `test.qsub`:
  Windows **CRLF** line endings (`file test.qsub` says "with CRLF line terminators"), a `cd` into a
  path that does not exist, and wrong directory permissions. `qstat -j <id> | grep error` says which.
- **"no suitable queues" (never submitted).** No queue satisfies the requested combination — the
  usual trigger is a resource mix plus a runtime only buy-in `-pub` queues could serve (98 of 100
  `-pub` queues cap at exactly `12:00:00`). This is the gdal/3.1.2 shape: `-pe mtresf 5` in
  `qsub_options`, Nextflow retrying and ignoring `Error submitting process`. Find what *would*
  satisfy a request without submitting: `qselect -l h_rt=... -l mem_per_core=... -pe omp8 -U $USER`.
  Note a merely *unlikely* request produces no error and simply waits forever.
- **Exit 137 = 128 + 9**, i.e. `SIGKILL` from outside. Three causes: the wall clock (`qacct -j <id>`
  runtime ≈ requested `h_rt`; an exact `12:00:00`/`24:00:00` is the giveaway), the OOM killer
  (`dmesg -T` on the node, search `Out of memory`), or a process reaper (the user gets email; killed
  PIDs appear in the node log with **no explanation** — that absence is what distinguishes it from
  OOM).

### The reapers — a second kill mechanism, distinct from `ulimit -u`

BU runs four reaper daemons. The two that matter here:

| Reaper | Threshold | Consequence |
|---|---|---|
| Login node | total CPU time **> 15 min AND > 25% of the process's lifetime** | process killed, email from `root@scc.bu.edu` |
| CPU limit (compute nodes) | the job's `Rate (%)` exceeds **(requested cores + 1) × 100%**, only after it has run **≥ 5 min** | **the whole job is terminated** |

`Rate (%)` is the integral of CPU consumed over the job's lifetime, **for the job as a whole** — how
many processes or threads it spawns is irrelevant, only the total. So this is *not* the same thing as
`resource_fork_exhaustion`, which is the per-user **process count** limit (`ulimit -u` /
`RLIMIT_NPROC`) refusing a `fork`/`pthread_create` with `EAGAIN`. Both are "re-run correctly, not a
package defect", but the fix differs: the reaper wants a request that matches actual usage
(`OMP_NUM_THREADS`/`-j` ≤ `$NSLOTS`), while fork exhaustion wants a compute node instead of a login
node (or fewer concurrent tests). Live view of the reaper's own numbers:
`/usr/local/etc/pr1.x -b`.

## Pipeline defaults worth knowing

From `pkgtest.nf`: `executor = sge`, `errorStrategy = ignore` (the run keeps going after a test
fails — which is why a task's non-zero exit is an *ignored per-test* failure and never a
pipeline-termination cause), `project = rcstest`, `keep_passed = true`. Each `test_metrics.csv` is
collected with `keepHeader` into `report_<csv_input>.csv`; `installer`, `category` and
`install_date` in that report come from `notes.txt` via the input CSV.
