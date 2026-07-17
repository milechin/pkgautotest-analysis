#!/usr/bin/env python3
"""Render pipeline_summary.md from pipeline_verdict.csv + pipeline_events.csv.

Part of the `pipeline-failure-analysis` skill. Deterministically turns the
(now Claude-adjudicated) single-row `pipeline_verdict.csv` and the per-task
`pipeline_events.csv` into a human-readable `pipeline_summary.md`: run identity
and lifecycle, the WorkflowStats tallies, the termination verdict with its
evidence and Claude's interpretation, and a list of the suspect (aborted /
running-at-termination) task dirs to investigate. All counts come from the CSVs.

Pure Python 3 standard library (3.6+); no third-party dependencies.
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# One-line meaning per termination cause. Keep in lockstep with the SIGNATURES
# table in diagnose_pipeline.py and references/termination-causes.md.
DESC = {
    "script_compile_error":
        "The pipeline script failed to compile/parse (Groovy/DSL error) — it never started tasks.",
    "config_error":
        "A `nextflow.config` problem (unknown attribute / unparseable config) stopped the run.",
    "plugin_or_startup_error":
        "A Nextflow plugin could not be downloaded/resolved at launch.",
    "missing_input":
        "A required input (e.g. the `--csv_input` file, or a channel) was missing or empty.",
    "driver_fork_exhaustion":
        "The Nextflow driver JVM could not fork a process / create a thread — the per-user process "
        "limit (`ulimit -u` / `RLIMIT_NPROC`) was exhausted, typically under `--executor local` on a "
        "login node (`Cannot run program \"bash\": error=11`, `unable to create native thread`). The "
        "pipeline-level twin of the per-test `resource_fork_exhaustion`. NOT a package defect — re-run "
        "on a compute node (`sge` executor) or throttle concurrency.",
    "executor_error":
        "The executor could not submit/launch a task (grid submit failure, cannot run program).",
    "process_failed_terminate":
        "A non-ignored process error aborted the whole run (distinct from the per-test failures "
        "PkgAutoTest ignores).",
    "driver_oom":
        "The Nextflow driver JVM ran out of heap (OutOfMemoryError / GC overhead).",
    "disk_error":
        "The driver hit `No space left on device` / `Disk quota exceeded` writing work/cache.",
    "signal_abort":
        "The run received a termination signal — `Session aborted -- Cause: SIG*`. SIGHUP usually "
        "means the controlling terminal/login session ended or the node rebooted (common for long "
        "`--executor local` runs on a login node); SIGINT = Ctrl-C; SIGTERM/SIGKILL = scheduler or "
        "`kill`. NOT a package defect — re-run detached / on a compute node.",
    "completed_clean":
        "The pipeline finished normally (`Execution complete -- Goodbye`, no abort, abortedCount=0). "
        "Any per-test failures are explained by the `pkgtest-failure-analysis` skill, not here.",
    "cache_teardown_error":
        "A cache/`collectFile` teardown crash (LevelDB / SortFileCollector NullPointerException). "
        "Usually a SYMPTOM of an abort that killed `collectFile` mid-write — check for a higher "
        "cause first.",
    "still_running_or_truncated":
        "No terminal marker in the log — the run may still be in progress, or was hard-killed "
        "(SIGKILL / OOM-killer / node crash) leaving no clean shutdown. Corroborate with "
        "`.nextflow/history` (duration `-`) and the log's mtime.",
    "unknown":
        "No signature matched and the log gave no determinable cause; needs human review "
        "(see pipeline_excerpts/termination_tail.txt).",
}

PHASE_DESC = {
    "completed": "ran to completion (exit 0)",
    "ignored_error": "a per-test failure PkgAutoTest ignored (see the pkgtest-failure-analysis skill)",
    "aborted_at_termination": "aborted when the run ended",
    "running_at_termination": "still running when the run ended (killed by the abort)",
    "submitted_never_completed": "submitted but the engine logged no final state (cut off)",
}


def find_run_root(explicit=None):
    if explicit:
        return Path(explicit).resolve()

    def looks_like_run(cand):
        if (cand / "out.csv").is_file() and (cand / "work").is_dir():
            return True
        if (cand / ".nextflow.log").is_file() or (cand / ".nextflow").is_dir():
            return True
        return any(cand.glob(".nextflow.log*"))

    for start in (Path.cwd(), Path(__file__).resolve().parent):
        s = start.resolve()
        for cand in (s, *s.parents):
            if looks_like_run(cand):
                return cand
    return Path.cwd()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run-dir", default=None,
                   help="Pipeline run root (auto-detected from CWD if omitted)")
    p.add_argument("--analysis-dir", default=None,
                   help="Where the pipeline_*.csv live / summary is written "
                        "(default: <run-dir>/analysis)")
    p.add_argument("--verdict", default=None, help="Path to pipeline_verdict.csv")
    p.add_argument("--events", default=None, help="Path to pipeline_events.csv")
    p.add_argument("--output", default=None, help="Path to pipeline_summary.md")
    a = p.parse_args(argv)
    run_root = find_run_root(a.run_dir)
    adir = Path(a.analysis_dir) if a.analysis_dir else run_root / "analysis"
    a.run_root = run_root
    a.verdict = Path(a.verdict) if a.verdict else adir / "pipeline_verdict.csv"
    a.events = Path(a.events) if a.events else adir / "pipeline_events.csv"
    a.output = Path(a.output) if a.output else adir / "pipeline_summary.md"
    return a


def main(argv=None):
    a = parse_args(argv)
    if not a.verdict.is_file():
        sys.exit("error: %s not found (run diagnose_pipeline.py first)" % a.verdict)
    rows = list(csv.DictReader(open(a.verdict, newline="")))
    if not rows:
        sys.exit("error: %s has no verdict row" % a.verdict)
    v = rows[0]
    events = list(csv.DictReader(open(a.events, newline=""))) if a.events.is_file() else []

    by_phase = defaultdict(list)
    for e in events:
        by_phase[e["phase"]].append(e)

    cause = v.get("termination_cause", "unknown")
    o = []
    o.append("# PkgAutoTest Pipeline Failure Analysis\n")
    o.append("Why the **Nextflow pipeline/engine** for run `%s` ended the way it did — the "
             "run-level counterpart to the per-test `pkgtest-failure-analysis` skill. Produced by "
             "the `pipeline-failure-analysis` skill (`diagnose_pipeline.py` first pass + Claude "
             "adjudication). Machine detail is in `pipeline_verdict.csv` / `pipeline_events.csv`; "
             "raw evidence is in `pipeline_excerpts/`.\n" % (v.get("run_name") or "?"))

    o.append("## Verdict\n")
    o.append("**Termination cause: `%s`** (%s confidence, method: %s)\n"
             % (cause, v.get("confidence") or "?", v.get("method") or "?"))
    o.append(DESC.get(cause, "") + "\n")
    if v.get("evidence"):
        o.append("- **Evidence:** %s" % v["evidence"])
    if v.get("evidence_path"):
        o.append("  `%s`" % v["evidence_path"])
    if v.get("interpretation"):
        o.append("\n%s\n" % v["interpretation"])

    o.append("\n## Run identity & lifecycle\n")
    o.append("| Field | Value |")
    o.append("|---|---|")
    for label, key in (("Run name", "run_name"), ("Session UUID", "session_uuid"),
                       ("Executor", "executor"), ("Driver host", "host"),
                       ("Started", "started"), ("Ended", "ended"),
                       ("Duration", "duration"), ("History status", "history_status")):
        if v.get(key):
            o.append("| %s | `%s` |" % (label, v[key]))
    o.append("")

    if any(v.get(k) for k in ("succeeded", "failed", "ignored", "aborted", "running")):
        o.append("## WorkflowStats (Nextflow's own tally)\n")
        o.append("| Succeeded | Failed | Ignored | Aborted | Running | Peak running |")
        o.append("|---:|---:|---:|---:|---:|---:|")
        o.append("| %s | %s | %s | %s | %s | %s |" % (
            v.get("succeeded") or "0", v.get("failed") or "0", v.get("ignored") or "0",
            v.get("aborted") or "0", v.get("running") or "0", v.get("peak_running") or "0"))
        o.append("\n> `Failed`/`Ignored` are individual **test** results (PkgAutoTest runs "
                 "`errorStrategy 'ignore'`), diagnosed by the `pkgtest-failure-analysis` skill — "
                 "not pipeline failures. `Aborted`/`Running` are tasks cut off when the run "
                 "ended.\n")

    if events:
        o.append("## Tasks by phase\n")
        o.append("| Phase | Count | Meaning |")
        o.append("|---|---:|---|")
        for ph in sorted(by_phase, key=lambda p: (-len(by_phase[p]), p)):
            o.append("| `%s` | %d | %s |" % (ph, len(by_phase[ph]), PHASE_DESC.get(ph, "")))
        o.append("")

    suspects = [e for e in events
                if e["phase"] in ("aborted_at_termination", "running_at_termination",
                                  "submitted_never_completed")
                and e.get("has_test_metrics") != "true"]
    if suspects:
        o.append("## Suspect tasks to investigate (aborted / running at termination)\n")
        o.append("These tasks were cut off when the run ended and never wrote a result "
                 "(INCOMPLETE). They are re-runs, not diagnosable failures. Digests are in "
                 "`pipeline_excerpts/`.\n")
        for e in sorted(suspects, key=lambda r: r["module"].lower()):
            loc = e.get("workdir_local") or e.get("workdir_official") or "<unresolved on disk>"
            o.append("- **%s** (`%s`, %s) — `%s`"
                     % (e["module"], e["short_tag"], e["phase"], loc))
        o.append("")

    if not (a.run_root / "report_out.csv").is_file() and \
       not (a.run_root / "analysis" / "report_out.csv").is_file():
        o.append("## Note: no `report_out.csv`\n")
        o.append("The pipeline's aggregate report is written by a final `collectFile` step. If the "
                 "run was aborted before that step (see the verdict above), the report was never "
                 "produced. Reconstruct it with the sibling skill's "
                 "`rebuild_report.py` (in `pkgtest-failure-analysis/scripts/`), then triage the "
                 "per-test failures there.\n")

    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text("\n".join(o) + "\n")
    print("Wrote %s  (cause=%s, %d task events)" % (a.output, cause, len(events)),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
