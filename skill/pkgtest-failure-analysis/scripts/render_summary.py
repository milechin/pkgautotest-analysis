#!/usr/bin/env python3
"""Render failure_summary.md from failure_analysis.csv.

Part of the `pkgtest-failure-analysis` skill. Deterministically turns the per-test
`failure_analysis.csv` into a grouped, human-readable `failure_summary.md`: a category
count table, then per-module bullets (module/version, installer, the classifying evidence
line, and the full path to the log file it came from), plus a "Needs review" section for
`unknown` rows. It also appends the modules `find_qsub.py` skipped before the run (from
`skipped.log`), which are absent from the run rather than passing. All counts are computed
from the CSV and that log, not hard-coded.

Pure Python 3 standard library (3.6+); no third-party dependencies.
"""

import argparse
import csv
import re
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
    p.add_argument("--skipped-log", default=None,
                   help="find_qsub.py's skipped.log listing modules never tested "
                        "(default: <run-dir>/skipped.log; omitted from the summary if absent)")
    p.add_argument("--no-skipped", action="store_true",
                   help="Do not append the 'Not tested' section.")
    a = p.parse_args(argv)
    run_root = find_run_root(a.run_dir)
    adir = Path(a.analysis_dir) if a.analysis_dir else run_root / "analysis"
    a.input = Path(a.input) if a.input else adir / "failure_analysis.csv"
    a.output = Path(a.output) if a.output else adir / "failure_summary.md"
    a.skipped_log = Path(a.skipped_log) if a.skipped_log else run_root / "skipped.log"
    return a


def parse_skipped_log(path):
    """Parse find_qsub.py's skipped.log into [(heading, [entry, ...]), ...].

    The file is a series of `-----`-separated blocks, each opening with a heading line
    ending in ':' followed by one entry per line -- either a bare module name (the
    blanket exclusion list) or `module/version,<pkg_dir>` (no test.qsub found). Parsed
    generically so headings find_qsub.py adds later still render."""
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return []
    blocks = []
    for chunk in re.split(r"^-{3,}\s*$", text, flags=re.MULTILINE):
        lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
        if not lines:
            continue
        heading = lines[0].rstrip(":") if lines[0].endswith(":") else "(unlabeled)"
        entries = lines[1:] if lines[0].endswith(":") else lines
        if entries:
            blocks.append((heading, entries))
    return blocks


def render_skipped(blocks, has_template_rows):
    """Markdown for the modules find_qsub.py never handed to the pipeline."""
    out = ["## Not tested (skipped before the run)\n"]
    out.append("Listed in `skipped.log` by `find_qsub.py`, so these modules never became pipeline "
               "tasks and appear nowhere in the counts above — they are **absent** from the run, "
               "not passing. Most are hard to test automatically (GUI apps, license-bound or "
               "interactive software, meta-packages). An entry without a version is a blanket "
               "exclusion covering every version of that module.\n")
    for heading, entries in blocks:
        out.append("### %s  (%d)\n" % (heading, len(entries)))
        for e in entries:
            mod, _, where = e.partition(",")
            out.append("- `%s`%s" % (mod.strip(), ("  — %s" % where.strip()) if where.strip() else ""))
        out.append("")
    if has_template_rows:
        out.append("Related: `test_template_not_implemented` above is the same gap one step later — "
                   "a `test.qsub` exists, so the module *was* tested, but the file is still the "
                   "unmodified skeleton.\n")
    return out


def safe_name(module, qsub_file):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_"
                   for ch in "%s__%s" % (module, qsub_file))


def main(argv=None):
    a = parse_args(argv)
    if not a.input.is_file():
        sys.exit("error: %s not found (run triage_failures.py first)" % a.input)

    rows = list(csv.DictReader(open(a.input, newline="")))
    skipped = [] if a.no_skipped else parse_skipped_log(a.skipped_log)
    n_skipped = sum(len(e) for _, e in skipped)
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
               "wrote a result because the task was aborted).%s\n"
               % (len(rows), n_failed, n_incomplete,
                  " A further %d entries were never tested at all (`skipped.log`) — see "
                  "\"Not tested\" at the end." % n_skipped if n_skipped else ""))

    out.append("## Categories\n")
    out.append("| Category | Count | Meaning |")
    out.append("|---|---:|---|")
    for c in order:
        meaning = describe(c).split(" — ")[0][:90]
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

    if skipped:
        out += render_skipped(skipped, "test_template_not_implemented" in by_cat)

    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text("\n".join(out) + "\n")
    print("Wrote %s  (%d tasks across %d categories%s)"
          % (a.output, len(rows), len(by_cat),
             ", %d skipped modules" % n_skipped if n_skipped else ""), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
