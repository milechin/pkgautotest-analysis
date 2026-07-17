#!/usr/bin/env python3
"""Deterministic first-pass diagnosis of a PkgAutoTest Nextflow *pipeline* run.

Part of the `pipeline-failure-analysis` skill. This is the complement to the
`pkgtest-failure-analysis` skill: that one explains why individual module *tests*
failed; this one explains why the Nextflow *pipeline/engine itself* failed,
terminated, or was aborted -- independent of any single test's result.

It streams `.nextflow.log` (plus `.nextflow/history`), reconstructs the run's
identity and lifecycle, classifies the run-termination cause via a prioritized
signature table, tabulates every task the engine saw, and drills into the
specific `work/XX/<hash>/` task dirs that were aborted or still running when the
run ended. Outputs go to an `analysis/` folder in the run directory:

  analysis/pipeline_verdict.csv    one row: run identity + WorkflowStats +
                                   termination cause (the Claude adjudication surface)
  analysis/pipeline_events.csv     one row per task the engine logged, with phase
                                   and (official + local) workdir
  analysis/pipeline_excerpts/      run_header.txt, termination_tail.txt, and one
                                   digest per suspect (aborted/running) task dir

A task-level non-zero exit in the log (`status: COMPLETED; exit: 254`) is an
*individual test* failure that PkgAutoTest deliberately ignores
(`errorStrategy 'ignore'`) -- it is counted here but never treated as a pipeline
cause; that is the sibling skill's domain. See
`.claude/skills/pipeline-failure-analysis/references/nextflow-log-format.md` and
`references/termination-causes.md`.

Pure Python 3 standard library; no dependencies. Standalone (does not import the
sibling skill's scripts).
"""

import argparse
import csv
import os
import re
import sys
from collections import deque
from pathlib import Path

# ---- .nextflow.log line grammar -------------------------------------------------
# "MMM-DD HH:MM:SS.mmm [thread] LEVEL  logger.name - message"  (no year in the ts;
# level is space-padded to 5; stack-trace / continuation lines have no timestamp
# and are attached to the preceding record).
LINE_HEAD_RE = re.compile(
    r"^([A-Z][a-z]{2})-(\d{2}) (\d{2}:\d{2}:\d{2}\.\d{3}) \[([^\]]+)\] "
    r"(\w+)\s+(\S+) - (.*)$")

# Header fields (first ~40 lines).
LAUNCH_RE   = re.compile(r"\$>\s*(nextflow .+?)\s*$")
VERSION_RE  = re.compile(r"N E X T F L O W\s*~\s*version (\S+)")
UUID_RE     = re.compile(r"Session UUID:\s*(\S+)")
RUNNAME_RE  = re.compile(r"Run name:\s*(\S+)")
POOL_RE     = re.compile(r"Executor pool size:\s*(\d+)")
PROCESS_RE  = re.compile(r"Process:\s*(\S+)")
WORKDIR_RE  = re.compile(r"Work-dir:\s*(\S+)")
EXECUTOR_RE = re.compile(r"--executor\s+(\S+)")

# Task lifecycle records.
SUBMITTED_RE = re.compile(r"\[(\w{2}/\w+)\] Submitted process > (\S+) \((.+)\)\s*$")
TASKHANDLER_RE = re.compile(
    r"TaskHandler\[id:\s*(\d+); name:\s*(\S+) \((.+?)\); status:\s*(\w+); "
    r"exit:\s*(\S+); error:\s*(.*?); workDir:\s*(\S+)\]")
IGNORED_NOTE_RE = re.compile(
    r"NOTE: Process .+ terminated with an error exit status \((-?\d+)\) -- Error is ignored")
UNABLE_KILL_RE = re.compile(r"Unable to kill (\S+) \((.+?)\) -- command:")
KILLING_RE = re.compile(r"Killing running tasks \((\d+)\)")
WORKFLOWSTATS_RE = re.compile(r"WorkflowStats\[(.+)\]")
GOODBYE_RE = re.compile(r"Execution complete -- Goodbye")
ABORT_RE = re.compile(r"Session aborted -- Cause:\s*(.+?)\s*$")
ADIEU_RE = re.compile(r"nextflow\.Session - Adieu\s*$")   # interrupt (SIGINT) shutdown
SIG_THREAD_RE = re.compile(r"^(SIG\w+) handler")           # e.g. "[SIGHUP handler]"

# A workdir path suffix: .../work/XX/<hash>
WORK_SUFFIX_RE = re.compile(r"(work/[0-9a-f]{2}/[0-9a-z]+)\s*$")

# ---- Termination-cause signature table -----------------------------------------
# Priority order: FIRST non-excluded match wins. Genuine early/root causes rank
# above late symptoms; `signal_abort` outranks `completed_clean` because Nextflow
# prints "Goodbye" even on abort. `completed_clean` and `still_running` are handled
# specially in classify_termination (they need compound conditions, not one regex).
# Mirror this list in references/termination-causes.md. Tuple:
#   (cause, confidence, include_regex, exclude_regex_or_None)
SIGNATURES = [
    ("script_compile_error", "high",
     r"MultipleCompilationErrorsException|Script compilation error|startup failed:|"
     r"unable to parse script|Unknown method `?\w+`? on|No signature of method", None),
    ("config_error", "high",
     r"Unknown config attribute|Cannot parse config file|Config file .* not found|"
     r"Unable to parse config file", None),
    ("plugin_or_startup_error", "high",
     r"Failed to (download|resolve|pull) plugin|Cannot find plugin|"
     r"Unable to (fetch|load) plugin", None),
    ("missing_input", "high",
     r"Cannot find any input file|Missing .* input file|No such file.*csv_input|"
     r"Path .* does not exist|Missing required parameter", None),
    ("driver_fork_exhaustion", "high",
     r"Cannot run program .*error=11|error=11, Resource temporarily unavailable|"
     r"unable to create native thread: possibly out of memory or process/resource limits|"
     r"pthread_create.*Resource temporarily unavailable|libgomp: Thread creation failed", None),
    ("executor_error", "high",
     r"Failed to submit process to grid|Error submitting process .* for execution|"
     r"Cannot run program|qsub: .*(error|fail)", r"error=11"),
    ("process_failed_terminate", "high",
     r"Error executing process >|Execution aborted due to an error|"
     r"Pipeline execution stopped with the following message", None),
    ("driver_oom", "high",
     r"java\.lang\.OutOfMemoryError|GC overhead limit exceeded", None),
    ("disk_error", "high",
     r"No space left on device|Disk quota exceeded", None),
    ("signal_abort", "high",
     r"Session aborted -- Cause:|nextflow\.Session - Adieu\s*$", None),
    ("cache_teardown_error", "medium",
     r"OperatorImpl - @unknown|org\.iq80\.leveldb|SortFileCollector|"
     r"CollectFileOp\.", None),
]
SIGNATURES = [(c, conf, re.compile(inc), re.compile(exc) if exc else None)
              for (c, conf, inc, exc) in SIGNATURES]

VERDICT_COLUMNS = [
    "run_name", "session_uuid", "executor", "host", "started", "ended", "duration",
    "history_status", "termination_cause", "confidence", "method",
    "succeeded", "failed", "ignored", "aborted", "running", "peak_running",
    "evidence", "evidence_path", "interpretation",
]

EVENT_COLUMNS = [
    "task_id", "short_tag", "module", "status", "task_exit", "error", "phase",
    "has_test_metrics", "exists_on_disk", "workdir_official", "workdir_local",
]


def find_run_root(explicit=None):
    """Return the PkgAutoTest run root. Like the sibling skill's helper, but
    RELAXED: a pipeline can die before `work/` is populated, so a directory with
    a `.nextflow.log` (or `.nextflow/`) also qualifies -- not only one with both
    `out.csv` and `work/`. Search ancestors of the current working directory
    first (run from inside the run dir), then ancestors of this script (so the
    installed <run>/.claude/skills/.../scripts/ layout also resolves)."""
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
    p.add_argument("--run-dir", type=Path, default=None,
                   help="Pipeline run root (auto-detected from CWD if omitted)")
    p.add_argument("--log", type=Path, default=None,
                   help="Analyze a specific .nextflow.log(.N) file (default: the active one)")
    p.add_argument("--session", default=None,
                   help="Pick the .nextflow.log* whose header Session UUID matches this")
    p.add_argument("--work-dir", type=Path, default=None)
    p.add_argument("--output-dir", type=Path, default=None,
                   help="default: <run-dir>/analysis")
    p.add_argument("--official-prefix", default=None,
                   help="Recorded (execution-time) run root, if it differs from the "
                        "on-disk location. Default: auto-detect from the log's Work-dir header.")
    p.add_argument("--local-paths", action="store_true",
                   help="Assume recorded paths are already local; disable official<->local rewrite.")
    p.add_argument("--tail", type=int, default=80,
                   help="How many trailing log records to keep for the termination digest.")
    a = p.parse_args(argv)
    a.run_dir = find_run_root(a.run_dir)
    a.work_dir = a.work_dir or a.run_dir / "work"
    a.output_dir = a.output_dir or a.run_dir / "analysis"
    a.excerpts_dir = a.output_dir / "pipeline_excerpts"
    a.output_dir.mkdir(parents=True, exist_ok=True)
    return a


def read_text(path, default=""):
    try:
        with open(path, errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def read_header_uuid(path):
    """Cheap peek: the Session UUID from a log file's header, or None."""
    try:
        with open(path, errors="replace") as fh:
            for _ in range(60):
                line = fh.readline()
                if not line:
                    break
                m = UUID_RE.search(line)
                if m:
                    return m.group(1)
    except OSError:
        pass
    return None


def find_active_log(run_dir, explicit_log, session):
    """Locate the target .nextflow.log. --log wins; then --session (match header
    UUID across all .nextflow.log*); else the active (unrotated) .nextflow.log;
    else the newest .nextflow.log* by mtime."""
    if explicit_log:
        return Path(explicit_log).resolve()
    candidates = sorted(run_dir.glob(".nextflow.log*"))
    if session:
        for c in candidates:
            if read_header_uuid(c) == session:
                return c.resolve()
        sys.exit("error: no .nextflow.log* with Session UUID %s in %s" % (session, run_dir))
    active = run_dir / ".nextflow.log"
    if active.is_file():
        return active.resolve()
    if candidates:
        return max(candidates, key=lambda c: c.stat().st_mtime).resolve()
    sys.exit("error: no .nextflow.log found in %s (is this a Nextflow run dir?)" % run_dir)


def iter_records(path):
    """Stream (lineno, ts, thread, level, logger, msg, cont_lines) records.

    A record is a header line plus any following continuation lines (stack
    traces, indented blocks) that lack their own `MMM-DD HH:MM:SS` header."""
    cur = None
    try:
        fh = open(path, errors="replace")
    except OSError as e:
        sys.exit("error: cannot read %s: %s" % (path, e))
    with fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.rstrip("\n")
            m = LINE_HEAD_RE.match(line)
            if m:
                if cur is not None:
                    yield cur
                cur = {
                    "lineno": lineno, "ts": "%s-%s %s" % (m.group(1), m.group(2), m.group(3)),
                    "thread": m.group(4), "level": m.group(5), "logger": m.group(6),
                    "msg": m.group(7), "cont": [], "raw": line,
                }
            elif cur is not None:
                cur["cont"].append(line)
                cur["raw"] += "\n" + line
            else:
                # Pre-header junk (rare); start a synthetic record.
                cur = {"lineno": lineno, "ts": "", "thread": "", "level": "",
                       "logger": "", "msg": line, "cont": [], "raw": line}
        if cur is not None:
            yield cur


def parse_header(path):
    """Read the run-identity header from the top of the log."""
    h = {"launch": "", "version": "", "session_uuid": "", "run_name": "",
         "pool": "", "host": "", "official_workdir": "", "executor": ""}
    text = ""
    try:
        with open(path, errors="replace") as fh:
            for _ in range(60):
                line = fh.readline()
                if not line:
                    break
                text += line
    except OSError:
        return h
    for key, rx in (("launch", LAUNCH_RE), ("version", VERSION_RE),
                    ("session_uuid", UUID_RE), ("run_name", RUNNAME_RE),
                    ("pool", POOL_RE), ("host", PROCESS_RE),
                    ("official_workdir", WORKDIR_RE)):
        m = rx.search(text)
        if m:
            h[key] = m.group(1)
    m = EXECUTOR_RE.search(h["launch"])
    if m:
        h["executor"] = m.group(1)
    elif h["pool"]:
        h["executor"] = "local"
    return h


def parse_history(run_dir, uuid):
    """Return the .nextflow/history row (dict) whose session UUID (col 6) matches.
    The short session id (col 5) can be identical across runs -- match col 6."""
    hist = run_dir / ".nextflow" / "history"
    text = read_text(hist, None)
    if text is None:
        return {}
    for line in text.splitlines():
        cols = line.split("\t")
        if len(cols) >= 6 and uuid and cols[5].strip() == uuid:
            return {"timestamp": cols[0].strip(), "duration": cols[1].strip(),
                    "run_name": cols[2].strip(), "status": cols[3].strip(),
                    "session_uuid": cols[5].strip()}
    return {}


def parse_workflow_stats(msg):
    """Parse `WorkflowStats[key=value; key=value; ...]` into a dict."""
    m = WORKFLOWSTATS_RE.search(msg)
    if not m:
        return {}
    stats = {}
    for pair in m.group(1).split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            stats[k.strip()] = v.strip()
    return stats


def official_root_from_workdir(official_workdir):
    """Strip a trailing '/work' from the header Work-dir to get the run root."""
    if not official_workdir:
        return None
    w = official_workdir.rstrip("/")
    if w.endswith("/work"):
        return w[:-len("/work")]
    return w


def resolve_local_workdir(run_dir, work_dir, full_official=None, short_tag=None):
    """Map a log-recorded (official) work dir to the on-disk location.

    Primary, prefix-agnostic: pull the `work/XX/<hash>` suffix from a full path
    and join it to the local run root. For a short tag `XX/6hex` (aborted/running
    tasks that never logged a full workDir), glob `work/XX/6hex*`. Returns a Path
    or None."""
    if full_official:
        m = WORK_SUFFIX_RE.search(full_official)
        if m:
            cand = run_dir / m.group(1)
            if cand.is_dir():
                return cand
    if short_tag and "/" in short_tag:
        level, prefix = short_tag.split("/", 1)
        matches = sorted(work_dir.glob("%s/%s*" % (level, prefix))) if work_dir.is_dir() else []
        matches = [m for m in matches if m.is_dir()]
        if len(matches) == 1:
            return matches[0]
    return None


def classify_termination(signals, has_goodbye, has_abort, abort_cause,
                         has_driver_error, stats):
    """Apply the priority table + compound rules. Returns
    (cause, confidence, evidence, evidence_lineno, corroboration)."""
    for cause, confidence, _inc, _exc in SIGNATURES:
        if cause in signals:
            ev = signals[cause]
            corro = ""
            if cause == "signal_abort" and abort_cause:
                corro = "cause=%s" % abort_cause
            return cause, confidence, ev["line"], ev["lineno"], corro

    aborted = stats.get("abortedCount")
    if has_goodbye and not has_abort and not has_driver_error and (
            aborted in (None, "0")):
        return ("completed_clean", "high",
                "Execution complete -- Goodbye (no abort, abortedCount=0)", 0, "")

    if not has_goodbye and not has_abort and not stats:
        return ("still_running_or_truncated", "medium",
                "No terminal marker (no Goodbye / Session aborted / WorkflowStats)", 0,
                "log may be for a run still in progress or hard-killed")

    return ("unknown", "none", "", 0, "")


def fence(title):
    return "\n" + "=" * 70 + "\n## %s\n" % title + "=" * 70


def write_run_header_digest(excerpts_dir, header, hist, stats, cause, evidence):
    parts = ["# PIPELINE RUN HEADER\n"]
    parts.append(fence("Run identity"))
    for k in ("run_name", "session_uuid", "executor", "version", "host",
              "official_workdir", "launch"):
        parts.append("%-18s %s" % (k + ":", header.get(k, "")))
    parts.append(fence(".nextflow/history row (matched on Session UUID)"))
    parts.append("\n".join("%-12s %s" % (k + ":", v) for k, v in hist.items())
                 if hist else "<no matching history row>")
    parts.append(fence("WorkflowStats"))
    parts.append("\n".join("%-16s %s" % (k, v) for k, v in stats.items())
                 if stats else "<no WorkflowStats line -- run did not reach the stats observer>")
    parts.append(fence("First-pass termination cause (regex)"))
    parts.append("cause:    %s\nevidence: %s" % (cause, evidence or "<none>"))
    (excerpts_dir / "run_header.txt").write_text("\n".join(parts) + "\n")


def write_tail_digest(excerpts_dir, tail_records):
    parts = ["# TERMINATION TAIL (last %d log records)\n" % len(tail_records)]
    parts.append("Noise (consequences of an abort, not causes): 'Killing running tasks (N)', "
                 "'Unable to kill ...', 'still active' operators, the trailing leveldb/"
                 "SortFileCollector NullPointerException (a teardown symptom). See "
                 "references/termination-causes.md.\n")
    parts.append("=" * 70)
    parts.append("\n".join(r["raw"] for r in tail_records))
    (excerpts_dir / "termination_tail.txt").write_text("\n".join(parts) + "\n")


def slug(s):
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)


def write_suspect_digest(excerpts_dir, module, short_tag, local_dir, official_dir):
    out = excerpts_dir / ("%s.txt" % slug("%s__%s" % (module, short_tag)))
    parts = ["# SUSPECT TASK (aborted / running at termination)\n"]
    parts.append("# MODULE: %s\n# SHORT TAG: %s\n# OFFICIAL DIR: %s\n# LOCAL DIR: %s\n"
                 % (module, short_tag, official_dir or "?", local_dir or "<unresolved>"))
    if not local_dir:
        parts.append(fence("status"))
        parts.append("Work dir could not be located on disk (cleaned, or run relocated "
                     "without --official-prefix). Nothing to drill into.")
        out.write_text("\n".join(parts) + "\n")
        return out
    ld = Path(local_dir)
    has_metrics = (ld / "test_metrics.csv").is_file()
    parts.append(fence("state"))
    parts.append("test_metrics.csv present: %s   (absent => INCOMPLETE: the task never "
                 "wrote a result -- aborted before finishing)" % has_metrics)
    exitcode = read_text(ld / ".exitcode", "").strip()
    parts.append(".exitcode (Nextflow task exit): %s" % (exitcode or "<none>"))
    parts.append(fence(".command.out  (echoed env: MODULE / JOB_ID / HOSTNAME / ...)"))
    parts.append("\n".join(read_text(ld / ".command.out", "").splitlines()[:25]) or "<empty>")
    parts.append(fence(".command.err  (last 25 lines)"))
    parts.append("\n".join(read_text(ld / ".command.err", "").splitlines()[-25:]) or "<empty>")
    parts.append(fence(".command.log  (last 25 lines)"))
    parts.append("\n".join(read_text(ld / ".command.log", "").splitlines()[-25:]) or "<empty>")
    for name in ("results.txt", "log.txt"):
        body = read_text(ld / name, None)
        if body is not None:
            parts.append(fence("%s  (last 20 lines)" % name))
            parts.append("\n".join(body.splitlines()[-20:]) or "<empty>")
    out.write_text("\n".join(parts) + "\n")
    return out


def main(argv=None):
    a = parse_args(argv)
    log_path = find_active_log(a.run_dir, a.log, a.session)
    a.excerpts_dir.mkdir(parents=True, exist_ok=True)

    header = parse_header(log_path)
    hist = parse_history(a.run_dir, header.get("session_uuid"))

    # Official (recorded) run root, for official<->local path rewrite.
    if a.local_paths:
        official_root = None
    else:
        official_root = a.official_prefix or official_root_from_workdir(
            header.get("official_workdir"))

    # ---- stream the log, building the task index + termination signals ----
    submitted = {}        # short_tag -> {module, lineno}
    completed = {}         # short_tag -> {task_id, module, status, exit, error, workdir}
    ignored_tags = set()   # short_tags that emitted an "Error is ignored" NOTE
    killed_modules = []    # modules named in "Unable to kill" during the kill sweep
    signals = {}           # cause -> {line, lineno}
    stats = {}
    has_goodbye = has_abort = has_driver_error = False
    abort_cause = ""
    signal_from_thread = ""
    first_ts = last_ts = ""
    tail = deque(maxlen=max(10, a.tail))
    sig_causes = [c for (c, _, _, _) in SIGNATURES]

    for rec in iter_records(log_path):
        tail.append(rec)
        if rec["ts"]:
            first_ts = first_ts or rec["ts"]
            last_ts = rec["ts"]
        msg, raw = rec["msg"], rec["raw"]

        m = SUBMITTED_RE.search(msg)
        if m:
            submitted[m.group(1)] = {"module": m.group(3), "lineno": rec["lineno"]}

        for m in TASKHANDLER_RE.finditer(raw):
            wd = m.group(7)
            sm = WORK_SUFFIX_RE.search(wd)
            tag = None
            if sm:
                p = sm.group(1).split("/")   # work, XX, hash
                tag = "%s/%s" % (p[1], p[2][:6])
            key = tag or ("id:%s" % m.group(1))
            completed[key] = {"task_id": m.group(1), "module": m.group(3),
                              "status": m.group(4), "exit": m.group(5),
                              "error": m.group(6), "workdir": wd}

        if IGNORED_NOTE_RE.search(msg):
            tm = re.search(r"\[(\w{2}/\w+)\]", msg)
            if tm:
                ignored_tags.add(tm.group(1))

        km = UNABLE_KILL_RE.search(msg)
        if km:
            killed_modules.append(km.group(2))

        if GOODBYE_RE.search(msg):
            has_goodbye = True
        tm = SIG_THREAD_RE.match(rec["thread"])
        if tm:
            signal_from_thread = tm.group(1)
        am = ABORT_RE.search(msg)
        if am:
            has_abort = True
            abort_cause = am.group(1)
        elif ADIEU_RE.search(rec["raw"]):
            has_abort = True
            abort_cause = abort_cause or signal_from_thread or "interrupt (Adieu)"
        if rec["level"] == "ERROR":
            has_driver_error = True
        if not stats:
            s = parse_workflow_stats(msg)
            if s:
                stats = s

        # Termination signatures: first match per cause (search the full record).
        for cause, _conf, inc_re, exc_re in SIGNATURES:
            if cause in signals:
                continue
            if inc_re.search(raw) and not (exc_re and exc_re.search(raw)):
                signals[cause] = {"line": (rec["msg"] or raw.splitlines()[0])[:300],
                                  "lineno": rec["lineno"]}

    cause, confidence, evidence, ev_lineno, corro = classify_termination(
        signals, has_goodbye, has_abort, abort_cause, has_driver_error, stats)

    # ---- classify every task into a phase + resolve its work dir ----
    # `completed` holds each task's LAST-seen TaskHandler (last-wins), so its
    # `status` is the task's final engine state: COMPLETED (finished, exit tells
    # pass/fail), RUNNING (cut off mid-run at termination), or ABORTED.
    def phase_of(tag):
        c = completed.get(tag)
        if not c:
            return "submitted_never_completed"  # submitted, no TaskHandler at all
        status = c["status"]
        if status == "ABORTED":
            return "aborted_at_termination"
        if status != "COMPLETED":
            return "running_at_termination"      # RUNNING (or any non-terminal state)
        exit_nonzero = (c["exit"] not in ("0", "-", "")
                        and c["exit"].lstrip("-").isdigit())
        if tag in ignored_tags or exit_nonzero:
            return "ignored_error"
        return "completed"

    all_tags = set(submitted) | set(completed)
    events = []
    suspects = []
    for tag in sorted(all_tags):
        c = completed.get(tag, {})
        module = c.get("module") or submitted.get(tag, {}).get("module", "?")
        phase = phase_of(tag)
        full_official = c.get("workdir") or ""
        local = resolve_local_workdir(a.run_dir, a.work_dir,
                                      full_official=full_official or None,
                                      short_tag=tag if "/" in tag else None)
        local_str = str(local) if local else ""
        official_str = full_official
        if not official_str and local and official_root:
            sm = WORK_SUFFIX_RE.search(str(local))
            if sm:
                official_str = official_root.rstrip("/") + "/" + sm.group(1)
        has_metrics = (local / "test_metrics.csv").is_file() if local else False
        events.append({
            "task_id": c.get("task_id", ""), "short_tag": tag,
            "module": module, "status": c.get("status", ""),
            "task_exit": c.get("exit", ""), "error": c.get("error", ""),
            "phase": phase, "has_test_metrics": str(has_metrics).lower(),
            "exists_on_disk": str(bool(local)).lower(),
            "workdir_official": official_str, "workdir_local": local_str,
        })
        if phase in ("aborted_at_termination", "running_at_termination",
                     "submitted_never_completed") and not has_metrics:
            suspects.append((module, tag, local_str, official_str))

    events.sort(key=lambda e: (e["phase"], e["module"].lower()))

    # ---- write outputs ----
    write_run_header_digest(a.excerpts_dir, header, hist, stats, cause, evidence)
    write_tail_digest(a.excerpts_dir, list(tail))
    for module, tag, local_str, official_str in suspects:
        write_suspect_digest(a.excerpts_dir, module, tag, local_str, official_str)

    events_path = a.output_dir / "pipeline_events.csv"
    with open(events_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=EVENT_COLUMNS)
        w.writeheader()
        w.writerows(events)

    verdict = {
        "run_name": header.get("run_name", ""), "session_uuid": header.get("session_uuid", ""),
        "executor": header.get("executor", ""), "host": header.get("host", ""),
        "started": first_ts, "ended": last_ts,
        "duration": hist.get("duration", ""), "history_status": hist.get("status", ""),
        "termination_cause": cause, "confidence": confidence, "method": "regex",
        "succeeded": stats.get("succeededCount", ""), "failed": stats.get("failedCount", ""),
        "ignored": stats.get("ignoredCount", ""), "aborted": stats.get("abortedCount", ""),
        "running": stats.get("runningCount", ""), "peak_running": stats.get("peakRunning", ""),
        "evidence": (evidence + ((" [%s]" % corro) if corro else "")) if evidence else corro,
        "evidence_path": "%s:%d" % (log_path, ev_lineno) if ev_lineno else str(log_path),
        "interpretation": "",
    }
    verdict_path = a.output_dir / "pipeline_verdict.csv"
    with open(verdict_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=VERDICT_COLUMNS)
        w.writeheader()
        w.writerow(verdict)

    # ---- stderr summary ----
    print("Analyzed log: %s" % log_path, file=sys.stderr)
    print("Run: %s (%s)  executor=%s  history=%s" % (
        header.get("run_name", "?"), header.get("session_uuid", "?"),
        header.get("executor", "?"), hist.get("status", "?")), file=sys.stderr)
    print("Termination cause (regex first pass): %s [%s]  %s" % (
        cause, confidence, corro), file=sys.stderr)
    if stats:
        print("WorkflowStats: succeeded=%s failed=%s ignored=%s aborted=%s running=%s" % (
            stats.get("succeededCount", "?"), stats.get("failedCount", "?"),
            stats.get("ignoredCount", "?"), stats.get("abortedCount", "?"),
            stats.get("runningCount", "?")), file=sys.stderr)
    ph = {}
    for e in events:
        ph[e["phase"]] = ph.get(e["phase"], 0) + 1
    print("task phases:", file=sys.stderr)
    for k, n in sorted(ph.items(), key=lambda kv: (-kv[1], kv[0])):
        print("    %-28s %d" % (k, n), file=sys.stderr)
    print("Wrote %s, %s" % (verdict_path, events_path), file=sys.stderr)
    print("Digests in %s/ (%d suspect task dir(s))" % (a.excerpts_dir, len(suspects)),
          file=sys.stderr)
    print("Next: adjudicate termination_cause/confidence/interpretation in "
          "pipeline_verdict.csv (read pipeline_excerpts/), then render_pipeline_summary.py.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
