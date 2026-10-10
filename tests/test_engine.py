import errno
import os
import sys
import tempfile
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp_home = tempfile.mkdtemp(prefix="shreder-test-xdg-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_tmp_home, "config")
os.environ["XDG_DATA_HOME"] = os.path.join(_tmp_home, "data")

from shreder import engine, jobs, system  # noqa: E402
from shreder.store import Store  # noqa: E402


def write(path, data: bytes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="shreder-test-")

    def capture_overwrite(self, method_id, data, verify=False):
        """Shred a file but keep a hard link so we can inspect what was written."""
        path = os.path.join(self.dir, "secret.txt")
        write(path, data)
        spy = os.path.join(self.dir, "spy")
        os.link(path, spy)
        real_trunc = engine.os.ftruncate
        engine.os.ftruncate = lambda fd, n: None
        try:
            sh = engine.Shredder(engine.METHODS[method_id], verify=verify, shared="shred")
            sh.run(engine.build_plan([(path, False)]))
        finally:
            engine.os.ftruncate = real_trunc
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(sh.stats.warnings), 1)  # it said the spy's data went too
        with open(spy, "rb") as f:
            return f.read(), sh

    def test_all_methods_destroy_content(self):
        secret = b"TOP SECRET " * 1000
        for mid, method in engine.METHODS.items():
            with self.subTest(method=mid):
                out, sh = self.capture_overwrite(mid, secret)
                os.unlink(os.path.join(self.dir, "spy"))
                self.assertNotIn(b"TOP SECRET", out)
                self.assertEqual(len(out) % 4096, 0)
                self.assertGreaterEqual(len(out), len(secret))
                last = method.passes[-1]
                if last is not None:
                    self.assertEqual(out, (last * len(out))[:len(out)])
                self.assertEqual(sh.stats.files_shredded, 1)
                self.assertEqual(sh.stats.bytes_written, len(out) * method.pass_count)

    def test_gutmann_has_35_passes(self):
        self.assertEqual(engine.METHODS["gutmann"].pass_count, 35)
        self.assertEqual(engine.METHODS["dod7"].pass_count, 7)

    def test_verify_random_and_pattern(self):
        for mid in ("dod3", "zero"):
            out, sh = self.capture_overwrite(mid, os.urandom(3 * engine.CHUNK + 17), verify=True)
            os.unlink(os.path.join(self.dir, "spy"))
            self.assertEqual(sh.stats.errors, [])

    def test_tree_keep_root_symlink_hidden(self):
        root = os.path.join(self.dir, "box")
        write(os.path.join(root, "a.txt"), b"a" * 10)
        write(os.path.join(root, "sub", "deep", "b.bin"), b"b" * 5000)
        write(os.path.join(root, ".hidden"), b"h")
        outside = os.path.join(self.dir, "outside.txt")
        write(outside, b"keep me")
        os.symlink(outside, os.path.join(root, "link"))
        os.symlink(self.dir, os.path.join(root, "dirlink"))
        os.mkfifo(os.path.join(root, "pipe"))

        plan = engine.build_plan([(root, True)], include_hidden=False)
        self.assertEqual(len(plan.files), 2)
        sh = engine.Shredder(engine.METHODS["zero"])
        sh.run(plan)
        self.assertTrue(os.path.isdir(root))
        self.assertEqual(sorted(os.listdir(root)), [".hidden", "pipe"])
        with open(outside, "rb") as f:
            self.assertEqual(f.read(), b"keep me")
        self.assertEqual(sh.stats.links_removed, 2)
        self.assertEqual(sh.stats.dirs_removed, 2)

        # whole folder, including hidden files (the fifo stays, so the folder stays)
        os.unlink(os.path.join(root, "pipe"))
        sh = engine.Shredder(engine.METHODS["random"])
        sh.run(engine.build_plan([(root, False)]))
        self.assertFalse(os.path.exists(root))

    def test_hard_link_outside_selection_keeps_data(self):
        """A trashed copy of a file that still has a name elsewhere: the name goes, the data must not.
        (2026-10-09: emptying the Trash securely zeroed Python packages hard-linked into live venvs.)"""
        keep = os.path.join(self.dir, "venv", "lib", "threadpoolctl.py")
        write(keep, b"class ThreadpoolController: pass\n" * 100)
        trash = os.path.join(self.dir, "Trash", "files", "old-venv")
        os.makedirs(trash)
        os.link(keep, os.path.join(trash, "threadpoolctl.py"))
        write(os.path.join(trash, "plain.txt"), b"shred me")
        plan = engine.build_plan([(os.path.join(self.dir, "Trash", "files"), True)])
        self.assertEqual(plan.shared, {os.path.join(trash, "threadpoolctl.py"): 1})
        sh = engine.Shredder(engine.METHODS["dod3"])
        sh.run(plan)
        self.assertEqual(os.listdir(os.path.join(self.dir, "Trash", "files")), [])
        with open(keep, "rb") as f:
            self.assertIn(b"ThreadpoolController", f.read())
        self.assertEqual((sh.stats.files_shredded, sh.stats.shared_kept), (1, 1))
        self.assertEqual(len(sh.stats.warnings), 1)
        self.assertIn("data kept", sh.stats.warnings[0])
        # the same selection, told to shred anyway: the data goes everywhere, with a warning
        os.makedirs(trash)
        os.link(keep, os.path.join(trash, "again.py"))
        sh = engine.Shredder(engine.METHODS["zero"], shared="shred")
        sh.run(engine.build_plan([(trash, False)]))
        self.assertFalse(os.path.exists(trash))
        self.assertEqual(os.path.getsize(keep), 0)
        self.assertIn("destroyed too", sh.stats.warnings[0])

    def test_hard_links_inside_selection_all_go(self):
        """Every name of a file inside the selection is removed, the data overwritten once."""
        root = os.path.join(self.dir, "box")
        a = os.path.join(root, "a.bin")
        write(a, b"q" * 9000)
        os.link(a, os.path.join(root, "b.bin"))
        os.link(a, os.path.join(root, "sub", "c.bin")) if os.makedirs(os.path.join(root, "sub")) is None else None
        plan = engine.build_plan([(root, False)])
        self.assertEqual(len(plan.files), 1)
        self.assertEqual(plan.shared, {})
        self.assertEqual(len(plan.aliases[plan.files[0][0]]), 2)
        sh = engine.Shredder(engine.METHODS["zero"])
        sh.run(plan)
        self.assertFalse(os.path.exists(root))
        self.assertEqual((sh.stats.files_shredded, sh.stats.shared_kept, sh.stats.warnings), (1, 0, []))
        self.assertEqual(sh.stats.bytes_written, 12288)

    def test_older_than_filter(self):
        root = os.path.join(self.dir, "dl")
        write(os.path.join(root, "old.zip"), b"x" * 100)
        write(os.path.join(root, "new.zip"), b"y" * 100)
        past = time.time() - 10 * 86400
        os.utime(os.path.join(root, "old.zip"), (past, past))
        engine.Shredder(engine.METHODS["zero"]).run(engine.build_plan([(root, True)], older_than_days=7))
        self.assertEqual(os.listdir(root), ["new.zip"])

    def test_readonly_file_and_unicode_name(self):
        path = os.path.join(self.dir, "résumé ñ 文件.pdf")
        write(path, b"data")
        os.chmod(path, 0o400)
        sh = engine.Shredder(engine.METHODS["dod3"])
        sh.run(engine.build_plan([(path, False)]))
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.listdir(self.dir), [])

    def test_empty_file(self):
        path = os.path.join(self.dir, "empty")
        write(path, b"")
        sh = engine.Shredder(engine.METHODS["dod3"])
        sh.run(engine.build_plan([(path, False)]))
        self.assertFalse(os.path.exists(path))

    def test_cancel(self):
        path = os.path.join(self.dir, "big")
        write(path, b"z" * (4 * engine.CHUNK))
        token = engine.CancelToken()
        token.cancel()
        sh = engine.Shredder(engine.METHODS["dod3"], cancel=token)
        with self.assertRaises(engine.Cancelled):
            sh.run(engine.build_plan([(path, False)]))

    def test_free_space_wipe_simulated_full_disk(self):
        budget = {"left": 5 * engine.CHUNK + 1234}
        real = engine.os
        fake = types.SimpleNamespace(**{k: getattr(real, k) for k in dir(real) if not k.startswith("__")})
        files_seen = {}

        def fake_write(fd, buf):
            n = len(buf)
            path = os.readlink(f"/proc/self/fd/{fd}")
            if engine.WIPE_DIR_PREFIX in path:
                size = os.fstat(fd).st_size
                pos = os.lseek(fd, 0, os.SEEK_CUR)
                grow = max(0, pos + n - size)
                if grow > budget["left"]:
                    if budget["left"] == 0:
                        raise OSError(errno.ENOSPC, "No space left on device")
                    n = budget["left"]
                    grow = n
                budget["left"] -= grow
                files_seen[path] = True
            return real.write(fd, buf[:n])

        fake.write = fake_write
        engine.os = fake
        workdirs = []
        try:
            sh = engine.Shredder(engine.METHODS["dod3"])
            sh.wipe_free_space(self.dir, on_workdir=lambda p, a: workdirs.append((p, a)))
        finally:
            engine.os = real
        self.assertEqual(os.listdir(self.dir), [])
        self.assertEqual(len(workdirs), 2)
        self.assertEqual(sh.stats.bytes_data, 5 * engine.CHUNK + 1234)
        self.assertEqual(sh.stats.bytes_written, 3 * (5 * engine.CHUNK + 1234))
        self.assertTrue(files_seen)


class SafetyTests(unittest.TestCase):
    def test_protected(self):
        for p in ("/", "/usr", "/etc/passwd", os.path.expanduser("~"), "/home",
                  os.path.expanduser("~/.config")):
            self.assertIsNotNone(system.check_target(p), p)

    def test_mountpoint_needs_keep_root(self):
        self.assertIsNotNone(system.check_target("/boot/efi"))

    def test_regular_ok(self):
        d = tempfile.mkdtemp(prefix="shreder-test-")
        self.assertIsNone(system.check_target(d))


class StoreJobTests(unittest.TestCase):
    def test_job_records_history_and_folder_runs(self):
        store = Store()
        d = tempfile.mkdtemp(prefix="shreder-test-")
        write(os.path.join(d, "x.txt"), b"hello")
        folder = store.add_folder(d)
        folder["schedule"] = "daily"
        store.save_folder(folder)
        self.assertEqual([f["id"] for f in store.due_folders()], [folder["id"]])
        job = jobs.Job(kind="shred", title="t", method_id="zero", targets=[(d, True)],
                       folder_ids=[folder["id"]])
        jobs.execute_job(job, store)
        self.assertEqual(job.state, "done")
        self.assertEqual(os.listdir(d), [])
        self.assertEqual(store.due_folders(), [])
        self.assertEqual(store.history()[0]["stats"]["files_shredded"], 1)
        self.assertEqual(store.stats["files"], 1)
        # a job with something to look at (here: a kept shared file) is "warning", not a quiet success
        write(os.path.join(d, "y.txt"), b"shared")
        os.link(os.path.join(d, "y.txt"), os.path.join(tempfile.mkdtemp(prefix="shreder-test-"), "y"))
        job = jobs.Job(kind="shred", title="t2", method_id="zero", targets=[(d, True)])
        jobs.execute_job(job, store)
        self.assertEqual(job.state, "warning")
        self.assertIn("1 kept (shared data)", job.summary())
        self.assertEqual(store.history()[0]["stats"]["shared_kept"], 1)
        self.assertEqual(os.listdir(d), [])

    def test_routine_cancel_stops_rest_of_group(self):
        store = Store()
        mgr = jobs.JobManager(store)
        d = tempfile.mkdtemp(prefix="shreder-test-")
        big = os.path.join(d, "big")
        write(big, os.urandom(64 * engine.CHUNK))
        later = os.path.join(d, "later")
        write(later, b"keep")
        first = jobs.Job(kind="shred", title="a", method_id="gutmann", targets=[(big, False)], group="g1")
        second = jobs.Job(kind="shred", title="b", method_id="zero", targets=[(later, False)], group="g1")
        mgr.submit(first)
        mgr.submit(second)
        while first.state != "running":
            time.sleep(0.01)
        mgr.cancel(first)
        for _ in range(200):
            if not mgr.busy:
                break
            time.sleep(0.02)
        mgr.shutdown()
        self.assertEqual(first.state, "cancelled")
        self.assertEqual(second.state, "cancelled")
        self.assertTrue(os.path.exists(later))

    def test_volume_for(self):
        mk = lambda mp: system.Volume("d", mp, "ext4", "", "", 1, 1, 1, False, False, "", mp)
        vols = [mk("/"), mk("/media/usb"), mk("/home")]
        self.assertEqual(system.volume_for("/home/x/file", vols).mountpoint, "/home")
        self.assertEqual(system.volume_for("/media/usb", vols).mountpoint, "/media/usb")
        self.assertEqual(system.volume_for("/media/usb2/f", vols).mountpoint, "/")

    def test_manager_thread(self):
        store = Store()
        done = []
        mgr = jobs.JobManager(store, on_finished=done.append)
        p = tempfile.mkstemp(prefix="shreder-test-")[1]
        mgr.submit(jobs.Job(kind="shred", title="t", method_id="dod3", targets=[(p, False)], verify=True))
        for _ in range(100):
            if done:
                break
            time.sleep(0.05)
        mgr.shutdown()
        self.assertEqual(done[0].state, "done")
        self.assertFalse(os.path.exists(p))


if __name__ == "__main__":
    unittest.main()
