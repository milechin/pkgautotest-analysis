#!/usr/bin/env python3
"""Render failure_summary.md from failure_analysis.csv.

Part of the `pkgtest-failure-analysis` skill. Deterministically turns the per-test
`failure_analysis.csv` into a grouped, human-readable `failure_summary.md`: a category
count table, then per-module bullets (module/version, installer, the classifying evidence
line, and the full path to the log file it came from), plus a "Needs review" section for
`unknown` rows. All counts are computed from the CSV, not hard-coded.

Pure Python 3 standard library (3.6+); no third-party dependencies.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# One-line meaning per category (most specific/genuine first, then symptom-like, then unknown).
DESC = {
    "module_load_failed":
        "`module load` itself failed (Lmod error, unknown module, or module conflict).",
    "missing_shared_library":
        "A required shared library (.so) could not be found/resolved, or a GLIBC version was too "
        "old — the module's dependency stack is broken.",
    "linker_compile_failure":
        "A test that compiles/links a small program failed at build (undefined reference, missing "
        "header).",
    "conda_env_error":
        "A conda environment could not be activated/resolved.",
    "network_download_error":
        "The test could not reach or download from the network.",
    "cpu_illegal_instruction":
        "The binary used a CPU instruction not available on the test node.",
    "resource_fork_exhaustion":
        "Process/thread creation failed with `Resource temporarily unavailable` (EAGAIN) — the "
        "per-user process limit (`ulimit -u` / `RLIMIT_NPROC`) was exhausted, typically from "
        "running many tests concurrently with `--executor local` on a login node (compute nodes "
        "have `-u unlimited`). NOT a package defect — re-run on a compute node or throttle "
        "concurrency.",
    "out_of_memory":
        "Genuine out-of-memory (distinct from the process-limit thread-creation failures).",
    "killed_or_timeout":
        "The process was killed or hit a time limit (excludes the pipeline's own Xvfb kill).",
    "segfault_crash":
        "The program crashed (segfault / abort / core dumped).",
    "permission_denied":
        "A permission / 'operation not permitted' error blocked the test.",
    "command_not_found":
        "A required executable was not found.",
    "python_exception":
        "A Python traceback / ModuleNotFoundError not caused by a missing `.so`.",
    "syntax_error":
        "A syntax error in the test script or program source.",
    "disk_quota":
        "Out of disk space or over quota.",
    "missing_file_or_input":
        "A missing/stale file, expected output not produced, stale lock, or bad bind mount "
        "(low confidence — verify against the test.qsub).",
    "stray_stdout":
        "The test failed only because something other than `Passed`/`Error` reached stdout — "
        "PkgAutoTest requires stdout to contain nothing else, so there is no `Error` here at "
        "all. A test-harness defect (the script's own messages belong on stderr or in "
        "`$LOG_FILE`), not a package defect.",
    "test_template_not_implemented":
        "No test has been written for this package — `test.qsub` is still the unmodified "
        "skeleton (`module load xyz/0.0.0`, `xyz_command`). NOT a broken modulefile.",
    "mpi_interconnect_error":
        "An InfiniBand/UCX fabric fault aborted the MPI job (`Transport retry count exceeded`) "
        "— infrastructure, not a package defect. Re-run; if it recurs on the same node, report "
        "that node to the fabric admins.",
    "transient_io_error":
        "A file the test could not open is present and readable at analysis time — the failure "
        "was transient (filesystem hiccup, or a container missing the right `--bind`) rather "
        "than a packaging error. Re-run.",
    "unknown":
        "No signature matched and the logs gave no determinable cause; needs human review "
        "(see the digest).",
}


def describe(category):
    """Description for a category. Categories Claude adds during adjudication are not in
    DESC, so fall back to the slug read as words rather than rendering an empty cell."""
    if category in DESC:
        return DESC[category]
    return "%s (category added during adjudication; see `references/categories.md`)." % \
        category.replace("_", " ").capitalize()


CAVEAT = (
    "> **Interpretation:** `resource_fork_exhaustion` is the per-user process-limit artifact "
    "(`ulimit -u` / `RLIMIT_NPROC`), which is hit when many tests run concurrently under "
    "`--executor local` on a login node — **not** a package defect. Those tasks, and any "
    "`INCOMPLETE` tasks (aborted before finishing), should be **re-run on a compute node** "
    "(`sge` executor, `-u unlimited`) before being treated as failures.\n"
)


def find_run_root(explicit=None):
    if explicit:
        return Path(explicit).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        s = start.resolve()
        for cand in (s, *s.parents):
            # Any top-level *.csv: the input CSV takes the launcher's name, not always out.csv.
            if (cand / "work").is_dir() and any(cand.glob("*.csv")):
                return cand
    return Path.cwd()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", default=None,
                   help="Pipeline run root (auto-detected from CWD if omitted)")
    p.add_argument("--analysis-dir", default=None,
                   help="Where failure_analysis.csv lives / summary is written "
                        "(default: <run-dir>/analysis)")
    p.add_argument("--input", default=None, help="Path to failure_analysis.csv")
    p.add_argument("--output", default=None, help="Path to failure_summary.md")
    a = p.parse_args(argv)
    run_root = find_run_root(a.run_dir)
    adir = Path(a.analysis_dir) if a.analysis_dir else run_root / "analysis"
    a.input = Path(a.input) if a.input else adir / "failure_analysis.csv"
    a.output = Path(a.output) if a.output else adir / "failure_summary.md"
    return a


def safe_name(module, qsub_file):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_"
                   for ch in "%s__%s" % (module, qsub_file))


def main(argv=None):
    a = parse_args(argv)
    if not a.input.is_file():
        sys.exit("error: %s not found (run triage_failures.py first)" % a.input)

    rows = list(csv.DictReader(open(a.input, newline="")))
    n_failed = sum(1 for r in rows if r["test_result"] == "FAILED")
    n_incomplete = sum(1 for r in rows if r["test_result"] == "INCOMPLETE")
    by_cat = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)
    order = sorted(by_cat, key=lambda c: (c == "unknown", -len(by_cat[c]), c))

    out = []
    out.append("# PkgAutoTest Failure Analysis\n")
    out.append("Classification of **why** each failing test failed, judged by the test.qsub "
               "logic. Produced by the `pkgtest-failure-analysis` skill (`triage_failures.py` "
               "first pass + Claude adjudication of ambiguous cases). Per-test detail is in "
               "`failure_analysis.csv`; raw evidence digests are in `failure_excerpts/`.\n")
    out.append("**Scope:** %d failing tasks — %d FAILED + %d INCOMPLETE (INCOMPLETE = never "
               "wrote a result because the task was aborted).\n" % (len(rows), n_failed, n_incomplete))

    out.append("## Categories\n")
    out.append("| Category | Count | Meaning |")
    out.append("|---|---:|---|")
    for c in order:
        meaning = DESC.get(c, "").split(" — ")[0][:90]
        out.append("| `%s` | %d | %s |" % (c, len(by_cat[c]), meaning))
    out.append("")
    out.append(CAVEAT)

    out.append("## Failures by category\n")
    for c in order:
        items = sorted(by_cat[c], key=lambda r: r["module"].lower())
        out.append("### `%s`  (%d)\n" % (c, len(items)))
        out.append(describe(c) + "\n")
        for r in items:
            ev = r["evidence"].strip() or "(no evidence line; see digest)"
            inst = (", installer: %s" % r["installer"]) if r.get("installer") else ""
            out.append("- **%s** (%s%s) — %s" % (r["module"], r["test_result"], inst, ev))
            if r["evidence_path"]:
                out.append("  `%s`" % r["evidence_path"])
        out.append("")

    unk = sorted(by_cat.get("unknown", []), key=lambda r: r["module"].lower())
    if unk:
        out.append("## Needs review (unknown)\n")
        out.append("No signature matched and no clear cause in the logs (often headless "
                   "GUI/X-display apps, no-log cases, or version mismatches). Open the digest "
                   "to classify by hand:\n")
        for r in unk:
            inst = (" (installer: %s)" % r["installer"]) if r.get("installer") else ""
            out.append("- **%s**%s — harness: %s — `failure_excerpts/%s.txt`"
                       % (r["module"], inst, r["harness_verdict"] or "(none)",
                          safe_name(r["module"], r["qsub_file"])))
        out.append("")

    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text("\n".join(out) + "\n")
    print("Wrote %s  (%d tasks across %d categories)" % (a.output, len(rows), len(by_cat)),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
