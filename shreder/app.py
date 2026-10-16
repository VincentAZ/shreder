"""Application entry point."""
from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, jobs, system  # noqa: E402
from .store import CONFIG_PATH, Store  # noqa: E402

APP_ID = "io.github.shreder.Shreder"
DATA_DIR = os.path.join(system.PROJECT_DIR, "data")


class ShrederApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.window = None
        self.store = None
        self.jobs = None
        GLib.set_application_name("Shreder")

    # ------------------------------------------------------------------ lifecycle
    def do_startup(self):
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(os.path.join(DATA_DIR, "icons"))
        css = Gtk.CssProvider()
        css.load_from_path(os.path.join(DATA_DIR, "style.css"))
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)

        self.store = Store()
        self.jobs = jobs.JobManager(self.store, dispatch=self._dispatch, on_finished=self._job_finished)
        self._watch_config()
        GLib.idle_add(self._startup_cleanup)

        for name, cb, accels in (
            ("quit", lambda *_: self._quit(), ["<Control>q"]),
            ("preferences", lambda *_: self._preferences(), ["<Control>comma"]),
            ("about", lambda *_: self._about(), []),
            ("shortcuts", lambda *_: self._shortcuts(), ["<Control>question"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)

    def do_activate(self):
        if self.window is None:
            from .ui.window import ShrederWindow
            self.window = ShrederWindow(self)
        self.window.present()

    def do_open(self, files, n_files, hint):
        self.do_activate()
        paths = [f.get_path() for f in files if f.get_path()]
        if paths:
            self.window.pages["shred"].add_paths(paths)
            self.window.navigate("shred")

    def do_shutdown(self):
        if self.jobs:
            self.jobs.shutdown(timeout=20)
        Adw.Application.do_shutdown(self)

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _dispatch(fn):
        GLib.idle_add(lambda: (fn(), False)[1])

    def _startup_cleanup(self):
        if self.store.data.get("pending_cleanup"):
            jobs.cleanup_stale_workdirs(self.store)
        return False

    def _watch_config(self):
        """Pick up changes made by the background scheduler."""
        self._monitor = Gio.File.new_for_path(CONFIG_PATH).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._reload_id = 0

        def changed(*_):
            if self._reload_id:
                return

            def reload():
                self._reload_id = 0
                self.store.reload()
                return False
            self._reload_id = GLib.timeout_add(600, reload)
        self._monitor.connect("changed", changed)

    def _job_finished(self, job):
        win = self.window
        text = {
            "done": f"{job.title} — done",
            "warning": f"{job.title} — finished with problems",
            "failed": f"{job.title} — failed: {job.error}",
            "cancelled": f"{job.title} — cancelled",
        }.get(job.state, job.title)
        if win:
            win.after_job(job, text)
        if self.store.settings.get("notify", True) and job.state != "cancelled" and (
                win is None or not win.is_active()):
            n = Gio.Notification.new(text if job.state != "done" else job.title)
            n.set_body(job.summary())
            n.set_icon(Gio.ThemedIcon.new(APP_ID))
            self.send_notification(f"job-{job.id}", n)

    # ------------------------------------------------------------------ actions
    def _quit(self):
        if self.window:
            self.window.close()
        else:
            self.quit()

    def _preferences(self):
        from .ui.preferences import PreferencesDialog
        if self.window:
            PreferencesDialog(self.window).present(self.window)

    def _about(self):
        about = Adw.AboutDialog(
            application_name="Shreder", application_icon=APP_ID, version=__version__,
            developer_name="Shreder", license_type=Gtk.License.MIT_X11,
            comments="A command center for secure file shredding and free-space wiping, with "
                     "DoD 5220.22-M, Gutmann and other overwrite standards.")
        about.add_legal_section(
            "A note on SSDs", None, Gtk.License.CUSTOM,
            "Flash storage remaps writes, so no software overwrite can guarantee every physical copy is "
            "destroyed. For whole-drive sanitization of SSDs use the drive's built-in secure erase "
            "or full-disk encryption.")
        about.present(self.window)

    def _shortcuts(self):
        dlg = Adw.ShortcutsDialog()
        sec = Adw.ShortcutsSection(title="General")
        for title, accel in (("Add files to shred", "<Control>o"), ("Add folders to shred", "<Control><Shift>o"),
                             ("Preferences", "<Control>comma"), ("Quit", "<Control>q")):
            sec.add(Adw.ShortcutsItem.new(title, accel))
        dlg.add(sec)
        nav = Adw.ShortcutsSection(title="Navigation")
        from .ui.window import PAGES
        for i, (_name, title, _icon) in enumerate(PAGES, 1):
            nav.add(Adw.ShortcutsItem.new(title, f"<Control>{i}"))
        dlg.add(nav)
        dlg.present(self.window)


def main(argv=None):
    app = ShrederApp()
    return app.run(argv if argv is not None else sys.argv)
