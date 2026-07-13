# pkgautotest-analysis

Claude Code tooling to analyze the results of a [PkgAutoTest](https://github.com/bu-rcs/PkgAutoTest)
Nextflow run: reconstruct the aggregate report and classify **why** each module test failed
(missing shared library, module-load failure, permission issue, process/`ulimit` exhaustion,
missing CPU instruction, network/download failure, conda-env problem, killed/timeout, segfault,
syntax error, …).

It ships as a **Claude Code skill** with bundled Python scripts. You install it into a run
directory; then, inside Claude Code, you ask it to "analyze this PkgAutoTest run" and it produces:

- `analysis/report_out.csv` — per-task PASSED / FAILED / INCOMPLETE / NOT_PROCESSED report for
  every input task (reconstructed even if the run crashed before writing its own report).
- `analysis/failure_analysis.csv` — one row per failing task with a root-cause `category`,
  `confidence`, `evidence` line, and full `evidence_path` to the log that justifies it.
- `analysis/failure_summary.md` — the failures grouped by category, human-readable.
- `analysis/failure_excerpts/` — a compact evidence digest per failing task.

## How it works

A deterministic first pass (`triage_failures.py`) applies a prioritized regex signature table to
each failing task's logs and assigns a category + evidence. Claude then adjudicates the `unknown`
and ambiguous cases by reading the per-failure digests (which include the module's `test.qsub`),
and `render_summary.py` renders the grouped summary. The signature table encodes hard-won
noise-exclusion rules (e.g. the pipeline's own Xvfb kill, `AVX` strings in paths, echoed
`conda activate` lines) so warnings and incidental output are not mistaken for causes.

## Requirements

- **Python 3.6+**, standard library only — **no `pip install`, no third-party packages.**
  Runs on the SCC default `python3` (no `module load` required).
- Claude Code, to drive the skill (the scripts can also be run standalone).

## Install (project-level, per run)

Clone once:

```bash
git clone <repo-url> ~/pkgautotest-analysis
```

After a run finishes, install the skill into that run's directory (the one containing `out.csv`
and `work/`):

```bash
~/pkgautotest-analysis/install.sh /path/to/run    # or run with no arg from inside the run dir
```

This copies the skill (with its bundled scripts) to `<run>/.claude/skills/pkgtest-failure-analysis/`,
so the analysis travels with the run directory and any colleague who opens it in Claude Code gets
the skill. (Optionally also `cp ~/pkgautotest-analysis/templates/CLAUDE.md /path/to/run/CLAUDE.md`
to give the run dir a short guide.)

## Use

```bash
cd /path/to/run
claude          # then ask: "analyze this PkgAutoTest run"
```

Or run the scripts directly from the run directory (they auto-detect the run root):

```bash
SK=.claude/skills/pkgtest-failure-analysis/scripts
python3 "$SK/rebuild_report.py"
python3 "$SK/triage_failures.py"
python3 "$SK/render_summary.py"
```

## Repo layout

```
skill/pkgtest-failure-analysis/     # the Claude skill (this is what install.sh copies)
  SKILL.md                          # workflow + when-to-use
  references/categories.md          # failure taxonomy, signatures, noise-exclusion rules
  references/run-layout.md          # PkgAutoTest run directory structure
  scripts/rebuild_report.py         # rebuild report_out.csv
  scripts/triage_failures.py        # regex first-pass classifier + digest generator
  scripts/render_summary.py         # failure_analysis.csv -> failure_summary.md
install.sh                          # install the skill into a run directory
templates/CLAUDE.md                 # optional guide to drop into a run directory
```

## Notes

- `resource_fork_exhaustion` failures (`fork`/`pthread_create` → `Resource temporarily unavailable`)
  are the **per-user process limit** (`ulimit -u` / `RLIMIT_NPROC`) being exhausted — typically
  from running many tests concurrently with `nextflow --executor local` on a **login node**
  (compute nodes have `-u unlimited`). They are not package defects: re-run on a compute node
  (`sge` executor) or throttle concurrency (`executor.queueSize` / `process.maxForks`,
  `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`).
- `INCOMPLETE` tasks never wrote a result (aborted before finishing) — they are re-runs, not
  diagnosable failures.
- Re-running `triage_failures.py` overwrites `failure_analysis.csv`, reverting Claude's
  adjudications; do the judgment pass after the final triage run, then render the summary.
