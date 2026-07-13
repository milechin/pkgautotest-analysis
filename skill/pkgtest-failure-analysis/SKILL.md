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
root (the dir containing `out.csv` and `work/`) from the current directory. See
`references/categories.md` for the taxonomy, signatures, and noise rules, and
`references/run-layout.md` for the run directory structure.

Run the commands below **from the run directory**. `$SK` is this skill's `scripts/` dir; for a
project-level install that is `.claude/skills/pkgtest-failure-analysis/scripts` (adjust if the
skill is installed elsewhere). All scripts are Python 3.6+, standard library only.

## How a test's pass/fail works (essential context)

Each module's `test.qsub` first does `module load`, then runs a sequence of commands, each echoing
`Passed` or `Error` to stdout by exit code. That stdout becomes `results.txt`; detailed command
output goes to `log.txt`. The pipeline marks the test FAILED if any `Error` appears. **The Nth
`Error` in `results.txt` corresponds to the Nth command in `test.qsub`** — so reading the test.qsub
tells you exactly which command failed and whether a "missing file" is the test's own input vs. a
real dependency. `INCOMPLETE` tasks (no `test_metrics.csv`) never wrote a result — the task was
aborted before finishing (killed, timed out, or the run was interrupted).

## Workflow

1. **Rebuild the report if needed.** If `analysis/report_out.csv` is absent, run
   `python3 "$SK/rebuild_report.py"` (`module load python3` first only if `python3` is unavailable).

2. **First-pass triage.** Run:
   ```bash
   python3 "$SK/triage_failures.py"
   ```
   This writes `analysis/failure_analysis.csv` (columns: `module, version, installer, qsub_file,
   test_result, category, confidence, method, harness_verdict, evidence, evidence_path, workdir`),
   one compact digest per failure in `analysis/failure_excerpts/`, and prints a category tally +
   the count needing review. `method=regex` marks rows classified by the signature table.

3. **Adjudicate the uncertain rows (Claude judgment).** For each row where `category=unknown`,
   `confidence` is `low`/`none`, OR `category=resource_fork_exhaustion` **and** the digest also
   shows an independent genuine error, open
   `analysis/failure_excerpts/<module>__<qsub_file>.txt`. It contains the **test.qsub**,
   `results.txt` (Passed/Error sequence), the `.command.err` head, and the `log.txt` tail. Using
   `references/categories.md`:
   - Map the failing `Error`(s) to their test.qsub command(s).
   - Determine the root cause **per the test.qsub logic**; ignore warnings and the documented noise.
   - Update that row in `analysis/failure_analysis.csv` in place: set `category`, `confidence`,
     `method=claude`, and set `evidence` + `evidence_path` to the specific line and its file
     (match the path style already in the CSV).
   - If a genuine, recurring pattern has no matching signature, add it to `SIGNATURES` in
     `scripts/triage_failures.py` (keep priority order: specific/genuine above symptom-like).
   Leave a row `unknown` only if the logs genuinely lack a determinable cause.

4. **Render the summary.** Run `python3 "$SK/render_summary.py"` to (re)generate
   `analysis/failure_summary.md` from the (now-adjudicated) CSV — a category count table, per-module
   bullets (module/version, installer, evidence, full `evidence_path`), and a "Needs review"
   section. Add any run-specific narrative afterward if useful.

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
- `resource_fork_exhaustion` is the per-user process limit (`ulimit -u` / `RLIMIT_NPROC`) being
  exhausted — usually from running many tests with `--executor local` on a login node — not a
  package defect; those are re-runs on a compute node.
