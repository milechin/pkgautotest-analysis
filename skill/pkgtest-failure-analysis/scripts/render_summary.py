#!/usr/bin/env python3
"""Render failure_summary.md from failure_analysis.csv.

Part of the `pkgtest-failure-analysis` skill. Deterministically turns the per-test
`failure_analysis.csv` into a grouped, human-readable `failure_summary.md`: a category
count table (each category linking to its section), then per category a module index and one
block per module (result, installer, working directory, and a table of key excerpts from
`results.txt` / `.command.err` / `log.txt`). `unknown` rows use the same block as every other
category. `missing_shared_library` also gets a table of the libraries identified. It also appends the modules `find_qsub.py` skipped before the run (from
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
    "container_bind_mount_failure":
        "The container could not start because a bind-mount source (e.g. `/rproject`) does not "
        "exist on the node — an environment/site problem rather than a package defect.",
    "gui_display_failure":
        "A GUI test failed for lack of a usable display (`HeadlessException`, viewer/GUI launch "
        "failure, `togl` widget). `Waiting for Xvfb` alone is not evidence.",
    "output_comparison_failed":
        "The harness's output check (`File content check failed`) found the output differed from "
        "the reference; the logs do not say why.",
    "task_ended_early":
        "`results.txt` is empty: the task ended before any check reported. Symptom only — the "
        "cause (timeout, kill, early exit) is not recoverable from the task files.",
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
    a.run_root = run_root
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


# Error-line filter for the key-excerpt table. KEEP IN SYNC with the copy in triage_failures.py
# (the scripts are standalone). Keyword-based: an aid for finding evidence, never a classifier.
KEY_RE = re.compile(
    r"error|fail|fatal|exception|traceback|cannot|can't|not found|no such|denied|killed|abort|"
    r"segfault|invalid|timeout|timed out|refused|unable|illegal|munmap|core dumped|signal 11|"
    r"exists!", re.IGNORECASE)
WARN_RE = re.compile(
    r"warn|^INFO:|^\s+at |mendel_errors|[A-Za-z_]*errors?\s*=\s*[\d.]+|_CFLAGS|Werror|Error=[\d.]+",
    re.IGNORECASE)
WARN_BLOCK_START_RE = re.compile(r"^\++\s.*WARNINGS")   # e.g. AFNI's IMAGE SAVE SETUP WARNINGS
WARN_BLOCK_END_RE = re.compile(r"^\++\s*$")

LIB_RE = re.compile(r"(lib[\w.+-]+\.so[\w.]*):? cannot open shared object file")
GLIBC_RE = re.compile(r"`(GLIBC[\w.]*)' not found")
GLIBC_LABEL = "`libc.so.6` — %s not found"


def error_lines(text):
    """[(1-based line number, line)] of error-related lines (KEY and not WARN), skipping any
    `+++ ... WARNINGS` ... `++++` banner block. Walks lines by index: no zero-width re.split
    (a ValueError on the SCC default Python 3.6)."""
    found, in_block = [], False
    for n, ln in enumerate(text.splitlines(), 1):
        if in_block:
            in_block = not WARN_BLOCK_END_RE.match(ln)
            continue
        if WARN_BLOCK_START_RE.match(ln):
            in_block = True
            continue
        if KEY_RE.search(ln) and not WARN_RE.search(ln):
            found.append((n, ln.strip()))
    return found


def md_cell(text, width=160):
    """Make text safe for a Markdown table cell."""
    return text[:width].replace("|", "\\|").replace("`", "'")


def local_taskdir(workdir, run_root):
    """The CSV records the official workdir; the run may since have moved. Re-root onto the
    local work/ dir when the recorded path is not there."""
    p = Path(workdir)
    if p.is_dir() or "/work/" not in workdir:
        return p
    return run_root / "work" / workdir.split("/work/", 1)[1]


def excerpt_cell(path, kind):
    """Key-excerpt cell for one log file: error lines (first 2 + last 2 past 4) as `L<n>: ...`."""
    try:
        text = open(str(path), errors="replace").read()
    except OSError:
        return "not present"
    if not text.strip():
        return "empty"
    found = error_lines(text)
    if not found:
        if kind == "err":
            n = sum(1 for ln in text.splitlines() if ln.strip())
            return "no error lines (%d lines of non-error output)" % n
        return "no error lines"
    if len(found) > 4:
        found = found[:2] + found[-2:]
    return "<br>".join("L%d: %s" % (n, md_cell(ln)) for n, ln in found)


def results_cell(path):
    try:
        text = open(str(path), errors="replace").read()
    except OSError:
        return "not present"
    words = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " → ".join(md_cell(w) for w in words) if words else "(empty — no Passed/Error written)"


def missing_libraries(rows, run_root):
    """{label: [module, ...]} for missing_shared_library rows, from each task's log.txt and
    .command.err. A `GLIBC_x not found` is a different failure (the library is there but too
    old), so it gets its own `libc.so.6 — GLIBC_x not found` label rather than a .so name."""
    libs = defaultdict(set)
    glibc_vers, glibc_mods = set(), set()
    for r in rows:
        td = local_taskdir(r["workdir"], run_root)
        text = ""
        for fname in ("log.txt", ".command.err"):
            try:
                text += open(str(td / fname), errors="replace").read() + "\n"
            except OSError:
                pass
        for lib in LIB_RE.findall(text):
            libs[("`%s`" % lib)].add(r["module"])
        for ver in GLIBC_RE.findall(text):
            glibc_vers.add(ver)
            glibc_mods.add(r["module"])
    if glibc_vers:
        libs[GLIBC_LABEL % " / ".join("`%s`" % v for v in sorted(glibc_vers))] = glibc_mods
    return {k: sorted(v, key=str.lower) for k, v in libs.items()}


def render_missing_libraries(libs):
    out = ["**Missing libraries identified:**\n", "| Library | Modules | Note |", "|---|---|---|"]
    for lib in sorted(libs, key=lambda k: (k.startswith("`libc.so.6`"), k.lower())):
        note = "bundled `libc.so.6` is older than the binary needs (not a missing file)" \
            if lib.startswith("`libc.so.6`") else ""
        out.append("| %s | %s | %s |" % (lib, ", ".join(libs[lib]), note))
    out.append("")
    return out


def mod_anchor(module):
    return "mod-" + re.sub(r"[^A-Za-z0-9._-]", "_", module)


def safe_name(module, qsub_file):
    return "".join(ch if ch.isalnum() or ch in "._-" else "_"
                   for ch in "%s__%s" % (module, qsub_file))


def main(argv=None):
    a = parse_args(argv)
    run_root = a.run_root
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
        meaning = describe(c).split(" — ")[0]
        if len(meaning) > 90:
            meaning = meaning[:90].rsplit(" ", 1)[0] + " …"
        out.append("| [`%s`](#cat-%s) | %d | %s |" % (c, c, len(by_cat[c]), meaning))
    out.append("")
    if "resource_fork_exhaustion" in by_cat or n_incomplete:
        out.append(CAVEAT)

    out.append("## Failures by category\n")
    for c in order:
        items = sorted(by_cat[c], key=lambda r: r["module"].lower())
        out.append('<a id="cat-%s"></a>' % c)
        out.append("### `%s`  (%d)\n" % (c, len(items)))
        out.append(describe(c) + "\n")
        if c == "missing_shared_library":
            libs = missing_libraries(items, run_root)
            if libs:
                out += render_missing_libraries(libs)
        out.append(" · ".join("[%s](#%s)" % (r["module"], mod_anchor(r["module"])) for r in items))
        out.append("")
        for r in items:
            td = local_taskdir(r["workdir"], run_root)
            out.append('<a id="%s"></a>' % mod_anchor(r["module"]))
            out.append("#### %s\n" % r["module"])
            out.append("- **Test result:** %s" % r["test_result"])
            if r.get("installer"):
                out.append("- **Installer:** %s" % r["installer"])
            out.append("- **Working directory:** `%s`\n" % r["workdir"])
            out.append("| Log file | Key excerpt |")
            out.append("|---|---|")
            out.append("| `results.txt` | %s |" % results_cell(td / "results.txt"))
            out.append("| `.command.err` | %s |" % excerpt_cell(td / ".command.err", "err"))
            out.append("| `log.txt` | %s |" % excerpt_cell(td / "log.txt", "log"))
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
