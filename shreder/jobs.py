"""Background job queue. GUI-agnostic: UI updates are pushed through `dispatch`."""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

from . import engine, system
from .engine import Cancelled, CancelToken, Progress, Stats


@dataclass
class Job:
    kind: str                          # shred | wipe | trim
    title: str
    method_id: str = engine.DEFAULT_METHOD
    targets: list = field(default_factory=list)   # [(path, keep_root)]
    older_than_days: float = 0
    include_hidden: bool = True
    verify: bool = False
    rename_passes: int = 3
    directory: str = ""                # wipe: where fill files go; trim: mount point
    mountpoint: str = ""
    elevated: bool = False
    folder_ids: list = field(default_factory=list)
    group: str = ""                    # jobs queued together by a guided routine
    icon: str = "shreder-symbolic"

    id: int = 0
    state: str = "queued"              # queued | running | done | warning | failed | cancelled
    progress: Progress = field(default_factory=Progress)
    stats: Stats = field(default_factory=Stats)
    cancel: CancelToken = field(default_factory=CancelToken)
    log: list = field(default_factory=list)
    error: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    _proc: object = None

    @property
    def method(self) -> engine.Method:
        return engine.get_method(self.method_id)

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def add_log(self, level: str, message: str):
        self.log.append((time.time(), level, message))
        if len(self.log) > 2000:
            del self.log[:500]

    def summary(self) -> str:
        s = self.stats
        if self.kind == "trim":
            return self.error or (self.log[-1][2] if self.log else "TRIM finished")
        if self.kind == "wipe":
            text = f"{system.human_size(s.bytes_data)} of free space overwritten × {self.method.pass_count}"
        else:
            parts = [f"{s.files_shredded} file{'s' if s.files_shredded != 1 else ''} shredded"]
            if s.dirs_removed:
                parts.append(f"{s.dirs_removed} folder{'s' if s.dirs_removed != 1 else ''} removed")
            parts.append(system.human_size(s.bytes_data))
            if s.shared_kept:
                parts.append(f"{s.shared_kept} kept (shared data)")
            text = " · ".join(parts)
        if s.files_failed or s.errors:
            n = max(s.files_failed, len(s.errors))
            text += f" · {n} problem{'s' if n != 1 else ''}"
        return text


class JobManager:
    def __init__(self, store, dispatch=lambda fn: fn(), on_finished=None):
        self.store = store
        self.dispatch = dispatch
        self.on_finished = on_finished
        self.queue: collections.deque[Job] = collections.deque()
        self.current: Job | None = None
        self.finished: list[Job] = []
        self._listeners = []
        self._cv = threading.Condition()
        self._next_id = 1
        self._stop = False
        self._thread = threading.Thread(target=self._worker, name="shreder-jobs", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------- public api
    def connect(self, callback):
        self._listeners.append(callback)

    def _emit(self):
        def fire():
            for cb in list(self._listeners):
                cb()
        self.dispatch(fire)

    @property
    def busy(self) -> bool:
        return self.current is not None or bool(self.queue)

    @property
    def pending(self) -> list[Job]:
        return list(self.queue)

    def submit(self, job: Job) -> Job:
        with self._cv:
            job.id = self._next_id
            self._next_id += 1
            self.queue.append(job)
            self._cv.notify()
        self._emit()
        return job

    def cancel(self, job: Job):
        with self._cv:
            if job in self.queue:
                self.queue.remove(job)
                job.state = "cancelled"
                job.finished = time.time()
                self._emit()
                return
        job.cancel.cancel()
        proc = job._proc
        if proc is not None:
            try:
                proc.stdin.close()  # the elevated helper treats EOF on stdin as "cancel"
            except (OSError, AttributeError, ValueError):
                pass

    def cancel_all(self):
        for job in list(self.queue):
            self.cancel(job)
        if self.current:
            self.cancel(self.current)

    def shutdown(self, timeout: float = 30.0):
        self.cancel_all()
        with self._cv:
            self._stop = True
            self._cv.notify()
        self._thread.join(timeout)

    # ------------------------------------------------------------- worker
    def _worker(self):
        while True:
            with self._cv:
                while not self.queue and not self._stop:
                    self._cv.wait()
                if self._stop and not self.queue:
                    return
                job = self.queue.popleft()
                self.current = job
            self._run(job)
            if job.group and job.state in ("cancelled", "failed"):
                # a routine is a sequence: stop the remaining steps
                for other in [j for j in self.queue if j.group == job.group]:
                    self.cancel(other)
            with self._cv:
                self.current = None
                self.finished.insert(0, job)
                del self.finished[50:]
            self._emit()
            if self.on_finished:
                self.dispatch(lambda j=job: self.on_finished(j))

    def _run(self, job: Job):
        execute_job(job, self.store, on_start=self._emit)


def execute_job(job: Job, store, on_start=None):
    """Run a job synchronously in the calling thread and record it in history."""
    job.state = "running"
    job.started = time.time()
    job.progress = Progress()
    if on_start:
        on_start()
    try:
        if job.kind == "shred":
            _run_shred(job)
        elif job.kind == "wipe":
            if job.elevated:
                _run_elevated_wipe(job)
            else:
                _run_wipe(job, store)
        elif job.kind == "trim":
            _run_trim(job)
        problems = job.stats.errors or job.stats.files_failed or job.stats.warnings
        job.state = "warning" if problems else "done"  # anything to look at shows as a problem, not as a quiet success
    except Cancelled:
        job.state = "cancelled"
    except Exception as e:  # noqa: BLE001 - surface every failure to the user
        job.state = "failed"
        job.error = getattr(e, "strerror", None) or str(e)
        job.add_log("error", job.error)
    finally:
        job.finished = time.time()
        job._proc = None
    if job.folder_ids and job.state in ("done", "warning"):
        store.mark_folders_run(job.folder_ids, job.finished)
    store.record(history_entry(job))


def _shredder(job: Job) -> engine.Shredder:
    sh = engine.Shredder(job.method, verify=job.verify, rename_passes=job.rename_passes,
                         cancel=job.cancel, progress=job.progress, log=job.add_log)
    sh.stats = job.stats
    return sh


def _run_shred(job: Job):
    job.progress.phase = "scanning"
    job.progress.current = "Scanning items…"
    plan = engine.build_plan(job.targets, older_than_days=job.older_than_days,
                             include_hidden=job.include_hidden, cancel=job.cancel)
    _shredder(job).run(plan)


def _run_wipe(job: Job, store):
    if not job.directory:
        raise OSError("This drive has no folder you can write to. Turn on administrator mode.")
    _shredder(job).wipe_free_space(job.directory, on_workdir=store.track_workdir)


def _run_elevated_wipe(job: Job):
    cmd = ["pkexec", sys.executable, "-I", system.CLI_PATH, "wipe-free", job.mountpoint,
           "--method", job.method_id, "--json", "--stdin-cancel"]
    _run_helper(job, cmd)


def _run_trim(job: Job):
    job.progress.phase = "trimming"
    job.progress.current = f"Asking the drive to discard unused blocks on {job.mountpoint}"
    proc = subprocess.Popen(["pkexec", "fstrim", "-v", job.mountpoint], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    job._proc = proc
    out, err = proc.communicate()
    if proc.returncode in (126, 127):
        raise Cancelled()
    if proc.returncode != 0:
        raise OSError((err or out or "fstrim failed").strip())
    job.add_log("info", (out or "TRIM complete").strip())


def _run_helper(job: Job, cmd: list):
    job.progress.phase = "authorizing"
    job.progress.current = "Waiting for administrator authorization…"
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    job._proc = proc
    cancelled = False
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "progress":
            p = job.progress
            for key in ("phase", "current", "pass_no", "pass_total", "done", "total",
                        "files_done", "files_total", "rate"):
                if key in msg:
                    setattr(p, key, msg[key])
        elif kind == "log":
            job.add_log(msg.get("level", "info"), msg.get("message", ""))
            if msg.get("level") == "error":
                job.stats.errors.append(msg.get("message", ""))
            elif msg.get("level") == "warning":
                job.stats.warnings.append(msg.get("message", ""))
        elif kind == "done":
            st = msg.get("stats", {})
            for key in ("files_shredded", "files_failed", "shared_kept", "dirs_removed", "links_removed",
                        "bytes_data", "bytes_written"):
                setattr(job.stats, key, st.get(key, 0))
        elif kind == "cancelled":
            cancelled = True
        elif kind == "error":
            job.error = msg.get("message", "")
    err = proc.stderr.read()
    rc = proc.wait()
    if cancelled or job.cancel.cancelled or rc in (126, 127):
        raise Cancelled()
    if rc != 0:
        raise OSError(job.error or (err or "").strip() or f"helper exited with code {rc}")


def history_entry(job: Job) -> dict:
    stats = job.stats.as_dict()
    stats["errors"] = stats["errors"][:50]
    stats["warnings"] = stats["warnings"][:50]
    return {
        "id": f"{int(job.created)}-{job.id}",
        "time": job.started or job.created,
        "duration": max(0.0, (job.finished or time.time()) - (job.started or job.created)),
        "kind": job.kind,
        "title": job.title,
        "method": job.method_id,
        "state": job.state,
        "error": job.error,
        "targets": [p for p, _ in job.targets][:25] or ([job.mountpoint] if job.mountpoint else []),
        "stats": stats,
        "summary": job.summary(),
    }


def cleanup_stale_workdirs(store) -> int:
    """Remove free-space fill folders left behind by a crash or power loss."""
    removed = 0
    for path in list(store.data.get("pending_cleanup", [])):
        if os.path.basename(path).startswith(engine.WIPE_DIR_PREFIX):
            engine.remove_wipe_dir(path)
            removed += 1
        store.track_workdir(path, False)
    return removed
