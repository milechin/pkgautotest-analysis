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
     r"No such file or directory|does not exist|cannot stat|cannot create directory.*File exists",
     None),
]
# Compile once.
SIGNATURES = [(c, conf, re.compile(inc, re.IGNORECASE),
               re.compile(exc, re.IGNORECASE) if exc else None)
              for (c, conf, inc, exc) in SIGNATURES]

# Evidence sources, scanned in this order (stderr first: root errors surface there).
EVIDENCE_FILES = [".command.err", "log.txt", ".command.out"]

CSV_COLUMNS = ["module", "version", "installer", "qsub_file", "test_result", "category",
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


def detect_official_root(work_dir):
    """Infer the official/recorded run root from a test_metrics.csv workdir column
    (the run may have been relocated; the recorded path is the canonical one)."""
    for level1 in sorted(p for p in work_dir.iterdir() if p.is_dir()):
        for task_dir in level1.iterdir():
            m = task_dir / "test_metrics.csv"
            if m.is_file():
                lines = read_text(m).splitlines()
                if len(lines) >= 2 and "/work/" in lines[1]:
                    return lines[1].split(",")[-1].strip().split("/work/")[0]
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
        lines = read_text(metrics).splitlines()
        if len(lines) >= 2 and lines[1].strip():
            parts = lines[1].split(",")
            return parts[3].strip() if len(parts) > 3 else ""
        return ""  # malformed metrics -> treat as non-failing (skip)
    return "INCOMPLETE"


def iter_failing_dirs(work_dir, only_failed):
    for level1 in sorted(p for p in work_dir.iterdir() if p.is_dir()):
        for task_dir in sorted(p for p in level1.iterdir() if p.is_dir()):
            if not (task_dir / ".command.sh").is_file():
                continue  # stray dir
            st = status_of(task_dir)
            if st == "FAILED" or (st == "INCOMPLETE" and not only_failed):
                yield task_dir, st


def classify(task_dir):
    """Return (category, confidence, evidence_source, evidence_line)."""
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
    return "unknown", "none", "", ""


def harness_verdict(task_dir):
    text = read_text(task_dir / ".command.err", "")
    found = []
    for m in HARNESS_RE.finditer(text):
        v = m.group(1)
        if v not in found:
            found.append(v)
    return "; ".join(found)


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

        category, confidence, ev_src, ev_line = classify(task_dir)
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
