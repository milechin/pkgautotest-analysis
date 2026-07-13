#!/usr/bin/env python3
"""Rebuild report_out.csv from a crashed/incomplete PkgAutoTest Nextflow run.

The PkgAutoTest pipeline normally aggregates every per-task ``test_metrics.csv``
into ``report_<input>.csv`` via Nextflow's ``collectFile`` as its final step.
When a run does not finish before that step (e.g. it was interrupted), the
report is never written. This script reconstructs it directly from the
surviving ``work/`` task directories, and additionally accounts for tasks that
never produced a result.

Every row of the input CSV (the intended unit of work) is represented in the
output with one of four ``test_result`` values:

  PASSED / FAILED  Faithful row copied verbatim from a completed task's
                   test_metrics.csv (the pipeline's own PASSED/FAILED verdict).
  INCOMPLETE       A task directory exists (has .command.sh) but wrote no
                   test_metrics.csv -- it started but was aborted/killed before
                   finishing. Fields are synthesized from the surviving files.
  NOT_PROCESSED    An input CSV row with no task directory at all -- Nextflow
                   terminated before it ever ran. Synthesized from the CSV row.

Faithfulness caveats:
  * A synthesized INCOMPLETE row's ``exit_code`` is the Nextflow *task* exit
    code (from .exitcode), NOT the module's test.qsub exit code (which never
    completed). This differs in meaning from completed rows' exit_code.
  * Completed rows keep their ``workdir`` exactly as recorded, which for this
    run is a stale path (the run was relocated after execution) and will not
    resolve on disk. This is intentional (faithful reconstruction).

Pure Python 3 standard library; no dependencies.
"""

import argparse
import os
import re
import sys
from pathlib import Path

# The canonical header emitted by the pipeline's test_metrics.csv. Reused
# verbatim (including its irregular spacing) so the output matches what
# collectFile(keepHeader:true) would have produced. 13 fields.
CANONICAL_HEADER = (
    "job_number, hostname, qsub_file, test_result,module, tests_passed, "
    "tests_failed, log_error_count, exit_code, installer, category, "
    "install_date,  workdir"
)

# Column order for synthesized rows. Matches CANONICAL_HEADER field-for-field.
FIELDS = [
    "job_number", "hostname", "qsub_file", "test_result", "module",
    "tests_passed", "tests_failed", "log_error_count", "exit_code",
    "installer", "category", "install_date", "workdir",
]

MODULE_RE = re.compile(r"^\s*echo MODULE=(.+?)\s*$", re.MULTILINE)
TEST_DIR_RE = re.compile(r"TEST_DIR=`dirname\s+(\S+)`")


def find_run_root(explicit=None):
    """Return the PkgAutoTest run root (dir containing out.csv and work/).
    If --run-dir is given, use it. Otherwise search ancestors of the current
    working directory first (the intuitive case: run from inside the run dir),
    then ancestors of this script (so the installed <run>/.claude/skills/.../
    scripts/ layout also resolves)."""
    if explicit:
        return Path(explicit).resolve()
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        s = start.resolve()
        for cand in (s, *s.parents):
            if (cand / "out.csv").is_file() and (cand / "work").is_dir():
                return cand
    return Path.cwd()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", type=Path, default=None,
                   help="Pipeline run root containing out.csv and work/ (auto-detected from CWD)")
    p.add_argument("--input-csv", type=Path, default=None,
                   help="Input CSV from find_qsub.py (default: <run-dir>/out.csv)")
    p.add_argument("--work-dir", type=Path, default=None,
                   help="Nextflow work directory (default: <run-dir>/work)")
    p.add_argument("--output", type=Path, default=None,
                   help="Output report path (default: <run-dir>/analysis/report_out.csv)")
    args = p.parse_args(argv)
    args.run_dir = find_run_root(args.run_dir)
    if args.input_csv is None:
        args.input_csv = args.run_dir / "out.csv"
    if args.work_dir is None:
        args.work_dir = args.run_dir / "work"
    if args.output is None:
        args.output = args.run_dir / "analysis" / "report_out.csv"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    return args


def load_input_csv(path):
    """Return (header_fields, list-of-row-dicts) preserving order & duplicates."""
    with open(path, newline="") as fh:
        lines = fh.read().splitlines()
    if not lines:
        sys.exit(f"error: {path} is empty")
    header = [c.strip() for c in lines[0].split(",")]
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        # find_qsub.py output has no quoted/embedded commas in the fields we
        # use; qsub_options may contain inline '#' comments but not commas.
        values = line.split(",")
        row = {header[i]: (values[i].strip() if i < len(values) else "")
               for i in range(len(header))}
        rows.append(row)
    return header, rows


def read_first(path, default=""):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return default


def grep_iow_count(path, word):
    """Count whole-word, case-insensitive matches of `word` -- mirrors the
    pipeline's ``grep -iow`` used to fill tests_passed/failed/log_error_count."""
    text = read_first(path, None)
    if text is None:
        return ""
    return str(len(re.findall(r"(?<![A-Za-z0-9])" + re.escape(word) +
                              r"(?![A-Za-z0-9])", text, re.IGNORECASE)))


def parse_command_sh(task_dir):
    """Return (module_name_version, test_path) parsed from .command.sh, or
    (None, None) if the file is absent (e.g. the stray empty dir)."""
    text = read_first(task_dir / ".command.sh", None)
    if text is None:
        return None, None
    m = MODULE_RE.search(text)
    module = m.group(1) if m else None
    t = TEST_DIR_RE.search(text)
    test_path = t.group(1) if t else None
    return module, test_path


def extract_echoed(task_dir, key):
    """Pull ``<key>=<value>`` echoed into .command.out (JOB_ID, HOSTNAME)."""
    text = read_first(task_dir / ".command.out", "")
    m = re.search(r"^%s=(.*)$" % re.escape(key), text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def make_row(**kw):
    """Build a synthesized data line matching the ', ' spacing of real rows."""
    return ", ".join(kw.get(f, "") for f in FIELDS)


def module_name_of(data_line):
    """Return the 'module' field (col 5) from a data line, stripped."""
    parts = data_line.split(",")
    return parts[4].strip() if len(parts) > 4 else ""


def main(argv=None):
    args = parse_args(argv)
    if not args.input_csv.is_file():
        sys.exit(f"error: input CSV not found: {args.input_csv}")
    if not args.work_dir.is_dir():
        sys.exit(f"error: work dir not found: {args.work_dir}")

    _, csv_rows = load_input_csv(args.input_csv)

    # module_name_version -> list of input rows (preserves duplicates)
    csv_by_module = {}
    for row in csv_rows:
        csv_by_module.setdefault(row.get("module_name_version", ""), []).append(row)

    data_lines = []            # completed (verbatim) + synthesized INCOMPLETE
    taskdir_count = {}         # module_name_version -> #task dirs seen
    skipped_dirs = []
    counts = {"PASSED": 0, "FAILED": 0, "INCOMPLETE": 0, "NOT_PROCESSED": 0}

    # Walk work/XX/<hash>/ task directories.
    for level1 in sorted(args.work_dir.iterdir()):
        if not level1.is_dir():
            continue
        for task_dir in sorted(level1.iterdir()):
            if not task_dir.is_dir():
                continue
            metrics = task_dir / "test_metrics.csv"
            command_sh = task_dir / ".command.sh"

            if metrics.is_file():
                lines = read_first(metrics).splitlines()
                if len(lines) < 2 or not lines[1].strip():
                    skipped_dirs.append((task_dir, "empty test_metrics.csv"))
                    continue
                data = lines[1].strip()
                data_lines.append(data)
                verdict = data.split(",")[3].strip() if len(data.split(",")) > 3 else ""
                counts[verdict] = counts.get(verdict, 0) + 1
                module, _ = parse_command_sh(task_dir)
                if module:
                    taskdir_count[module] = taskdir_count.get(module, 0) + 1
                continue

            module, test_path = parse_command_sh(task_dir)
            if module is None:
                # No .command.sh at all -> not a real task (e.g. stray dir).
                skipped_dirs.append((task_dir, "no .command.sh"))
                continue

            taskdir_count[module] = taskdir_count.get(module, 0) + 1
            src = csv_by_module.get(module, [{}])[0]
            qsub_file = os.path.basename(test_path) if test_path else \
                os.path.basename(src.get("test_path", ""))
            exitcode = read_first(task_dir / ".exitcode", "").strip()

            data_lines.append(make_row(
                job_number=extract_echoed(task_dir, "JOB_ID"),
                hostname=extract_echoed(task_dir, "HOSTNAME"),
                qsub_file=qsub_file,
                test_result="INCOMPLETE",
                module=module,
                tests_passed=grep_iow_count(task_dir / "results.txt", "Passed"),
                tests_failed=grep_iow_count(task_dir / "results.txt", "Error"),
                log_error_count=grep_iow_count(task_dir / "log.txt", "error"),
                exit_code=exitcode,
                installer=src.get("module_installer", ""),
                category=src.get("module_category", ""),
                install_date=src.get("module_install_date", ""),
                workdir=str(task_dir.resolve()),
            ))
            counts["INCOMPLETE"] += 1

    # Reconcile coverage: input rows with no (or too few) task dirs were never
    # processed by Nextflow. Synthesize NOT_PROCESSED rows for the shortfall.
    for module, rows in csv_by_module.items():
        shortfall = len(rows) - taskdir_count.get(module, 0)
        for src in rows[len(rows) - shortfall:] if shortfall > 0 else []:
            data_lines.append(make_row(
                qsub_file=os.path.basename(src.get("test_path", "")),
                test_result="NOT_PROCESSED",
                module=module,
                installer=src.get("module_installer", ""),
                category=src.get("module_category", ""),
                install_date=src.get("module_install_date", ""),
                workdir="NA",
            ))
            counts["NOT_PROCESSED"] += 1

    # Sort by module name (col 5) for readability; stable, so duplicate-module
    # rows keep their discovery order.
    data_lines.sort(key=module_name_of)

    with open(args.output, "w") as fh:
        fh.write(CANONICAL_HEADER + "\n")
        for line in data_lines:
            fh.write(line + "\n")

    # ---- summary to stderr ----
    total = len(data_lines)
    print(f"Wrote {args.output}", file=sys.stderr)
    print(f"  data rows: {total}", file=sys.stderr)
    for k in ("PASSED", "FAILED", "INCOMPLETE", "NOT_PROCESSED"):
        print(f"    {k:<13} {counts.get(k, 0)}", file=sys.stderr)
    other = {k: v for k, v in counts.items()
             if k not in ("PASSED", "FAILED", "INCOMPLETE", "NOT_PROCESSED")}
    for k, v in other.items():
        print(f"    {k!r:<13} {v}  (unexpected test_result value)", file=sys.stderr)

    expected = len(csv_rows)
    status = "OK" if total == expected else "MISMATCH"
    print(f"  coverage: {total} report rows vs {expected} input rows [{status}]",
          file=sys.stderr)
    if total != expected:
        print("  WARNING: row count does not match input CSV -- investigate.",
              file=sys.stderr)
    if skipped_dirs:
        print(f"  skipped {len(skipped_dirs)} dir(s):", file=sys.stderr)
        for d, why in skipped_dirs:
            print(f"    {d}  ({why})", file=sys.stderr)

    return 0 if total == expected else 1


if __name__ == "__main__":
    sys.exit(main())
