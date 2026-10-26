#!/usr/bin/env python3
"""Deterministic first-pass classifier for PkgAutoTest failures + digest generator.

Part of the `pkgtest-failure-analysis` skill. This script does the fast, reproducible
first pass: for every failing task it applies a prioritized signature table to the logs,
assigns a root-cause category with the evidence line + full log path, and writes a compact
per-failure "digest" (test.qsub + results.txt + log excerpts) that a Claude session then
reads to adjudicate the `unknown`, low-confidence, and crash-contaminated cases.

It classifies WHY a test failed based on the test.qsub logic, deliberately ignoring
warnings and known incidental noise (Xvfb kills, AVX-in-paths, echoed `conda activate`
lines, shared-scratch cleanup permission errors, etc.). See
`.claude/skills/pkgtest-failure-analysis/references/categories.md` for the full taxonomy.

Pure Python 3 standard library; no dependencies. Standalone (does not import rebuild_report.py).
"""

import argparse
import csv
import os
import re
import sys
from pathlib import Path

MODULE_RE = re.compile(r"^\s*echo MODULE=(.+?)\s*$", re.MULTILINE)
TEST_DIR_RE = re.compile(r"TEST_DIR=`dirname\s+(\S+)`")

# Lines that are never evidence: warnings and toil info/warn log levels.
NOISE_LINE_RE = re.compile(r"(?i)\bwarning\b|\[\s*[IW]\s*\]")

# Harness verdict phrases (the test.qsub-logic-level reason), scanned in .command.err.
HARNESS_RE = re.compile(
    r"(Executable failed to run|Keyword check failed|File existence check failed|"
    r"File content check failed|Numeric check failed)", re.IGNORECASE)

# Prioritized signature table: (category, confidence, include_regex, exclude_regex_or_None).
# Most-specific / most-genuine first. First category with a non-excluded match wins.
# Ordering rationale: genuine build/dependency causes rank above resource_fork_exhaustion,
# which ranks above symptom-like categories (oom/killed/segfault/generic-file) that the
# crash can induce -- so a real cause wins and crash symptoms are not mislabeled.
SIGNATURES = [
    ("module_load_failed", "high",
     r"Lmod has detected the following error|Unable to load module because of error|"
     r"module\(s\) are unknown|Cannot load module .* because these module", None),
    # Apptainer/Singularity could not start the container: a bind source (e.g. /rproject) is
    # absent on the node. Genuine infrastructure cause; outranks missing_file_or_input, which
    # would otherwise be the nearest bucket.
    ("container_bind_mount_failure", "high",
     r"mount source \S+ doesn't exist|container creation failed: mount hook", None),
    ("missing_shared_library", "high",
     r"error while loading shared libraries|cannot open shared object file|"
     r"ImportError: .*\.so[0-9.]*:|undefined symbol:|"
     r"GLIBC_[0-9.]+.? not found|: version .* not found \(required by", None),
    ("linker_compile_failure", "high",
     r"/usr/bin/ld:|undefined reference to|collect2: error|cannot find -l|"
     r"fatal error: .*\.h: No such file", None),
    ("conda_env_error", "high",
     r"CondaError|EnvironmentNameNotFound|Could not find conda environment|"
     r"PackagesNotFoundError|CommandNotFoundError.*conda|conda: command not found", None),
    ("network_download_error", "high",
     r"Could not resolve host|Connection (refused|timed out)|Failed to connect|"
     r"Temporary failure in name resolution|Network is unreachable|urlopen error|"
     r"SSLError|curl: \([0-9]+\)|wget:.*(unable|failed)", None),
    ("cpu_illegal_instruction", "high",
     r"Illegal instruction|illegal hardware instruction|SIGILL", None),
    # InfiniBand/UCX fabric fault: infrastructure, not a package defect. Must outrank
    # segfault_crash, which would otherwise catch the resulting MPI abort.
    ("mpi_interconnect_error", "high",
     r"Transport retry count exceeded|ib_mlx5_log\.c:[0-9]+|ibv_\w+ failed|UCX +ERROR", None),
    ("resource_fork_exhaustion", "medium",
     r"Resource temporarily unavailable|TermWidth\.lua.*index a nil|"
     r"unable to create native thread: possibly out of memory or process/resource limits|"
     r"Cannot create worker (GC )?thread|Out of system resources", None),
    ("out_of_memory", "medium",
     r"Out of memory|std::bad_alloc|Cannot allocate memory|MemoryError|OutOfMemoryError",
     r"unable to create native thread"),
    ("killed_or_timeout", "medium",
     r"[0-9]+ (Killed|Terminated)\b|DUE TO TIME LIMIT|timeout: sending",
     r"Xvfb"),
    ("segfault_crash", "medium",
     r"Segmentation fault|SIGSEGV|core dumped|Trace/breakpoint trap|Aborted \(core", None),
    ("permission_denied", "medium",
     r"Permission denied",
     r"/etc/pki|/boot/lost\+found"),
    ("command_not_found", "medium",
     r"command not found|/usr/bin/env: .*: No such file|: not found", None),
    ("python_exception", "medium",
     r"ModuleNotFoundError|Traceback \(most recent call last\)|ImportError", None),
    ("syntax_error", "medium",
     r"syntax error|SyntaxError|unexpected (end of file|token)|parse error near", None),
    ("disk_quota", "medium",
     r"No space left on device|Disk quota exceeded", None),
    ("missing_file_or_input", "low",
     r"No such file or directory|does not exist|cannot stat|cannot create directory.*File exists|"
     # A program's own open failure names the path but not errno; needed so the
     # transient_io_error downgrade below gets a chance to re-stat it.
     r"[Ff]ailed to open (file|input)|unable to open file",
     # The pipeline greps $LOG_FILE unconditionally; a test.qsub that delegates to a sibling
     # script may never write log.txt, so this line is harness noise, not the failure.
     r"grep: .*\blog\.txt: No such file"),
    # Symptom-like categories, last: they only name where the test gave up, so every genuine
    # cause above wins. "Waiting for Xvfb" alone is deliberately NOT a signature -- it is
    # printed on healthy GUI starts (qupath's real failure was an unrelated stale-output error).
    ("gui_display_failure", "medium",
     r"java\.awt\.HeadlessException|VisIt's viewer exited abnormally|GUI failed to run|"
     r"Couldn't configure togl widget", None),
    ("output_comparison_failed", "medium",
     r"File content check failed", None),
]
# Compile once.
SIGNATURES = [(c, conf, re.compile(inc, re.IGNORECASE),
               re.compile(exc, re.IGNORECASE) if exc else None)
              for (c, conf, inc, exc) in SIGNATURES]

# An unmodified test.qsub skeleton: the placeholder module `xyz/0.0.0` and/or the placeholder
# command `xyz_command`. Checked BEFORE the signature table, because the resulting
# `module(s) are unknown` would otherwise read as module_load_failed -- i.e. as a broken
# modulefile for a package that simply has no test written yet.
TEMPLATE_RE = re.compile(r"^\s*(module\s+load\s+.*\bxyz/0\.0\.0\b|.*\bxyz_command\b.*)\s*$",
                         re.MULTILINE)

# A path mentioned in an evidence line, used to re-stat "missing" package files (see
# maybe_transient_io). Only /share/pkg* paths are checked: those are the read-only package
# tree, so a file that is present and readable now was never actually missing.
SHARE_PATH_RE = re.compile(r"(/share/pkg[^\s'\"()]*)")

# Evidence sources, scanned in this order (stderr first: root errors surface there).
EVIDENCE_FILES = [".command.err", "log.txt", ".command.out"]

# Error-line filter, used to pick an `unknown` row's evidence line. KEEP IN SYNC with the copy in
# render_summary.py (the scripts are standalone, so it is duplicated). Keyword-based: it finds
# *candidate* evidence for a human, and never decides `category`.
KEY_RE = re.compile(
    r"error|fail|fatal|exception|traceback|cannot|can't|not found|no such|denied|killed|abort|"
    r"segfault|invalid|timeout|timed out|refused|unable|illegal|munmap|core dumped|signal 11|"
    r"exists!", re.IGNORECASE)
WARN_RE = re.compile(
    r"warn|^INFO:|^\s+at |mendel_errors|[A-Za-z_]*errors?\s*=\s*[\d.]+|_CFLAGS|Werror|Error=[\d.]+",
    re.IGNORECASE)
WARN_BLOCK_START_RE = re.compile(r"^\++\s.*WARNINGS")   # e.g. AFNI's IMAGE SAVE SETUP WARNINGS
WARN_BLOCK_END_RE = re.compile(r"^\++\s*$")


def error_lines(text):
    """[(1-based line number, line)] of error-related lines: KEY matches, WARN does not, and
    lines inside a `+++ ... WARNINGS` ... `++++` banner block are skipped."""
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


def first_error_line(task_dir):
    """(file name, line) of the first error-related line in .command.err, else log.txt."""
    for fname in (".command.err", "log.txt"):
        found = error_lines(read_text(task_dir / fname, ""))
        if found:
            return fname, found[0][1][:300]
    return "", ""


# installer leads: the first question about a failure is usually who owns the package.
# Consumers read this CSV by column name (csv.DictReader), never by position.
CSV_COLUMNS = ["installer", "module", "version", "qsub_file", "test_result", "category",
               "confidence", "method", "harness_verdict", "evidence", "evidence_path", "workdir"]


def find_run_root(explicit=None):
    """Return the PkgAutoTest run root (dir holding work/ and the input CSV).
    If --run-dir is given, use it. Otherwise search ancestors of the current
    working directory first (the intuitive case: run from inside the run dir),
    then ancestors of this script (so the installed <run>/.claude/skills/.../
    scripts/ layout also resolves). The input CSV is NOT always named out.csv --
    it takes the launcher's name (e.g. modules_crontab.csv) -- so any top-level
    *.csv alongside work/ identifies the root."""
    if explicit:
        return Path(explicit).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        s = start.resolve()
        for cand in (s, *s.parents):
            if (cand / "work").is_dir() and any(cand.glob("*.csv")):
                return cand
    return Path.cwd()


def detect_input_csv(run_dir):
    """The run's input CSV: <run>/out.csv, or whatever it was named instead.
    The pipeline names its outputs report_<input>.csv, so exclude those (and
    skipped.log-style leftovers) and require the input's own header column."""
    cands = [run_dir / "out.csv"] if (run_dir / "out.csv").is_file() else []
    cands += sorted(p for p in run_dir.glob("*.csv")
                    if not p.name.startswith("report_") and p.name != "out.csv")
    for p in cands:
        with open(p, newline="") as fh:
            header = fh.readline()
        if "module_name_version" in header:
            return p
    return run_dir / "out.csv"


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", type=Path, default=None,
                   help="Pipeline run root containing out.csv and work/ (auto-detected from CWD)")
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument("--input-csv", type=Path, default=None)
    p.add_argument("--output", type=Path, default=None,
                   help="default: <run-dir>/analysis/failure_analysis.csv")
    p.add_argument("--digests-dir", type=Path, default=None,
                   help="default: <run-dir>/analysis/failure_excerpts")
    p.add_argument("--only-failed", action="store_true",
                   help="Classify only FAILED tasks; skip INCOMPLETE ones.")
    p.add_argument("--official-prefix", default=None,
                   help="Emit workdir/evidence_path under this run root (the official/recorded "
                        "location) instead of the local one. Default: auto-detect from a "
                        "test_metrics.csv workdir column.")
    p.add_argument("--local-paths", action="store_true",
                   help="Keep the local (currently-relocated) paths; disable official-prefix rewrite.")
    a = p.parse_args(argv)
    a.run_dir = find_run_root(a.run_dir)
    a.work_dir = a.work_dir or a.run_dir / "work"
    a.input_csv = a.input_csv or detect_input_csv(a.run_dir)
    a.output = a.output or a.run_dir / "analysis" / "failure_analysis.csv"
    a.digests_dir = a.digests_dir or a.run_dir / "analysis" / "failure_excerpts"
    a.output.parent.mkdir(parents=True, exist_ok=True)
    return a


def read_text(path, default=""):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def load_out_csv(path):
    """module_name_version -> first matching row dict (for installer/category enrichment)."""
    by_mod = {}
    if not path.is_file():
        return by_mod
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh, skipinitialspace=True):
            by_mod.setdefault(row.get("module_name_version", ""), row)
    return by_mod


def read_metrics(path):
    """Return test_metrics.csv's data row as {column: value}, or None if there is no data row.

    Columns are looked up by header name: the column set has grown over time (`test_path` is
    now last, after `workdir`), so positions are not stable. If the header lacks the known
    names, fall back to the original layout (test_result 4th, workdir last)."""
    rows = [r for r in csv.reader(read_text(path).splitlines(), skipinitialspace=True) if r]
    if len(rows) < 2:
        return None
    header, row = [h.strip() for h in rows[0]], [v.strip() for v in rows[1]]
    if "test_result" in header and "workdir" in header:
        return dict(zip(header, row))
    return {"test_result": row[3] if len(row) > 3 else "", "workdir": row[-1]}


def detect_official_root(work_dir):
    """Infer the official/recorded run root from a test_metrics.csv workdir column
    (the run may have been relocated; the recorded path is the canonical one)."""
    for level1 in sorted(p for p in work_dir.iterdir() if p.is_dir()):
        for task_dir in level1.iterdir():
            m = task_dir / "test_metrics.csv"
            if m.is_file():
                rec = read_metrics(m)
                if rec and "/work/" in rec.get("workdir", ""):
                    return rec["workdir"].split("/work/")[0]
    return None


def parse_command_sh(task_dir):
    text = read_text(task_dir / ".command.sh", None)
    if text is None:
        return None, None
    m = MODULE_RE.search(text)
    t = TEST_DIR_RE.search(text)
    module = m.group(1) if m else None
    test_path = t.group(1) if t else None
    return module, test_path


def status_of(task_dir):
    """Return 'FAILED'/'PASSED' from test_metrics.csv, or 'INCOMPLETE' if none."""
    metrics = task_dir / "test_metrics.csv"
    if metrics.is_file():
        rec = read_metrics(metrics)
        return rec.get("test_result", "") if rec else ""  # malformed -> non-failing (skip)
    return "INCOMPLETE"


def iter_failing_dirs(work_dir, only_failed):
    for level1 in sorted(p for p in work_dir.iterdir() if p.is_dir()):
        for task_dir in sorted(p for p in level1.iterdir() if p.is_dir()):
            if not (task_dir / ".command.sh").is_file():
                continue  # stray dir
            st = status_of(task_dir)
            if st == "FAILED" or (st == "INCOMPLETE" and not only_failed):
                yield task_dir, st


def unimplemented_template(test_path):
    """If test.qsub is the unmodified skeleton, return its offending line, else None."""
    if not test_path:
        return None
    m = TEMPLATE_RE.search(read_text(Path(test_path), ""))
    return m.group(1).strip()[:300] if m else None


def stray_stdout(task_dir):
    """If the task failed only by printing extra lines to stdout, return that line.

    PkgAutoTest requires all three of: >=1 `Passed` in results.txt, *zero* other lines
    (results.txt IS the job's stdout), and exit 0. So a test can be FAILED with no `Error`
    at all -- rule 3 of the test.qsub contract broken. Reported only when there is no
    `Error`: if a check did fail, that failure is the cause and this is a side effect.
    See references/harness-contract.md."""
    text = read_text(task_dir / "results.txt", None)
    if text is None:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(ln == "Error" for ln in lines):
        return None
    stray = [ln for ln in lines if ln not in ("Passed", "Error")]
    return stray[0][:300] if stray else None


def maybe_transient_io(category, evidence):
    """Downgrade a 'missing' /share/pkg file that is present and readable right now.

    A package file under /share/pkg is not created by the test, so if it exists and is
    readable at analysis time it was never missing -- the open failed transiently (a GPFS
    hiccup, or a container started without the right `--bind`). Returns a possibly-rewritten
    (category, confidence, evidence)."""
    if category != "missing_file_or_input":
        return category, None, evidence
    for path in SHARE_PATH_RE.findall(evidence):
        if os.access(path, os.R_OK):
            return ("transient_io_error", "medium",
                    "%s  [file present and readable at analysis time]" % evidence)
    return category, None, evidence


def classify(task_dir, test_path=None, status="FAILED"):
    """Return (category, confidence, evidence_source, evidence_line)."""
    tmpl = unimplemented_template(test_path)
    if tmpl:
        # Empty source: the evidence is test.qsub itself, not a file in the task dir; main()
        # points evidence_path at the test.qsub source for this category.
        return "test_template_not_implemented", "high", "", tmpl

    # Read candidate lines per source, dropping noise.
    per_source = []
    for fname in EVIDENCE_FILES:
        text = read_text(task_dir / fname, None)
        if text is None:
            continue
        lines = [ln.rstrip("\n") for ln in text.splitlines()
                 if ln.strip() and not NOISE_LINE_RE.search(ln)]
        per_source.append((fname, lines))

    for category, confidence, inc_re, exc_re in SIGNATURES:
        for fname, lines in per_source:
            for ln in lines:
                if inc_re.search(ln) and not (exc_re and exc_re.search(ln)):
                    return category, confidence, fname, ln.strip()[:300]

    # Fallback, after the log signatures: the test failed with no `Error` at all, only an
    # extra line on stdout. Deliberately NOT checked before the table -- a crash can itself
    # be the stray line (a program writing to stdout instead of $LOG_FILE), and blaming the
    # harness would hide a genuine defect. FAILED only: an INCOMPLETE task never got a
    # verdict and its results.txt was cut off mid-run.
    if status == "FAILED":
        stray = stray_stdout(task_dir)
        if stray:
            return "stray_stdout", "high", "results.txt", stray
        # Symptom only: the task ended before any command wrote Passed/Error. The cause
        # (timeout, kill, early exit) is not recoverable from the task files.
        res = read_text(task_dir / "results.txt", None)
        if res is not None and not res.strip():
            return ("task_ended_early", "medium", "results.txt",
                    "results.txt is empty (no Passed/Error written)")
    # Still unknown: no category, but surface the first error-looking line as a lead.
    ev_src, ev_line = first_error_line(task_dir)
    return "unknown", "none", ev_src, ev_line


def harness_verdict(task_dir):
    text = read_text(task_dir / ".command.err", "")
    found = []
    for m in HARNESS_RE.finditer(text):
        v = m.group(1)
        if v not in found:
            found.append(v)
    return "; ".join(found)


ERR_ECHO_RE = re.compile(r"^\s*echo\s+[\"']?Error[\"']?\s*(#.*)?$")
IF_RE = re.compile(r"^\s*(if|elif)\b")
TEST_COND_RE = re.compile(r"^\s*(if|elif)\s+(!\s*)?(\[|test\b|\(\()")   # `if [ ... ]`: the command precedes


def failing_commands(qsub_text, results_text, limit=5):
    """Best-effort map of each `Error` in results.txt to the test.qsub command that emitted it.

    Assumes the usual shape: one `<command>` / `if [ $? -eq 0 ]; then echo Passed; else echo
    Error; fi` block per check, so the Nth Passed/Error line of results.txt belongs to the Nth
    `echo Error` branch. Loops, helper functions and conditional checks break that; the digest
    labels the output best-effort. Returns [(results_line_no, qsub_line_no, [command lines])]."""
    src = qsub_text.splitlines()
    branches = [i for i, ln in enumerate(src) if ERR_ECHO_RE.match(ln)]
    verdicts = [ln.strip() for ln in results_text.splitlines() if ln.strip() in ("Passed", "Error")]
    out = []
    for pos, v in enumerate(verdicts):
        if v != "Error" or pos >= len(branches):
            continue
        i = branches[pos]
        while i >= 0 and not IF_RE.match(src[i]):
            i -= 1
        if i < 0:
            continue
        if TEST_COND_RE.match(src[i]):           # command is on the line(s) before the `if`
            j = i - 1
            while j >= 0 and (not src[j].strip() or src[j].lstrip().startswith("#")):
                j -= 1
            end = j
            while j > 0 and src[j - 1].rstrip().endswith("\\"):
                j -= 1                            # include backslash-continued lines
            cmd = (src[j:end + 1] if end >= 0 else []) + [src[i]]   # keep the `if` for context
        else:                                     # `if <command>; then` — the if is the command
            j, cmd = i, [src[i]]
        if cmd:
            out.append((pos + 1, j + 1, [c.rstrip()[:200] for c in cmd[:4]]))
        if len(out) >= limit:
            break
    return out


def write_digest(digests_dir, module, qsub_file, task_dir, test_path, display_dir=None):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", f"{module}__{qsub_file}")
    out = digests_dir / f"{safe}.txt"
    parts = []
    parts.append(f"# MODULE: {module}\n# QSUB: {qsub_file}\n# TASK DIR: {display_dir or task_dir}\n")

    qsub_src = test_path or ""
    parts.append("=" * 70 + f"\n## test.qsub  ({qsub_src})\n" + "=" * 70)
    parts.append(read_text(Path(qsub_src), "<test.qsub not found at source path>")
                 if qsub_src else "<no test_path parsed from .command.sh>")

    parts.append("\n" + "=" * 70 + "\n## results.txt  (ordered Passed/Error; Nth Error = Nth test.qsub command)\n" + "=" * 70)
    parts.append(read_text(task_dir / "results.txt", "<empty>"))

    results_text = read_text(task_dir / "results.txt", "")
    parts.append("\n" + "=" * 70 + "\n## Failing command(s)  (BEST-EFFORT: Nth Passed/Error line of "
                 "results.txt <-> Nth `echo Error` branch of test.qsub; verify against the source "
                 "above)\n" + "=" * 70)
    if not results_text.strip():
        parts.append("<results.txt is empty: the task ended before any check reported>")
    else:
        fails = failing_commands(read_text(Path(qsub_src), "") if qsub_src else "", results_text)
        if fails:
            for res_no, qsub_no, cmd in fails:
                parts.append("results.txt line %d = Error  ->  test.qsub line %d:" % (res_no, qsub_no))
                parts.extend("    " + c for c in cmd)
        else:
            parts.append("<none mapped (no Error, or test.qsub not in the usual check shape)>")

    parts.append("\n" + "=" * 70 + "\n## .command.err  (first 15 lines)\n" + "=" * 70)
    err = read_text(task_dir / ".command.err", "").splitlines()
    parts.append("\n".join(err[:15]) if err else "<empty>")

    parts.append("\n" + "=" * 70 + "\n## log.txt  (last 60 non-warning lines)\n" + "=" * 70)
    logl = [ln for ln in read_text(task_dir / "log.txt", "").splitlines()
            if not NOISE_LINE_RE.search(ln)]
    parts.append("\n".join(logl[-60:]) if logl else "<empty or absent>")

    out.write_text("\n".join(parts) + "\n")
    return out


def main(argv=None):
    a = parse_args(argv)
    if not a.work_dir.is_dir():
        sys.exit(f"error: work dir not found: {a.work_dir}")
    a.digests_dir.mkdir(parents=True, exist_ok=True)

    # Emit workdir/evidence_path under the official (recorded) run root when the run has been
    # relocated, so the CSV points at the canonical location. `official` may equal local_root.
    local_root = str(a.run_dir)
    if a.local_paths:
        official = None
    else:
        official = a.official_prefix or detect_official_root(a.work_dir)

    def to_official(p):
        if official and p and p.startswith(local_root):
            return official + p[len(local_root):]
        return p

    by_mod = load_out_csv(a.input_csv)
    rows = []
    tally = {}
    unknown_or_low = 0

    for task_dir, status in iter_failing_dirs(a.work_dir, a.only_failed):
        module, test_path = parse_command_sh(task_dir)
        module = module or "?"
        name, _, ver = module.partition("/")
        qsub_file = os.path.basename(test_path) if test_path else \
            os.path.basename(by_mod.get(module, {}).get("test_path", ""))

        category, confidence, ev_src, ev_line = classify(task_dir, test_path, status)
        category, new_conf, ev_line = maybe_transient_io(category, ev_line)
        confidence = new_conf or confidence
        if category == "test_template_not_implemented":
            ev_path = test_path or ""          # evidence is test.qsub at its permanent source
        else:
            ev_path = str((task_dir / ev_src).resolve()) if ev_src else ""

        rows.append({
            "module": module, "version": ver,
            "installer": by_mod.get(module, {}).get("module_installer", ""),
            "qsub_file": qsub_file,
            "test_result": status, "category": category, "confidence": confidence,
            "method": "regex", "harness_verdict": harness_verdict(task_dir),
            "evidence": ev_line, "evidence_path": to_official(ev_path),
            "workdir": to_official(str(task_dir.resolve())),
        })
        tally[category] = tally.get(category, 0) + 1
        if category == "unknown" or confidence in ("low", "none"):
            unknown_or_low += 1

        write_digest(a.digests_dir, module, qsub_file, task_dir.resolve(), test_path,
                     display_dir=to_official(str(task_dir.resolve())))

    rows.sort(key=lambda r: (r["category"], r["module"]))
    with open(a.output, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    print(f"Wrote {a.output}  ({len(rows)} failing tasks)", file=sys.stderr)
    print(f"Digests in {a.digests_dir}/", file=sys.stderr)
    print("category tally:", file=sys.stderr)
    for cat, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"    {cat:<26} {n}", file=sys.stderr)
    print(f"needs Claude review (unknown/low/none confidence): {unknown_or_low}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
