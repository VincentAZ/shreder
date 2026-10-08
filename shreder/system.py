"""System helpers: volumes, drive types, path safety, privacy locations, scheduler."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass

HOME = os.path.expanduser("~")
UID = os.getuid()
PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_PATH = os.path.join(PROJECT_DIR, "shreder", "cli.py")

REAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "vfat", "exfat", "ntfs", "ntfs3",
           "fuseblk", "f2fs", "jfs", "reiserfs", "zfs", "bcachefs", "hfsplus", "nilfs2"}
COW_FS = {"btrfs", "zfs", "bcachefs", "nilfs2"}


# --------------------------------------------------------------------------- formatting

def human_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(n) < 1000 or unit == "PB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}" if n < 100 else f"{n:.0f} {unit}"
        n /= 1000
    return f"{n:.0f} PB"


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def short_path(path: str) -> str:
    if path == HOME:
        return "~"
    if path.startswith(HOME + os.sep):
        return "~" + path[len(HOME):]
    return path


# --------------------------------------------------------------------------- safety

_PROTECTED_TREES = ["/proc", "/sys", "/dev", "/boot", "/usr", "/etc", "/bin", "/sbin",
                    "/lib", "/lib32", "/lib64", "/libx32", "/snap", "/run", "/var/lib", "/var/snap"]
_PROTECTED_EXACT = {"/", "/home", "/root", "/var", "/opt", "/srv", "/mnt", "/media", "/tmp",
                    HOME, os.path.join(HOME, ".config"), os.path.join(HOME, ".local"),
                    os.path.join(HOME, ".local", "share"), os.path.join(HOME, ".cache"),
                    os.path.join(HOME, "snap")}


def check_target(path: str, keep_root: bool = False) -> str | None:
    """Return a reason string if `path` must not be shredded, else None."""
    try:
        path = os.path.abspath(path)
        parent = os.path.realpath(os.path.dirname(path))
        real = os.path.join(parent, os.path.basename(path)) if os.path.basename(path) else parent
    except OSError:
        return "Path cannot be resolved"
    for p in {path, real}:
        if p in _PROTECTED_EXACT:
            return f"{short_path(p)} is a protected system or home folder"
        for tree in _PROTECTED_TREES:
            if p == tree or p.startswith(tree + os.sep):
                if p.startswith(f"/run/media/") or p.startswith(f"/run/user/{UID}/gvfs"):
                    continue
                return f"{p} is inside a protected system location"
    from . import store  # local import avoids a cycle
    for own in (store.CONFIG_DIR, store.DATA_DIR):
        if real == own or real.startswith(own + os.sep) or own.startswith(real + os.sep):
            return "That location contains Shreder's own settings"
    if not os.path.lexists(path):
        return "Path no longer exists"
    if not keep_root and _is_mountpoint(real):
        return f"{real} is a mounted drive — use 'Shred contents' to keep the mount point"
    return None


def _is_mountpoint(path: str) -> bool:
    try:
        return os.path.ismount(path) and not os.path.islink(path)
    except OSError:
        return False


# --------------------------------------------------------------------------- sizes

def path_size(path: str, include_hidden: bool = True, limit_seconds: float = 20.0):
    """(bytes, files) for a file or tree, without following symlinks."""
    import time
    deadline = time.monotonic() + limit_seconds
    try:
        st = os.lstat(path)
    except OSError:
        return 0, 0
    if not stat.S_ISDIR(st.st_mode):
        return (st.st_size if stat.S_ISREG(st.st_mode) else 0), 1
    total = files = 0
    stack = [path]
    while stack:
        if time.monotonic() > deadline:
            break
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if not include_hidden and e.name.startswith("."):
                        continue
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        if s.st_dev == st.st_dev:
                            stack.append(e.path)
                    else:
                        files += 1
                        if stat.S_ISREG(s.st_mode):
                            total += s.st_size
        except OSError:
            continue
    return total, files


# --------------------------------------------------------------------------- volumes

@dataclass
class Volume:
    device: str
    mountpoint: str
    fstype: str
    label: str
    model: str
    total: int
    free: int          # free for an unprivileged user
    free_root: int     # including root-reserved blocks
    rotational: bool | None
    removable: bool
    transport: str
    work_dir: str | None   # where an unprivileged wipe can write

    @property
    def name(self) -> str:
        if self.mountpoint == "/":
            return "System Drive"
        if self.label:
            return self.label
        return os.path.basename(self.mountpoint) or self.mountpoint

    @property
    def is_ssd(self) -> bool:
        return self.rotational is False

    @property
    def is_cow(self) -> bool:
        return self.fstype in COW_FS

    @property
    def kind(self) -> str:
        if self.transport == "usb" or self.removable:
            return "USB"
        if self.transport == "nvme":
            return "NVMe SSD"
        if self.rotational is False:
            return "SSD"
        if self.rotational:
            return "HDD"
        return "Disk"

    @property
    def icon(self) -> str:
        if self.transport == "usb" or self.removable:
            return "drive-removable-media-symbolic"
        return "drive-harddisk-symbolic"


def _lsblk_index() -> dict:
    index: dict = {}
    try:
        out = subprocess.run(
            ["lsblk", "-J", "-o", "PATH,MAJ:MIN,LABEL,MODEL,ROTA,TRAN,RM"],
            capture_output=True, text=True, timeout=5).stdout
        data = json.loads(out or "{}")
    except (OSError, ValueError, subprocess.SubprocessError):
        return index

    def boolish(v):
        if isinstance(v, bool):
            return v
        if v in ("1", 1):
            return True
        if v in ("0", 0):
            return False
        return None

    def visit(dev, parent):
        info = {
            "path": dev.get("path") or "",
            "label": dev.get("label") or "",
            "model": (dev.get("model") or (parent or {}).get("model") or "").strip(),
            "rota": boolish(dev.get("rota")),
            "tran": dev.get("tran") or (parent or {}).get("tran") or "",
            "rm": bool(boolish(dev.get("rm")) or (parent or {}).get("rm")),
        }
        index[dev.get("maj:min", "")] = info
        for child in dev.get("children", []) or []:
            visit(child, info)

    for dev in data.get("blockdevices", []):
        visit(dev, None)
    return index


def _unescape_mount(s: str) -> str:
    return s.encode().decode("unicode_escape").encode("latin-1").decode("utf-8", "replace") if "\\" in s else s


def list_volumes() -> list[Volume]:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    blk = _lsblk_index()
    by_dev: dict = {}
    home_dev = _dev(HOME)
    for line in lines:
        parts = line.split()
        try:
            sep = parts.index("-")
        except ValueError:
            continue
        majmin, root, mountpoint = parts[2], parts[3], _unescape_mount(parts[4])
        fstype, source = parts[sep + 1], parts[sep + 2]
        if fstype not in REAL_FS or root != "/":
            continue
        if mountpoint.startswith(("/snap/", "/var/snap/", "/proc", "/sys")):
            continue
        if mountpoint.startswith("/boot") and mountpoint != "/boot":
            continue  # EFI partitions are not something to wipe
        prev = by_dev.get(majmin)
        if prev and len(prev[0]) <= len(mountpoint):
            continue
        by_dev[majmin] = (mountpoint, fstype, source)

    volumes = []
    for majmin, (mountpoint, fstype, source) in by_dev.items():
        if mountpoint in ("/boot",):
            continue
        try:
            vfs = os.statvfs(mountpoint)
        except OSError:
            continue
        info = blk.get(majmin, {})
        device = info.get("path") or source
        dev_num = _dev(mountpoint)
        work = None
        if os.access(mountpoint, os.W_OK | os.X_OK):
            work = mountpoint
        elif home_dev is not None and home_dev == dev_num:
            work = HOME
        else:
            for cand in ("/var/tmp", "/tmp"):
                if _dev(cand) == dev_num and os.access(cand, os.W_OK):
                    work = cand
                    break
        rota = info.get("rota")
        if rota is None:
            rota = _sys_rotational(majmin)
        volumes.append(Volume(
            device=device, mountpoint=mountpoint, fstype=fstype,
            label=info.get("label", ""), model=info.get("model", ""),
            total=vfs.f_blocks * vfs.f_frsize, free=vfs.f_bavail * vfs.f_frsize,
            free_root=vfs.f_bfree * vfs.f_frsize, rotational=rota,
            removable=info.get("rm", False), transport=info.get("tran", ""), work_dir=work))
    volumes.sort(key=lambda v: (v.mountpoint != "/", v.mountpoint))
    return volumes


def volume_for(path: str, volumes) -> Volume | None:
    """The mounted volume holding `path` (longest matching mount point)."""
    path = os.path.abspath(path)
    best = None
    for v in volumes or []:
        mp = v.mountpoint.rstrip("/") or "/"
        if path == mp or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best.mountpoint):
                best = v
    return best


def estimate_seconds(nbytes: float, ssd: bool) -> float:
    """Rough wall-clock estimate for writing nbytes (random data is the bottleneck)."""
    return nbytes / (450e6 if ssd else 120e6)


def _dev(path: str):
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _sys_rotational(majmin: str):
    try:
        with open(f"/sys/dev/block/{majmin}/queue/rotational") as f:
            return f.read().strip() == "1"
    except OSError:
        return None


# --------------------------------------------------------------------------- privacy locations

def trash_dirs() -> list[str]:
    dirs = []
    base = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share")), "Trash")
    for sub in ("files", "info", "expunged"):
        p = os.path.join(base, sub)
        if os.path.isdir(p):
            dirs.append(p)
    for vol in list_volumes():
        for name in (f".Trash-{UID}", os.path.join(".Trash", str(UID))):
            p = os.path.join(vol.mountpoint, name)
            for sub in ("files", "info"):
                q = os.path.join(p, sub)
                if os.path.isdir(q) and os.access(q, os.W_OK):
                    dirs.append(q)
    return dirs


def thumbnail_dirs() -> list[str]:
    cache = os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache"))
    return [p for p in (os.path.join(cache, "thumbnails"),) if os.path.isdir(p)]


def recent_files() -> list[str]:
    data = os.environ.get("XDG_DATA_HOME", os.path.join(HOME, ".local", "share"))
    return [p for p in (os.path.join(data, "recently-used.xbel"),) if os.path.isfile(p)]


def dirs_size(paths: list[str]):
    total = files = 0
    for p in paths:
        b, n = path_size(p)
        total += b
        files += n
    return total, files


# --------------------------------------------------------------------------- scheduler (systemd --user)

SYSTEMD_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")),
                           "systemd", "user")
TIMER = "shreder-scheduler.timer"


def scheduler_units() -> dict[str, str]:
    service = f"""[Unit]
Description=Shreder scheduled folder shredding

[Service]
Type=oneshot
Nice=10
IOSchedulingClass=idle
ExecStart={sys.executable} -I {CLI_PATH} run-scheduled
"""
    timer = """[Unit]
Description=Run Shreder scheduled shredding hourly

[Timer]
OnCalendar=hourly
OnStartupSec=5min
Persistent=true

[Install]
WantedBy=timers.target
"""
    return {"shreder-scheduler.service": service, TIMER: timer}


def set_scheduler(enabled: bool) -> str | None:
    """Install/enable or disable the user timer. Returns an error string or None."""
    if not shutil.which("systemctl"):
        return "systemd is not available"
    try:
        if enabled:
            os.makedirs(SYSTEMD_DIR, exist_ok=True)
            for name, text in scheduler_units().items():
                path = os.path.join(SYSTEMD_DIR, name)
                try:
                    with open(path) as f:
                        if f.read() == text:
                            continue
                except OSError:
                    pass
                with open(path, "w") as f:
                    f.write(text)
            subprocess.run(["systemctl", "--user", "daemon-reload"], timeout=15, capture_output=True)
            r = subprocess.run(["systemctl", "--user", "enable", "--now", TIMER],
                               timeout=15, capture_output=True, text=True)
        else:
            if not os.path.exists(os.path.join(SYSTEMD_DIR, TIMER)):
                return None
            r = subprocess.run(["systemctl", "--user", "disable", "--now", TIMER],
                               timeout=15, capture_output=True, text=True)
        if r.returncode != 0:
            return (r.stderr or "systemctl failed").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    return None


def scheduler_active() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", TIMER],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- desktop integration

NAUTILUS_SCRIPT = os.path.join(HOME, ".local", "share", "nautilus", "scripts", "Shred with Shreder")
LAUNCHER = os.path.join(PROJECT_DIR, "bin", "shreder")


def file_manager_action_installed() -> bool:
    return os.path.exists(NAUTILUS_SCRIPT)


def install_file_manager_action(enable: bool) -> str | None:
    try:
        if enable:
            os.makedirs(os.path.dirname(NAUTILUS_SCRIPT), exist_ok=True)
            with open(NAUTILUS_SCRIPT, "w") as f:
                f.write("#!/bin/bash\n"
                        "# Added by Shreder: queue the selected files for secure shredding\n"
                        "IFS=$'\\n' read -r -d '' -a files <<< \"$NAUTILUS_SCRIPT_SELECTED_FILE_PATHS\"\n"
                        f"exec \"{LAUNCHER}\" \"${{files[@]}}\"\n")
            os.chmod(NAUTILUS_SCRIPT, 0o755)
        elif os.path.exists(NAUTILUS_SCRIPT):
            os.unlink(NAUTILUS_SCRIPT)
    except OSError as e:
        return str(e)
    return None
