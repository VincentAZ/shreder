"""Secure overwrite engine: file shredding and free-space wiping.

Pure Python, no GUI dependencies, so it is shared by the desktop app and the CLI.
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import stat
import string
import threading
import time
from dataclasses import dataclass, field

CHUNK = 1 << 20  # 1 MiB writes
NAME_CHARS = string.ascii_letters + string.digits
WIPE_DIR_PREFIX = ".shreder-wipe-"

RANDOM = None  # a pass pattern of None means cryptographically random data


class Cancelled(Exception):
    """Raised inside the engine when the user cancels."""


class VerifyError(OSError):
    pass


class CancelToken:
    def __init__(self):
        self._event = threading.Event()

    def cancel(self):
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self):
        if self._event.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------- methods

@dataclass(frozen=True)
class Method:
    id: str
    name: str
    short: str
    passes: tuple
    summary: str

    @property
    def pass_count(self) -> int:
        return len(self.passes)

    def describe_passes(self) -> list[str]:
        return [describe_pattern(p) for p in self.passes]


def describe_pattern(pattern) -> str:
    if pattern is RANDOM:
        return "random data"
    return " ".join(f"0x{b:02X}" for b in pattern)


def _gutmann_passes() -> tuple:
    passes = [RANDOM] * 4
    passes += [b"\x55", b"\xaa", b"\x92\x49\x24", b"\x49\x24\x92", b"\x24\x92\x49"]
    passes += [bytes([i * 0x11]) for i in range(16)]
    passes += [b"\x92\x49\x24", b"\x49\x24\x92", b"\x24\x92\x49",
               b"\x6d\xb6\xdb", b"\xb6\xdb\x6d", b"\xdb\x6d\xb6"]
    passes += [RANDOM] * 4
    return tuple(passes)


METHODS: dict[str, Method] = {m.id: m for m in (
    Method("zero", "Zero Fill", "1 pass", (b"\x00",),
           "Overwrites once with zeros. Fast; enough for modern drives against software recovery."),
    Method("random", "Random Data", "1 pass", (RANDOM,),
           "Overwrites once with cryptographically random data (NIST 800-88 Clear equivalent)."),
    Method("dod3", "DoD 5220.22-M (E)", "3 passes", (b"\x00", b"\xff", RANDOM),
           "US DoD standard: zeros, ones, then random data. The recommended default."),
    Method("dod7", "DoD 5220.22-M (ECE)", "7 passes",
           (b"\x00", b"\xff", RANDOM, RANDOM, b"\x00", b"\xff", RANDOM),
           "Extended DoD standard: the 3-pass sequence, a random pass, then the 3-pass sequence again."),
    Method("schneier", "Bruce Schneier", "7 passes", (b"\xff", b"\x00") + (RANDOM,) * 5,
           "Ones, zeros, then five passes of random data."),
    Method("gutmann", "Peter Gutmann", "35 passes", _gutmann_passes(),
           "35 passes designed for legacy magnetic encodings. Very slow; overkill for modern disks."),
)}

DEFAULT_METHOD = "dod3"


def get_method(method_id: str) -> Method:
    return METHODS.get(method_id) or METHODS[DEFAULT_METHOD]


# --------------------------------------------------------------------------- progress

@dataclass
class Progress:
    phase: str = "preparing"     # preparing | scanning | overwriting | verifying | cleanup | done
    current: str = ""
    pass_no: int = 0
    pass_total: int = 0
    done: int = 0
    total: int = 0
    files_done: int = 0
    files_total: int = 0
    rate: float = 0.0            # bytes / second
    started: float = field(default_factory=time.monotonic)
    _mark_time: float = field(default_factory=time.monotonic)
    _mark_done: int = 0

    def advance(self, n: int):
        self.done += n
        now = time.monotonic()
        dt = now - self._mark_time
        if dt >= 0.5:
            inst = (self.done - self._mark_done) / dt
            self.rate = inst if self.rate == 0 else self.rate * 0.6 + inst * 0.4
            self._mark_time, self._mark_done = now, self.done

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.total))

    @property
    def eta(self) -> float | None:
        if self.rate <= 0 or self.total <= 0:
            return None
        return max(0.0, (self.total - self.done) / self.rate)


@dataclass
class Stats:
    files_shredded: int = 0
    files_failed: int = 0
    shared_kept: int = 0         # names removed without overwriting: their data belongs to files elsewhere too
    links_removed: int = 0
    dirs_removed: int = 0
    bytes_data: int = 0          # logical size of destroyed data
    bytes_written: int = 0       # everything written, all passes
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "files_shredded": self.files_shredded,
            "files_failed": self.files_failed,
            "shared_kept": self.shared_kept,
            "links_removed": self.links_removed,
            "dirs_removed": self.dirs_removed,
            "bytes_data": self.bytes_data,
            "bytes_written": self.bytes_written,
            "errors": self.errors[:200],
            "warnings": self.warnings[:200],
        }


# --------------------------------------------------------------------------- planning

@dataclass
class Plan:
    files: list = field(default_factory=list)    # [(path, size)]
    links: list = field(default_factory=list)    # symlinks: unlinked, targets untouched
    dirs: list = field(default_factory=list)     # [(path, mtime)] deepest first
    skipped: list = field(default_factory=list)  # [(path, reason)]
    # Hard links: one file's data can have several names. aliases[path] lists the other names inside the
    # selection (removed with it); shared[path] counts the names outside it, whose data must survive.
    aliases: dict = field(default_factory=dict)
    shared: dict = field(default_factory=dict)

    @property
    def data_bytes(self) -> int:
        return sum(size for _, size in self.files)

    @property
    def shared_count(self) -> int:
        return len(self.shared)

    @property
    def item_count(self) -> int:
        return len(self.files) + len(self.links)


def build_plan(targets, *, older_than_days: float = 0, include_hidden: bool = True,
               cancel: CancelToken | None = None) -> Plan:
    """targets: iterable of (path, keep_root). keep_root=True shreds a folder's
    contents but leaves the folder itself in place."""
    plan = Plan()
    cutoff = time.time() - older_than_days * 86400 if older_than_days else None
    seen: dict = {}      # (dev, inode) -> the first path planned for it, or None when it was filtered out
    nlinks: dict = {}    # first path -> how many names the file has on disk

    def old_enough(st) -> bool:
        return cutoff is None or st.st_mtime < cutoff

    def add_entry(path: str, st, root_dev: int):
        key = (st.st_dev, st.st_ino)
        if stat.S_ISLNK(st.st_mode):
            if old_enough(st):
                plan.links.append(path)
        elif stat.S_ISREG(st.st_mode):
            if key in seen:
                first = seen[key]
                if first is not None and old_enough(st):
                    plan.aliases.setdefault(first, []).append(path)
                return
            if old_enough(st):
                seen[key] = path
                plan.files.append((path, st.st_size))
                if st.st_nlink > 1:
                    nlinks[path] = st.st_nlink
            else:
                seen[key] = None
        elif stat.S_ISDIR(st.st_mode):
            walk(path, st, root_dev, keep=False)
        else:
            plan.skipped.append((path, "special file (device, socket or pipe)"))

    def walk(top: str, top_st, root_dev: int, keep: bool):
        stack = [(top, top_st)]
        order = []
        while stack:
            if cancel:
                cancel.check()
            d, dst = stack.pop()
            if d != top or not keep:
                order.append((d, dst.st_mtime))
            try:
                entries = list(os.scandir(d))
            except OSError as e:
                plan.skipped.append((d, e.strerror or str(e)))
                continue
            for entry in entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as e:
                    plan.skipped.append((entry.path, e.strerror or str(e)))
                    continue
                if stat.S_ISDIR(st.st_mode):
                    if st.st_dev != root_dev:
                        plan.skipped.append((entry.path, "another filesystem is mounted here"))
                        continue
                    stack.append((entry.path, st))
                else:
                    add_entry(entry.path, st, root_dev)
        # remove deepest directories first
        order.sort(key=lambda item: item[0].count(os.sep), reverse=True)
        for d, mtime in order:
            if cutoff is None or mtime < cutoff:
                plan.dirs.append((d, mtime))

    for path, keep_root in targets:
        path = os.path.abspath(path)
        try:
            st = os.lstat(path)
        except OSError as e:
            plan.skipped.append((path, e.strerror or str(e)))
            continue
        if stat.S_ISDIR(st.st_mode):
            walk(path, st, st.st_dev, keep=keep_root)
        else:
            add_entry(path, st, st.st_dev)
    for path, n in nlinks.items():
        outside = n - 1 - len(plan.aliases.get(path, ()))
        if outside > 0:
            plan.shared[path] = outside
    return plan


# --------------------------------------------------------------------------- shredder

def _sync_dir(path: str):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class Shredder:
    def __init__(self, method: Method, *, verify: bool = False, rename_passes: int = 3,
                 cancel: CancelToken | None = None, progress: Progress | None = None,
                 log=None, shared: str = "keep"):
        """shared: what to do with a file whose data also belongs to names outside the plan (hard links).
        "keep" (the default) removes the name and leaves the data, since overwriting it would destroy
        those other files too; "shred" overwrites it anyway."""
        self.method = method
        self.passes = method.passes
        self.verify = verify
        self.shared = shared
        self.rename_passes = max(0, int(rename_passes))
        self.cancel = cancel or CancelToken()
        self.progress = progress or Progress()
        self.log = log or (lambda level, message: None)
        self.stats = Stats()
        self._patterns: dict[bytes, memoryview] = {}

    # ----------------------------------------------------------------- helpers
    def _error(self, path: str, err):
        msg = f"{path}: {getattr(err, 'strerror', None) or err}"
        self.stats.errors.append(msg)
        self.log("error", msg)

    def _warn(self, msg: str):
        self.stats.warnings.append(msg)
        self.log("warning", msg)

    def _advance(self, n: int):
        self.stats.bytes_written += n
        self.progress.advance(n)

    def _chunk(self, pattern, offset: int, n: int):
        if pattern is RANDOM:
            return os.urandom(n)
        rep = self._patterns.get(pattern)
        if rep is None:
            rep = memoryview(pattern * (CHUNK // len(pattern) + 2))
            self._patterns[pattern] = rep
        phase = offset % len(pattern)
        return rep[phase:phase + n]

    @staticmethod
    def _write_all(fd: int, buf):
        view = memoryview(buf)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "write made no progress")
            view = view[written:]

    def _open_rw(self, path: str) -> int:
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(path, flags)
        except PermissionError:
            st = os.lstat(path)
            if st.st_uid != os.geteuid() and os.geteuid() != 0:
                raise
            os.chmod(path, stat.S_IMODE(st.st_mode) | stat.S_IRUSR | stat.S_IWUSR)
            return os.open(path, flags)

    def _obfuscate(self, path: str) -> str:
        """Rename an entry through random names of decreasing length so the
        original file name does not survive in the directory."""
        parent, name = os.path.split(path)
        current = path
        length = min(max(len(name), 1), 255)
        for k in range(self.rename_passes):
            size = max(1, length - k)
            for _ in range(16):
                candidate = os.path.join(parent, "".join(secrets.choice(NAME_CHARS) for _ in range(size)))
                if not os.path.lexists(candidate):
                    break
            else:
                continue
            try:
                os.rename(current, candidate)
            except OSError:
                break
            current = candidate
        if current != path:
            _sync_dir(parent)
        return current

    # ----------------------------------------------------------------- passes
    def _write_pass(self, fd: int, length: int, pattern, keep_digests: bool):
        os.lseek(fd, 0, os.SEEK_SET)
        digests = [] if keep_digests else None
        offset = 0
        while offset < length:
            self.cancel.check()
            n = min(CHUNK, length - offset)
            buf = self._chunk(pattern, offset, n)
            self._write_all(fd, buf)
            if digests is not None:
                digests.append(hashlib.blake2b(buf, digest_size=16).digest())
            offset += n
            self._advance(n)
        os.fsync(fd)
        return digests

    def _verify(self, fd: int, path: str, length: int, pattern, digests):
        self.progress.phase = "verifying"
        try:  # drop cached pages so the read-back comes from the disk
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        os.lseek(fd, 0, os.SEEK_SET)
        offset = index = 0
        while offset < length:
            self.cancel.check()
            n = min(CHUNK, length - offset)
            data = bytearray()
            while len(data) < n:
                part = os.read(fd, n - len(data))
                if not part:
                    break
                data += part
            if pattern is RANDOM:
                ok = hashlib.blake2b(data, digest_size=16).digest() == digests[index]
            else:
                ok = data == self._chunk(pattern, offset, n)
            if not ok:
                raise VerifyError(errno.EIO, f"verification failed at byte {offset}")
            offset += n
            index += 1
            self.progress.advance(n)
        self.progress.phase = "overwriting"

    # ----------------------------------------------------------------- files
    def shred_file(self, path: str):
        parent = os.path.dirname(path) or "."
        if not os.access(parent, os.W_OK | os.X_OK):
            raise PermissionError(errno.EACCES, "the containing folder is read-only")
        fd = self._open_rw(path)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            block = st.st_blksize or 4096
            length = -(-st.st_size // block) * block  # cover the slack in the last block
            n_passes = len(self.passes)
            digests = None
            for i, pattern in enumerate(self.passes, 1):
                self.progress.pass_no, self.progress.pass_total = i, n_passes
                keep = self.verify and i == n_passes and pattern is RANDOM
                digests = self._write_pass(fd, length, pattern, keep)
            if self.verify and length:
                self._verify(fd, path, length, self.passes[-1], digests)
            os.ftruncate(fd, 0)
            os.fsync(fd)
        finally:
            os.close(fd)
        final = self._obfuscate(path)
        os.unlink(final)
        _sync_dir(parent)
        self.stats.bytes_data += st.st_size
        return length

    def _remove_name(self, path: str):
        """Remove one name of a file without touching its data (another name keeps it, or it's gone already)."""
        parent = os.path.dirname(path) or "."
        final = self._obfuscate(path)
        os.unlink(final)
        _sync_dir(parent)

    def _remove_link(self, path: str):
        final = self._obfuscate(path)
        os.unlink(final)
        self.stats.links_removed += 1

    def _remove_dir(self, path: str):
        try:
            with os.scandir(path) as it:
                if any(True for _ in it):
                    return  # something was kept (filters / errors): leave the folder alone
        except OSError:
            return
        final = self._obfuscate(path)
        try:
            os.rmdir(final)
            self.stats.dirs_removed += 1
        except OSError as e:
            if final != path:
                try:
                    os.rename(final, path)
                except OSError:
                    pass
            if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                self._error(path, e)
        _sync_dir(os.path.dirname(path))

    def run(self, plan: Plan):
        p = self.progress
        n_passes = len(self.passes)
        block = 4096
        rounded = [(path, -(-size // block) * block) for path, size in plan.files]
        p.total = sum(size for _, size in rounded) * (n_passes + (1 if self.verify else 0))
        p.files_total = plan.item_count
        p.pass_total = n_passes
        p.phase = "overwriting"
        for path, reason in plan.skipped:
            self._warn(f"{path}: skipped ({reason})")
        for path, expected in rounded:
            self.cancel.check()
            p.current = path
            base = p.done
            try:
                outside = plan.shared.get(path, 0)
                if outside and self.shared != "shred":
                    # Overwriting would destroy the other files that share this data: only the name goes.
                    self._remove_name(path)
                    self.stats.shared_kept += 1
                    self._warn(f"{path}: not overwritten, its data also belongs to {outside} other "
                               f"file{'s' if outside != 1 else ''} outside the selection (hard links); "
                               "the name was removed, the data kept")
                else:
                    if outside:
                        self._warn(f"{path}: overwritten although {outside} other file{'s' if outside != 1 else ''} "
                                   "outside the selection shared its data (hard links); they are destroyed too")
                    self.shred_file(path)
                    self.stats.files_shredded += 1
                    for alias in plan.aliases.get(path, ()):  # the same data under other names in the selection
                        try:
                            self._remove_name(alias)
                        except FileNotFoundError:
                            pass
                        except OSError as e:
                            self._error(alias, e)
            except Cancelled:
                raise
            except FileNotFoundError:
                self._warn(f"{path}: already gone")
            except OSError as e:
                self.stats.files_failed += 1
                self._error(path, e)
            # keep the bar honest even if the file changed size since scanning
            p.done = base + expected * (n_passes + (1 if self.verify else 0))
            p.files_done += 1
        p.phase = "cleanup"
        for path in plan.links:
            self.cancel.check()
            try:
                self._remove_link(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                self._error(path, e)
            p.files_done += 1
        for path, _ in plan.dirs:
            self.cancel.check()
            self._remove_dir(path)
        p.phase = "done"
        p.current = ""
        return self.stats

    # ----------------------------------------------------------------- free space
    def wipe_free_space(self, directory: str, on_workdir=None):
        """Fill all free space on the filesystem holding `directory` with each
        pass pattern, then delete the fill files. on_workdir(path, active) is
        called so callers can record the temp folder for crash recovery."""
        p = self.progress
        vfs = os.statvfs(directory)
        blocks = vfs.f_bfree if os.geteuid() == 0 else vfs.f_bavail
        free = blocks * vfs.f_frsize
        n_passes = len(self.passes)
        p.total = max(free, 1) * n_passes
        p.pass_total = n_passes
        p.phase = "overwriting"
        workdir = os.path.join(directory, WIPE_DIR_PREFIX + secrets.token_hex(4))
        os.mkdir(workdir, 0o700)
        if on_workdir:
            on_workdir(workdir, True)
        files: list = []
        try:
            for i, pattern in enumerate(self.passes, 1):
                p.pass_no = i
                if i == 1:
                    self._fill(workdir, pattern, files)
                    filled = sum(size for _, size in files)
                    p.total = max(filled, 1) * n_passes
                    p.done = filled
                else:
                    target = p.done + sum(size for _, size in files)
                    self._refill(files, pattern)
                    p.done = target
            self.stats.bytes_data = sum(size for _, size in files)
        finally:
            p.phase = "cleanup"
            p.current = "Removing fill files"
            remove_wipe_dir(workdir)
            if on_workdir:
                on_workdir(workdir, False)
        p.phase = "done"
        p.current = ""
        return self.stats

    def _fill(self, workdir: str, pattern, files: list):
        index = 0
        full = False
        # big files until the disk is full; then small files for leftover fragments
        while not full:
            self.cancel.check()
            path = os.path.join(workdir, f"fill-{index:05d}")
            index += 1
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            written = 0
            try:
                self.progress.current = f"Filling free space ({os.path.basename(path)})"
                while True:
                    self.cancel.check()
                    buf = self._chunk(pattern, written, CHUNK)
                    try:
                        n = os.write(fd, buf)
                    except OSError as e:
                        if e.errno in (errno.ENOSPC, errno.EDQUOT):
                            full = True
                            break
                        if e.errno == errno.EFBIG:  # e.g. FAT32's 4 GiB limit: start a new file
                            break
                        raise
                    if n <= 0:
                        full = True
                        break
                    written += n
                    self._advance(n)
                try:
                    os.fsync(fd)
                except OSError as e:
                    if e.errno not in (errno.ENOSPC, errno.EDQUOT):
                        raise
            finally:
                os.close(fd)
            files.append((path, written))
            if written == 0:
                break
        # mop up partially free blocks with small files
        for k in range(512):
            self.cancel.check()
            path = os.path.join(workdir, f"tail-{k:04d}")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
            except OSError:
                break
            written = 0
            try:
                written = os.write(fd, self._chunk(pattern, 0, 4096))
                os.fsync(fd)
            except OSError:
                pass
            finally:
                os.close(fd)
            files.append((path, written))
            if written <= 0:
                break
            self._advance(written)

    def _refill(self, files: list, pattern):
        for path, size in files:
            self.cancel.check()
            if size <= 0:
                continue
            self.progress.current = f"Overwriting free space ({os.path.basename(path)})"
            fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                offset = 0
                while offset < size:
                    self.cancel.check()
                    n = min(CHUNK, size - offset)
                    try:
                        self._write_all(fd, self._chunk(pattern, offset, n))
                    except OSError as e:
                        if e.errno in (errno.ENOSPC, errno.EDQUOT):
                            self._warn("Filesystem could not overwrite in place (copy-on-write?); "
                                       "pass ended early")
                            return
                        raise
                    offset += n
                    self._advance(n)
                os.fsync(fd)
            finally:
                os.close(fd)


def remove_wipe_dir(workdir: str):
    """Delete a free-space fill folder (fill data is junk, plain unlink is fine)."""
    if not os.path.basename(workdir.rstrip("/")).startswith(WIPE_DIR_PREFIX):
        return
    try:
        for entry in os.scandir(workdir):
            try:
                os.unlink(entry.path)
            except OSError:
                pass
        os.rmdir(workdir)
    except FileNotFoundError:
        pass
    except OSError:
        pass
    _sync_dir(os.path.dirname(workdir))
