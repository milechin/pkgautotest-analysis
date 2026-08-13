# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

This repo is **not** a PkgAutoTest run and contains no run data. It is the *source* of a Claude Code
skill — `pkgtest-failure-analysis` — plus an installer that copies that skill into a
[PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest) Nextflow run directory. The skill (with its
bundled Python scripts and reference docs) then travels with the run so anyone who opens that run in
Claude Code can triage it.

Two contexts to keep distinct:
- **Here (this repo):** you edit the skill — its scripts, signatures, references, and workflow.
- **A run directory (elsewhere):** where the scripts actually execute, against `out.csv` + `work/`.

## Layout

There are **two sibling skills** under `skill/`; `install.sh` copies every `skill/*/` into a run's
`.claude/skills/`. They answer complementary questions and are kept deliberately separate (see the
scope boundary below).

- `skill/pkgtest-failure-analysis/` — *why individual module tests failed* (task-level).
  - `SKILL.md` — the workflow Claude follows in a run dir (rebuild → triage → adjudicate → render).
  - `scripts/` — `rebuild_report.py`, `triage_failures.py`, `render_summary.py`.
  - `references/categories.md` — failure taxonomy + signature/noise rules (mirrors the `SIGNATURES`
    table in `triage_failures.py`).
  - `references/run-layout.md` — anatomy of a PkgAutoTest run directory.
- `skill/pipeline-failure-analysis/` — *why the Nextflow pipeline/engine itself terminated*
  (session-level).
  - `SKILL.md` — the workflow (diagnose → adjudicate the cause → render).
  - `scripts/` — `diagnose_pipeline.py` (streams `.nextflow.log`, classifies the termination cause,
    tabulates tasks, writes digests), `render_pipeline_summary.py`.
  - `references/termination-causes.md` — termination taxonomy + signatures/noise (mirrors the
    `SIGNATURES` table in `diagnose_pipeline.py` **and** the `DESC` map in `render_pipeline_summary.py`
    — keep all three in sync).
  - `references/nextflow-log-format.md` — `.nextflow.log` grammar, record types, and `history`.
- `install.sh` — copies **both** skills to `<run>/.claude/skills/`.
- `reset_run.sh` — resets a run dir for a re-run by removing a **denylist** of generated artifacts
  (`work/`, `.nextflow/`, `.nextflow.log*`, `out.csv`, `skipped.log`, `analysis/`, top-level
  `report_*.csv`) while preserving the launcher and the installed `.claude/`. Dry-run by default
  (`--force` to delete; `--exclude` to protect paths). Its denylist must stay in sync with the
  generated/ephemeral patterns in `.gitignore` — same "keep in sync" discipline as the signature
  tables below.
- `templates/CLAUDE.md` — a short guide dropped into a *run* dir (do not confuse with this file).

## Scope boundary between the two skills (do not blur it)

`pkgtest-failure-analysis` reads a task's own `test.qsub`/`results.txt`/`log.txt` and produces many
per-test rows. `pipeline-failure-analysis` reads the **engine** log (`.nextflow.log`) and produces one
run-level verdict + a few drill-downs. The pipeline skill must **never** re-classify an individual
test failure: a `TaskHandler[... status: COMPLETED; exit: 254]` is an *ignored per-test* failure
(PkgAutoTest runs `errorStrategy 'ignore'`) — it is counted, never blamed as a termination cause. The
one shared narrative is fork/process-limit exhaustion (`ulimit -u`): the test skill's
`resource_fork_exhaustion` (per test) and the pipeline skill's `driver_fork_exhaustion` (the driver
JVM) are the same root cause at two levels.

## Testing changes

There is no test suite, lint, or build. The scripts are **Python 3.6+, standard library only — no
`pip`, no third-party packages, no `module load` required** (must run on the SCC default `python3`).
Preserve that constraint; do not add imports outside the stdlib.

To exercise a change you need a real run dir — ask the user which one to use; runs are transient and
none is referenced here on purpose. Prefer one that exercises the case you touched (e.g. an aborted
run with rotated `.nextflow.log.N` for multiple termination causes). A run may have its own
`analysis/`, so point `--output-dir`/`--analysis-dir` at a scratch dir rather than overwriting it.

```bash
# test-failure skill:
SK=skill/pkgtest-failure-analysis/scripts
python3 "$SK/rebuild_report.py"  --run-dir /path/to/run --output ...
python3 "$SK/triage_failures.py" --run-dir /path/to/run --output ...
python3 "$SK/render_summary.py"  --run-dir /path/to/run --output ...

# pipeline-failure skill (against a real run, writing to scratch):
PK=skill/pipeline-failure-analysis/scripts
python3 "$PK/diagnose_pipeline.py"       --run-dir /path/to/run --output-dir /path/to/scratch
python3 "$PK/diagnose_pipeline.py" --log /path/to/run/.nextflow.log.1 ...   # a rotated session
python3 "$PK/render_pipeline_summary.py" --run-dir /path/to/run --analysis-dir /path/to/scratch
```

Every script auto-detects the run root by walking up from CWD (then from the script's own path);
`--run-dir` overrides. They are standalone (no cross-imports) and each duplicates its own
`find_run_root` — but the two skills' copies differ on purpose: the test skill requires `out.csv` +
`work/`; the pipeline skill's is **relaxed** to also accept a dir with `.nextflow.log` (a pipeline can
die before `work/` is populated). Path remapping is also mirror-imaged: the test skill rewrites
local→official for *display*; the pipeline skill rewrites official→local to *open* the work dirs the
log records.

## How the scripts fit together

Both skills follow the same shape: **deterministic first pass + Claude judgment + render.**

### `pkgtest-failure-analysis` (three scripts)

1. **`rebuild_report.py`** reconstructs `report_out.csv` for a crashed/incomplete run (the pipeline
   normally writes it via `collectFile` only on success). Every input-CSV row maps to exactly one of
   `PASSED` / `FAILED` (copied verbatim from a completed `test_metrics.csv`), `INCOMPLETE` (task dir
   exists but wrote no `test_metrics.csv`), or `NOT_PROCESSED` (input row Nextflow never ran).
   Faithful reconstruction is the goal — it deliberately keeps stale recorded `workdir` paths and
   uses the Nextflow *task* exit code for synthesized rows.

2. **`triage_failures.py`** walks failing task dirs and applies the prioritized `SIGNATURES` regex
   table to each task's logs (`.command.err`, then `log.txt`, then `.command.out`), assigning a
   category + confidence + evidence line. It also writes a per-failure **digest** to
   `analysis/failure_excerpts/` (test.qsub source + `results.txt` + `.command.err` head +
   `log.txt` tail) that Claude reads to adjudicate ambiguous cases.

3. **Claude adjudicates** the `unknown` / low-confidence / crash-contaminated rows by reading the
   digests, then edits `failure_analysis.csv` in place (`method=claude`). Re-running
   `triage_failures.py` **overwrites** that CSV and reverts those edits — always finish triage
   before adjudicating.

4. **`render_summary.py`** turns the (adjudicated) CSV into `failure_summary.md`. All counts come
   from the CSV; nothing is hard-coded.

### `pipeline-failure-analysis` (two scripts)

1. **`diagnose_pipeline.py`** streams `.nextflow.log` (identified via `--log`/`--session` or the
   active log), parses the run header + `.nextflow/history` (correlate on the UUID, col 6 — the short
   id repeats across reruns), classifies the termination cause with its prioritized `SIGNATURES`
   table, and buckets every task into a `phase` from its **last** `TaskHandler` status (COMPLETED
   exit 0 / ignored_error / running_at_termination / aborted / submitted_never_completed). It writes
   `pipeline_verdict.csv` (one row, `method=regex`), `pipeline_events.csv`, and digests
   (`run_header.txt`, `termination_tail.txt`, one per suspect task dir).
2. **Claude adjudicates** the single verdict row (confirm the cause, interpret the log tail, set
   `method=claude`, fill `interpretation`) — re-running the script overwrites it, same footgun as the
   test skill.
3. **`render_pipeline_summary.py`** turns the adjudicated CSVs into `pipeline_summary.md`.

## Domain model that drives classification (read before touching signatures)

- A test's pass/fail comes from its `test.qsub`: `module load`, then a series of commands each
  echoing `Passed`/`Error` by exit code into `results.txt`. **The Nth `Error` in `results.txt`
  corresponds to the Nth command in `test.qsub`** — so the test.qsub tells you which command failed
  and whether a "missing file" is the test's own input vs. a real dependency.
- Classification is done **per the test.qsub's own logic**: only what makes it emit `Error` counts;
  warnings and known incidental noise are ignored. Prefer the *first/root* error in a cascade
  (a missing `.so` causing a later `ModuleNotFoundError` → `missing_shared_library`, not
  `python_exception`).
- `resource_fork_exhaustion` (`fork`/`pthread_create` → `Resource temporarily unavailable`) is the
  per-user process limit (`ulimit -u` / `RLIMIT_NPROC`) being exhausted — typically from
  `--executor local` on a login node — **not a package defect**. `INCOMPLETE` tasks are aborted
  re-runs, not diagnosable failures. Both should be re-run on a compute node before being treated as
  failures.

## Editing a signature table

Both skills carry a priority-ordered `SIGNATURES` list where **first non-excluded match wins**, with
entries `(category, confidence, include_regex, exclude_regex_or_None)`. The ordering principle is the
same in both: genuine/specific root causes rank above symptom-like categories that a crash/abort can
induce, so a real cause is preferred and artifacts aren't mislabeled. Use `exclude_regex` for known
noise the include would otherwise catch — e.g. in `triage_failures.py`, `out_of_memory` excludes the
JVM `unable to create native thread` string (really fork exhaustion); in `diagnose_pipeline.py`,
`executor_error` excludes `error=11` (really `driver_fork_exhaustion`).

**When you change signatures or categories, keep the table and its docs in sync — per skill:**

- `pkgtest-failure-analysis`: `SIGNATURES` in `triage_failures.py` ↔ `references/categories.md` ↔ the
  `DESC` map in `render_summary.py`. Its verified noise-exclusion rules (Xvfb kill, `AVX`-in-paths,
  echoed `conda activate`, `/etc/pki` & `/boot/lost+found` permission noise) exist because they were
  false positives — don't drop them without cause.
- `pipeline-failure-analysis`: `SIGNATURES` in `diagnose_pipeline.py` ↔ `references/termination-causes.md`
  ↔ the `DESC` map in `render_pipeline_summary.py`. Its noise rules (the abort's own kill sweep, the
  trailing LevelDB/`collectFile` teardown NPE, `Goodbye`-prints-on-abort, `peakMemory=0` on local) are
  likewise verified — a task-level non-zero `exit` is an *ignored per-test* failure, never a pipeline
  cause. `completed_clean` and `still_running_or_truncated` are compound conditions handled after the
  regex loop, not single-regex rows.
