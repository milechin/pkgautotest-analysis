---
name: pkgtest-failure-analysis
description: >-
  Analyze/triage a PkgAutoTest Nextflow run to classify WHY each module test failed (missing
  shared library, module-load failure, permission issue, OOM, missing CPU instruction,
  network/download failure, conda-env problem, killed/timeout, segfault, syntax error, process
  (ulimit) exhaustion, ...). Use when the user asks why module tests failed, wants a failure
  summary, or wants to triage a PkgAutoTest run directory. Hybrid: deterministic regex first pass
  + Claude judgment on the ambiguous cases.
---

# PkgAutoTest failure analysis

Classifies the root cause of each failed test in a PkgAutoTest run, judged **by the test.qsub's
own logic** — ignoring warnings and known incidental noise. Outputs go to an **`analysis/`** folder
in the run directory: `analysis/failure_analysis.csv` (per-test) and `analysis/failure_summary.md`
(grouped, human-readable). The bundled scripts (in this skill's `scripts/`) auto-detect the run
root (the dir containing `work/` and the input CSV — usually `out.csv`, but it takes the launcher's
name, e.g. `modules_crontab.csv`) from the current directory. See
`references/categories.md` for the taxonomy, signatures, and noise rules,
`references/harness-contract.md` for what the harness requires of a `test.qsub` (the three-part pass
criterion, the assertion helpers behind each `harness_verdict` phrase, and why a task may never have
run), and `references/run-layout.md` for the run directory structure.

Run the commands below **from the run directory**. `$SK` is this skill's `scripts/` dir; for a
project-level install that is `.claude/skills/pkgtest-failure-analysis/scripts` (adjust if the
skill is installed elsewhere). All scripts are Python 3.6+, standard library only.

## How a test's pass/fail works (essential context)

Each module's `test.qsub` first does `module load`, then runs a sequence of commands, each echoing
`Passed` or `Error` to stdout by exit code. That stdout becomes `results.txt`; detailed command
output goes to `log.txt`. A test `PASSED` only if **all three** hold: at least one `Passed`, **no
other line on stdout**, and exit code 0 — so an `Error` is the usual cause of failure but not the
only one (a stray stdout line fails a test by itself → `stray_stdout`). **The Nth
`Error` in `results.txt` corresponds to the Nth command in `test.qsub`** — so reading the test.qsub
tells you exactly which command failed and whether a "missing file" is the test's own input vs. a
real dependency. `INCOMPLETE` tasks (no `test_metrics.csv`) never wrote a result — the task was
aborted before finishing (killed, timed out, or the run was interrupted).

## Workflow

1. **Rebuild the report if needed.** The pipeline's report is named after the input CSV:
   `report_<input>.csv` (so `out.csv` → `report_out.csv`, but a run launched with
   `modules_crontab.csv` yields `report_modules_crontab.csv`). If neither a top-level
   `report_*.csv` nor `analysis/report_*.csv` exists, run `python3 "$SK/rebuild_report.py"`
   (`module load python3` first only if `python3` is unavailable); it detects the input CSV and
   writes `analysis/report_<input>.csv`. Do **not** rebuild just because `report_out.csv` is
   absent — check for the run's actual name first.

2. **First-pass triage.** Run:
   ```bash
   python3 "$SK/triage_failures.py"
   ```
   This writes `analysis/failure_analysis.csv` (columns: `installer, module, version, qsub_file,
   test_result, category, confidence, method, harness_verdict, evidence, evidence_path, workdir`),
   one compact digest per failure in `analysis/failure_excerpts/`, and prints a category tally +
   the count needing review. `method=regex` marks rows classified by the signature table. For an
   `unknown` row, `evidence` holds the first error-looking line of `.command.err`/`log.txt` as a
   *lead* only — a keyword match, not a diagnosis.

3. **Adjudicate the uncertain rows (Claude judgment).** For each row where `category=unknown`,
   `confidence` is `low`/`none`, OR `category=resource_fork_exhaustion` **and** the digest also
   shows an independent genuine error, open
   `analysis/failure_excerpts/<module>__<qsub_file>.txt`. It contains the **test.qsub**,
   `results.txt` (Passed/Error sequence), the `.command.err` head, and the `log.txt` tail. Using
   `references/categories.md`:
   - Map the failing `Error`(s) to their test.qsub command(s). The digest's "Failing command(s)"
     block does this best-effort (Nth `Passed`/`Error` line ↔ Nth `echo Error` branch); check it
     against the test.qsub above it, since loops and helper functions defeat the parse.
   - Determine the root cause **per the test.qsub logic**; ignore warnings and the documented noise.
   - Update that row in `analysis/failure_analysis.csv` in place: set `category`, `confidence`,
     `method=claude`, and set `evidence` + `evidence_path` to the specific line and its file
     (match the path style already in the CSV).
   - If a genuine pattern has no matching signature, **do not edit the skill** — record it in
     `analysis/skill_improvement_suggestions.md` (step 5).
   Leave a row `unknown` only if the logs genuinely lack a determinable cause.

   CSV rows are keyed by `module` = `name/version` (e.g. `qgis/3.40.14`), **not** the bare name. A
   bulk edit keyed by bare name matches nothing and fails silently — have any snippet you write
   print how many rows it updated, and stop if that is 0.

4. **Render the summary.** Run `python3 "$SK/render_summary.py"` to (re)generate
   `analysis/failure_summary.md` from the (now-adjudicated) CSV — a category count table (each name
   links to its section), then per category a module index and one block per module (result,
   installer, working directory, and a table of key excerpts from `results.txt`, `.command.err` and
   `log.txt`; `unknown` rows use the same block). The excerpts are error-keyword lines for finding
   evidence, not a classifier. The `missing_shared_library` section also tabulates the libraries
   identified, with GLIBC-too-old reported separately from a missing `.so`. The
   `resource_fork_exhaustion` interpretation note appears only when it applies. It ends with a
   **"Not tested"** section listing the modules `find_qsub.py` skipped before the run (parsed
   from `<run>/skipped.log`: the blanket exclusion list plus modules with no `test.qsub`). Those
   never became tasks, so they are absent from every count — untested, not passing; report them that
   way. Pass `--skipped-log PATH` if the log is elsewhere, or `--no-skipped` to omit the section.
   Add any run-specific narrative afterward if useful.

5. **Propose skill improvements (do not implement them).** If the pass revealed gaps in the skill
   itself — a missing category, a mis-ordered or over-broad signature, undocumented noise, a
   thin digest, a doc/script bug — write them to `analysis/skill_improvement_suggestions.md`
   instead of changing any file under `.claude/skills/`. See "Proposing skill changes" below.

## Proposing skill changes (never edit the skill in place)

The skill's scripts, references, and this file are **read-only from a run directory.** They are a
copy installed by [pkgautotest-analysis](https://github.com/milechin/pkgautotest-analysis)
(`install.sh`); an edit here is invisible to the maintainer, is lost on the next install, and
silently makes this run's results non-reproducible against every other run. So: change nothing
under `.claude/skills/` — propose instead.

Append to (or create) **`analysis/skill_improvement_suggestions.md`**, written so it can be pasted
straight into a GitHub issue on that repo, or handed to Claude Code in a checkout of it as the
implementation brief. Keep it a *proposal*: state the evidence and the intended rule, and let the
maintainer decide.

Structure it as:

- **A title line** naming the skill, and one paragraph of context: which run (absolute path), how
  many tests/failures, and how many rows the first pass got wrong or left `unknown` — the miss rate
  is what justifies the change.
- **One `##` section per suggestion**, ordered by value, each with: what is wrong today and the
  wrong conclusion it produces; the concrete evidence from this run (module/version + the actual log
  line or test.qsub line, and the digest/task path); the proposed rule — for a new category, its
  name, priority position *relative to the existing ones* and why (specific/genuine above
  symptom-like), a tight include regex, and an exclude regex for noise it would otherwise catch;
  and every file that would have to change to keep things in sync (`SIGNATURES` in
  `scripts/triage_failures.py` ↔ `references/categories.md` ↔ the `DESC` map in
  `scripts/render_summary.py`).
- **A "no change needed" section** for rows the first pass got *right*, so the maintainer does not
  re-litigate them, plus any purely-cosmetic evidence-string nits.
- **A closing priority ordering**, so a partial implementation still picks up the highest-value items.

Distinguish clearly between a rule the script can decide deterministically and one that needs
Claude's judgment — the latter belongs in `references/categories.md` as adjudication guidance, not
in the regex table. If a suggestion needs a signature you cannot verify from this run alone, say so
rather than proposing a broad regex.

Mention the file by path when you report back, so the user can open it, file it, or discard it.

## Notes
- Re-running `triage_failures.py` overwrites `failure_analysis.csv` (reverting Claude edits in
  step 3) and regenerates digests. Do the judgment pass after the final triage run, then render.
- Default scope is FAILED + INCOMPLETE; pass `--only-failed` to skip INCOMPLETE.
- `workdir`/`evidence_path` are written under the run root auto-detected from a `test_metrics.csv`
  `workdir` column — this matters when a run was executed elsewhere and later moved (the recorded
  path is preserved). Pass `--official-prefix PATH` to set it explicitly, or `--local-paths` to use
  the current on-disk location.
- test.qsub is read from its permanent source (`/share/pkg.8/.../test/test.qsub`, i.e. `out.csv`'s
  `test_path`), so analysis works even if the per-task copied `test/` dirs were deleted.
- Three categories mean "not a package defect, no fix needed here": `test_template_not_implemented`
  (no test written yet — the test.qsub is still the `xyz/0.0.0` skeleton), `mpi_interconnect_error`
  (an InfiniBand/UCX fabric fault), and `transient_io_error` (a "missing" `/share/pkg` file that is
  present and readable at analysis time). Report them as such rather than as broken packages.
- `resource_fork_exhaustion` is the per-user process limit (`ulimit -u` / `RLIMIT_NPROC`) being
  exhausted — usually from running many tests with `--executor local` on a login node — not a
  package defect; those are re-runs on a compute node.
