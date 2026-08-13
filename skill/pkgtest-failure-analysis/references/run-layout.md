# PkgAutoTest run directory layout

Reference for the `pkgtest-failure-analysis` skill: the structure of a
[PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest) Nextflow run directory and how to read it.

## Top level

- `out.csv` — the pipeline **input** produced by `find_qsub.py`: one row per module test to run.
  Key columns: `module_name_version`, `module_installer`, `module_category`, `module_install_date`,
  `test_path` (the module's `test.qsub`), `qsub_options`. **The name is not fixed** — the file takes
  the launcher's name (e.g. `modules_crontab.csv`), and the pipeline's report follows it as
  `report_<input>.csv`. Detect it as the top-level `*.csv` whose header has `module_name_version`.
- `skipped.log` — the complement of that input, also from `find_qsub.py`: the modules it did **not**
  emit a row for. `-----`-separated blocks, each a heading line ending in `:` then one entry per
  line — currently `Excluded modules:` (bare module names, i.e. every version, from find_qsub.py's
  blanket exclusion list — typically GUI/interactive/meta packages that are impractical to test)
  and `Tests skipped due to missing a test.qsub:` (`module/version,<pkg_dir>` — no test written).
  These modules never became tasks, so they appear in no report and in no `work/` dir: they are
  **untested**, which is not the same as passing. `render_summary.py` appends them to
  `failure_summary.md` as "Not tested" so a reader does not mistake absence for success.
- `work/XX/<hash>/` — one directory **per Nextflow task** (per test). `XX` = first 2 hex chars of
  the task hash.
- `.nextflow.log*`, `.nextflow/` — engine logs/history (task id → module → workdir; concurrency).
- `report_<input>.csv` — the aggregate report the pipeline writes at the end via `collectFile`
  (e.g. `report_out.csv`). **Absent if the run did not finish** — `rebuild_report.py` reconstructs it.

## Files inside each `work/XX/<hash>/` task dir

- `.command.sh` — the wrapper Nextflow ran. Contains `echo MODULE=<name>/<ver>` (authoritative
  module id) and ``TEST_DIR=`dirname <test_path>` `` (the source test.qsub path).
- `.command.err` / `.command.out` / `.command.log` — stderr / stdout of the wrapper.
- `.exitcode` — the Nextflow task exit code.
- `results.txt` — **the job's stdout**, and the harness's whole verdict input: a sequence of
  `Passed`/`Error` words, one per command the test ran. **The Nth `Error` = the Nth command in
  test.qsub.** Anything else appearing here — any line that is neither word — fails the test on its
  own (→ `stray_stdout`; see `harness-contract.md`).
- `log.txt` — detailed output of the `test.qsub` commands (the richest evidence).
- `test_metrics.csv` — the per-task result row (2 lines: header + data). Its presence means the
  task **finished**.
- `test/` — the copied module test dir. May have been deleted to save space; the source test.qsub
  remains at `test_path` under `/share/pkg.8/...`.

## The three outcome states (do not conflate exit code with test result)

1. **PASSED** — `test_metrics.csv` present, `test_result=PASSED` (results.txt is all `Passed` and
   the test.qsub exit code is 0).
2. **FAILED** — `test_metrics.csv` present, `test_result=FAILED` (an `Error` appeared or a non-zero
   exit). This is what the failure classifier explains.
3. **INCOMPLETE** — **no `test_metrics.csv`** in a task dir that has a `.command.sh`. The task
   started but never wrote a result — it was aborted (killed, timed out, or the run was interrupted).
   These are re-runs, not diagnosable failures.

A task dir with no `.command.sh` (rare stray) should be ignored.

## `test_metrics.csv` columns

`job_number, hostname, qsub_file, test_result, module, tests_passed, tests_failed,
log_error_count, exit_code, installer, category, install_date, workdir`
- `tests_passed`/`tests_failed` = literal counts of the words `Passed`/`Error` in `results.txt`;
  `log_error_count` = count of `error` in `log.txt` (informational; **explicitly not** used for
  pass/fail — never read it as the verdict). A `FAILED` row with `tests_failed=0` failed on one of
  the other two criteria: a stray stdout line (→ `stray_stdout`) or a non-zero `exit_code`.
- `exit_code` is `test.qsub`'s own exit status, distinct from the Nextflow task exit code in
  `.exitcode`.
- `installer`/`category`/`install_date` originate in the module's `notes.txt` (read by
  `find_qsub.py`), so an unfilled `INSTALLER:` field shows up as an empty column here.
- **`workdir` may be stale**: it records the path where the run *executed*. If the run directory
  was later moved/copied, that path differs from the current on-disk location — rewrite the prefix
  (current run root ↔ recorded root) when following it. The analysis scripts auto-detect the
  recorded ("official") root and emit paths under it by default (`--local-paths` to override).

## How test.qsub defines pass/fail

`test.qsub` does `module purge` + `module load <deps> <module>` (echoing `Passed`/`Error` on the
load), then runs commands each followed by `if [ $? -eq 0 ]; then echo Passed; else echo Error`,
appending detail to `$LOG_FILE` (→ `log.txt`). Some tests use a check helper that also prints
`Executable failed to run` / `Keyword check failed` / `File existence|content check failed`
(`test #N of M`) to stderr — captured as `harness_verdict`. To find a cause: locate the first
failing command in test.qsub, then read its output in `log.txt`.
