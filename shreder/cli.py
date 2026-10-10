#!/usr/bin/env python3
"""Shreder command line: headless shredding, free-space wipes, scheduled runs.

Also used by the desktop app as the elevated (pkexec) helper for wipes.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shreder import engine, system  # noqa: E402


class Reporter:
    """Prints progress either as a terminal status line or as JSON lines."""

    def __init__(self, progress: engine.Progress, as_json: bool):
        self.progress = progress
        self.as_json = as_json
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def emit(self, obj: dict):
        with self._lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def log(self, level: str, message: str):
        if self.as_json:
            self.emit({"type": "log", "level": level, "message": message})
        else:
            with self._lock:
                sys.stderr.write(f"\r\033[K{level}: {message}\n")

    def _loop(self):
        while not self._stop.wait(0.25):
            self.tick()

    def tick(self):
        p = self.progress
        if self.as_json:
            self.emit({"type": "progress", "phase": p.phase, "current": p.current,
                       "pass_no": p.pass_no, "pass_total": p.pass_total, "done": p.done,
                       "total": p.total, "files_done": p.files_done,
                       "files_total": p.files_total, "rate": p.rate})
        elif sys.stderr.isatty():
            with self._lock:
                sys.stderr.write(
                    f"\r\033[K{p.fraction * 100:5.1f}%  pass {p.pass_no}/{p.pass_total}  "
                    f"{system.human_size(p.done)}/{system.human_size(p.total)}  "
                    f"{system.human_size(p.rate)}/s  ETA {system.human_duration(p.eta)}")
                sys.stderr.flush()

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(1)
        self.tick()
        if not self.as_json and sys.stderr.isatty():
            sys.stderr.write("\n")


def install_cancel(token: engine.CancelToken, watch_stdin: bool):
    def handler(signum, frame):
        token.cancel()
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGHUP, handler)
    if watch_stdin:
        def watch():
            try:
                while sys.stdin.read(1024):
                    pass
            except (OSError, ValueError):
                pass
            token.cancel()
        threading.Thread(target=watch, daemon=True).start()


def notify(title: str, body: str):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-a", "Shreder", "-i", "io.github.shreder.Shreder", title, body],
                       capture_output=True)


# --------------------------------------------------------------------------- commands

def cmd_methods(args):
    for m in engine.METHODS.values():
        default = " (default)" if m.id == engine.DEFAULT_METHOD else ""
        print(f"{m.id:10} {m.name} — {m.short}{default}\n{'':10} {m.summary}")


def cmd_shred(args):
    targets = []
    for path in args.paths:
        reason = system.check_target(path, keep_root=args.keep_root)
        if reason:
            print(f"refusing {path}: {reason}", file=sys.stderr)
            return 2
        targets.append((path, args.keep_root))
    method = engine.get_method(args.method)
    plan = engine.build_plan(targets, older_than_days=args.older_than, include_hidden=not args.skip_hidden)
    if plan.shared and not args.json:
        n = plan.shared_count
        if args.shred_shared:
            print(f"{n} file{'s' if n != 1 else ''} share{'' if n != 1 else 's'} data with files outside the "
                  "selection (hard links): --shred-shared destroys those files too.", file=sys.stderr)
        else:
            print(f"{n} file{'s' if n != 1 else ''} share{'' if n != 1 else 's'} data with files outside the "
                  "selection (hard links): the names are removed, the data is kept "
                  "(--shred-shared overwrites it anyway).", file=sys.stderr)
    if not args.yes and not args.json:
        print(f"About to shred {plan.item_count} items ({system.human_size(plan.data_bytes)}) "
              f"with {method.name} ({method.short}). This cannot be undone.")
        if input("Type 'shred' to continue: ").strip().lower() != "shred":
            print("Aborted.")
            return 1
    token = engine.CancelToken()
    install_cancel(token, args.stdin_cancel)
    shredder = engine.Shredder(method, verify=args.verify, rename_passes=args.rename_passes, cancel=token,
                               shared="shred" if args.shred_shared else "keep")
    rep = Reporter(shredder.progress, args.json)
    shredder.log = rep.log
    try:
        with rep:
            stats = shredder.run(plan)
    except engine.Cancelled:
        rep.emit({"type": "cancelled"}) if args.json else print("Cancelled.", file=sys.stderr)
        return 130
    if args.json:
        rep.emit({"type": "done", "stats": stats.as_dict()})
    else:
        kept = f", {stats.shared_kept} kept (shared data)" if stats.shared_kept else ""
        print(f"Shredded {stats.files_shredded} files ({system.human_size(stats.bytes_data)}){kept}, "
              f"{len(stats.errors)} errors.")
    return 1 if stats.errors else 0


def cmd_wipe_free(args):
    directory = os.path.abspath(args.directory)
    method = engine.get_method(args.method)
    token = engine.CancelToken()
    install_cancel(token, args.stdin_cancel)
    shredder = engine.Shredder(method, cancel=token)
    rep = Reporter(shredder.progress, args.json)
    shredder.log = rep.log
    if not args.yes and not args.json:
        free = os.statvfs(directory)
        print(f"About to fill {system.human_size(free.f_bavail * free.f_frsize)} of free space on the drive "
              f"holding {directory} with {method.name} ({method.short}).")
        if input("Type 'wipe' to continue: ").strip().lower() != "wipe":
            return 1
    try:
        with rep:
            stats = shredder.wipe_free_space(directory)
    except engine.Cancelled:
        rep.emit({"type": "cancelled"}) if args.json else print("Cancelled.", file=sys.stderr)
        return 130
    except OSError as e:
        if args.json:
            rep.emit({"type": "error", "message": e.strerror or str(e)})
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1
    if args.json:
        rep.emit({"type": "done", "stats": stats.as_dict()})
    else:
        print(f"Overwrote {system.human_size(stats.bytes_data)} of free space "
              f"({system.human_size(stats.bytes_written)} written).")
    return 0


def cmd_run_scheduled(args):
    from shreder import jobs
    from shreder.store import DATA_DIR, Store
    store = Store()
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "scheduler.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another scheduled run is in progress", file=sys.stderr)
            return 0
        jobs.cleanup_stale_workdirs(store)
        due = store.due_folders()
        if not due:
            if not store.any_scheduled() and not args.keep_timer:
                system.set_scheduler(False)
            return 0
        settings = store.settings
        total_files = total_bytes = 0
        for folder in due:
            path = folder["path"]
            if not os.path.isdir(path) or system.check_target(path, keep_root=True):
                continue
            job = jobs.Job(kind="shred", title=f"Scheduled: {folder['label']}",
                           method_id=folder.get("method") or settings.get("method", engine.DEFAULT_METHOD),
                           targets=[(path, True)],  # scheduled runs always keep the folder
                           older_than_days=folder.get("older_than_days", 0),
                           include_hidden=folder.get("include_hidden", True),
                           verify=settings.get("verify", False),
                           rename_passes=settings.get("rename_passes", 3), folder_ids=[folder["id"]])
            jobs.execute_job(job, store)
            total_files += job.stats.files_shredded
            total_bytes += job.stats.bytes_data
            print(f"{folder['label']}: {job.state} — {job.summary()}")
        if total_files and settings.get("notify", True):
            notify("Scheduled shredding finished",
                   f"{total_files} files ({system.human_size(total_bytes)}) securely destroyed")
    return 0


def cmd_folders(args):
    from shreder.store import Store
    for f in Store().folders:
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(f["last_run"])) if f.get("last_run") else "never"
        print(f"{f['id']}  {f['label']:20} {f['path']}  schedule={f['schedule']}  last={last}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="shreder-cli", description="Secure file shredder and free-space wiper")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("methods", help="list overwrite methods").set_defaults(fn=cmd_methods)
    sub.add_parser("folders", help="list saved folders").set_defaults(fn=cmd_folders)

    s = sub.add_parser("shred", help="securely shred files or folders")
    s.add_argument("paths", nargs="+")
    s.add_argument("-m", "--method", default=engine.DEFAULT_METHOD, choices=list(engine.METHODS))
    s.add_argument("--verify", action="store_true", help="read back and verify the final pass")
    s.add_argument("--keep-root", action="store_true", help="shred folder contents, keep the folder")
    s.add_argument("--older-than", type=float, default=0, metavar="DAYS")
    s.add_argument("--skip-hidden", action="store_true")
    s.add_argument("--rename-passes", type=int, default=3)
    s.add_argument("--shred-shared", action="store_true",
                   help="overwrite a file even when its data also belongs to files outside the selection "
                        "(hard links), destroying those too; by default only the name is removed")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    s.add_argument("--json", action="store_true")
    s.add_argument("--stdin-cancel", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_shred)

    w = sub.add_parser("wipe-free", help="overwrite all free space on a drive")
    w.add_argument("directory", help="any folder on the drive to wipe")
    w.add_argument("-m", "--method", default=engine.DEFAULT_METHOD, choices=list(engine.METHODS))
    w.add_argument("-y", "--yes", action="store_true")
    w.add_argument("--json", action="store_true")
    w.add_argument("--stdin-cancel", action="store_true", help=argparse.SUPPRESS)
    w.set_defaults(fn=cmd_wipe_free)

    r = sub.add_parser("run-scheduled", help="shred saved folders whose schedule is due")
    r.add_argument("--keep-timer", action="store_true", help=argparse.SUPPRESS)
    r.set_defaults(fn=cmd_run_scheduled)

    args = ap.parse_args(argv)
    return args.fn(args) or 0


if __name__ == "__main__":
    sys.exit(main())
