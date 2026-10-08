"""Main window: sidebar navigation, page stack, live job panel and shared actions."""
from __future__ import annotations

import os

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .. import engine, jobs, system
from . import common
from .activity import ActivityPage
from .dashboard import DashboardPage
from .folders import FoldersPage
from .guide import GuidePage
from .shred import ShredPage
from .wipe import WipePage

PAGES = [
    ("dashboard", "Command Center", "view-grid-symbolic"),
    ("shred", "Shred Files", "shreder-symbolic"),
    ("folders", "Remembered Folders", "starred-symbolic"),
    ("wipe", "Wipe Free Space", "drive-harddisk-symbolic"),
    ("guide", "Guides", "help-about-symbolic"),
    ("activity", "Activity", "document-open-recent-symbolic"),
]

PHASES = {
    "preparing": "Preparing", "scanning": "Scanning", "overwriting": "Overwriting",
    "verifying": "Verifying", "cleanup": "Cleaning up", "authorizing": "Waiting for authorization",
    "trimming": "Running TRIM", "done": "Finishing",
}


class JobPanel(Gtk.Box):
    """Live progress for the running job, docked at the bottom of the content area."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["job-panel"])
        self.win = win
        top = Gtk.Box(spacing=12)
        self.icon = Gtk.Image(icon_name="edit-delete-symbolic", pixel_size=24, valign=Gtk.Align.CENTER)
        top.append(self.icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0, css_classes=["job-title"], ellipsize=Pango.EllipsizeMode.END)
        self.current = Gtk.Label(xalign=0, css_classes=["job-meta"], ellipsize=Pango.EllipsizeMode.MIDDLE)
        text.append(self.title)
        text.append(self.current)
        top.append(text)
        self.percent = Gtk.Label(css_classes=["job-percent"], valign=Gtk.Align.CENTER)
        top.append(self.percent)
        self.cancel_btn = Gtk.Button(label="Cancel", valign=Gtk.Align.CENTER, css_classes=["pill"])
        self.cancel_btn.connect("clicked", self._on_cancel)
        top.append(self.cancel_btn)
        self.append(top)
        self.bar = Gtk.ProgressBar(css_classes=["job-bar"])
        self.append(self.bar)
        self.meta = Gtk.Label(xalign=0, css_classes=["job-meta"], ellipsize=Pango.EllipsizeMode.END)
        self.append(self.meta)

    def _on_cancel(self, *_):
        job = self.win.jobs.current
        if job:
            self.win.jobs.cancel(job)
            self.cancel_btn.set_sensitive(False)
            self.cancel_btn.set_label("Stopping…")

    def update(self, job: jobs.Job):
        p = job.progress
        self.icon.set_from_icon_name(job.icon)
        self.title.set_label(job.title)
        phase = PHASES.get(p.phase, p.phase.title())
        current = p.current
        if current and os.sep in current and job.kind == "shred":
            current = system.short_path(current)
        self.current.set_label(f"{phase} — {current}" if current else phase)
        if p.total > 0 and p.phase not in ("authorizing", "trimming", "scanning"):
            self.bar.set_fraction(p.fraction)
            self.percent.set_label(f"{p.fraction * 100:.0f}%")
        else:
            self.bar.pulse()
            self.percent.set_label("")
        bits = []
        if p.pass_total:
            bits.append(f"Pass {max(p.pass_no, 1)} of {p.pass_total}")
        if p.files_total:
            bits.append(f"{p.files_done:,} of {p.files_total:,} items")
        if p.total:
            bits.append(f"{system.human_size(p.done)} of {system.human_size(p.total)}")
        if p.rate:
            bits.append(f"{system.human_size(p.rate)}/s")
            bits.append(f"{system.human_duration(p.eta)} left")
        queued = len(self.win.jobs.pending)
        if queued:
            bits.append(f"{queued} more queued")
        self.meta.set_label("  ·  ".join(bits))
        if not job.cancel.cancelled:
            self.cancel_btn.set_sensitive(True)
            self.cancel_btn.set_label("Cancel")


class SidebarJob(Gtk.Button):
    def __init__(self, win):
        super().__init__(css_classes=["card", "sidebar-job"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.title = Gtk.Label(xalign=0, css_classes=["job-title"], ellipsize=Pango.EllipsizeMode.END)
        self.bar = Gtk.ProgressBar(css_classes=["job-bar"])
        self.meta = Gtk.Label(xalign=0, css_classes=["job-meta"], ellipsize=Pango.EllipsizeMode.END)
        box.append(self.title)
        box.append(self.bar)
        box.append(self.meta)
        self.set_child(box)
        self.connect("clicked", lambda *_: win.navigate("activity"))

    def update(self, job: jobs.Job):
        p = job.progress
        self.title.set_label(job.title)
        if p.total > 0 and p.phase not in ("authorizing", "trimming", "scanning"):
            self.bar.set_fraction(p.fraction)
            self.meta.set_label(f"{p.fraction * 100:.0f}%  ·  {system.human_duration(p.eta)} left"
                                if p.rate else f"{p.fraction * 100:.0f}%")
        else:
            self.bar.pulse()
            self.meta.set_label(PHASES.get(p.phase, "Working"))


class ShrederWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Shreder", default_width=1180, default_height=780)
        self.set_size_request(360, 500)
        self.app = app
        self.store = app.store
        self.jobs = app.jobs
        self._poll_id = 0
        self._refresh_pending = False
        self.volumes: list[system.Volume] = []

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=230, max_sidebar_width=280)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # ---------------------------------------------------------------- sidebar
        side_tv = Adw.ToolbarView()
        side_header = Adw.HeaderBar()
        brand = Gtk.Box(spacing=10)
        brand.append(Gtk.Image(icon_name=app.get_application_id(), pixel_size=28))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        names.append(Gtk.Label(label="Shreder", xalign=0, css_classes=["brand-title"]))
        names.append(Gtk.Label(label="Secure Erase Center", xalign=0, css_classes=["brand-sub"]))
        brand.append(names)
        side_header.set_title_widget(brand)
        side_header.set_show_title(True)
        side_tv.add_top_bar(side_header)

        self.nav = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.nav_rows = {}
        self.nav_counts = {}
        for name, title, icon in PAGES:
            row = Gtk.ListBoxRow()
            row._page = name
            box = Gtk.Box(spacing=12)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            count = Gtk.Label(css_classes=["nav-count"], visible=False)
            box.append(count)
            row.set_child(box)
            self.nav.append(row)
            self.nav_rows[name] = row
            self.nav_counts[name] = count
        self.nav.connect("row-activated", lambda _lb, row: self.navigate(row._page))
        side_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        side_scroll.set_child(self.nav)
        side_tv.set_content(side_scroll)
        self.sidebar_job = SidebarJob(self)
        self.sidebar_job_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.sidebar_job_rev.set_child(self.sidebar_job)
        side_tv.add_bottom_bar(self.sidebar_job_rev)
        self.split.set_sidebar(Adw.NavigationPage(title="Shreder", child=side_tv))

        # ---------------------------------------------------------------- content
        self.content_tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(title="Command Center")
        header.set_title_widget(self.window_title)
        menu = Gio.Menu()
        section = Gio.Menu()
        section.append("Preferences", "app.preferences")
        section.append("Keyboard Shortcuts", "app.shortcuts")
        section.append("About Shreder", "app.about")
        menu.append_section(None, section)
        quit_section = Gio.Menu()
        quit_section.append("Quit", "app.quit")
        menu.append_section(None, quit_section)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main Menu", primary=True))
        self.content_tv.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=120)
        self.pages = {
            "dashboard": DashboardPage(self),
            "shred": ShredPage(self),
            "folders": FoldersPage(self),
            "wipe": WipePage(self),
            "guide": GuidePage(self),
            "activity": ActivityPage(self),
        }
        for name, page in self.pages.items():
            self.stack.add_named(page, name)
        self.content_tv.set_content(self.stack)

        self.job_panel = JobPanel(self)
        self.job_rev = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        self.job_rev.set_child(self.job_panel)
        self.content_tv.add_bottom_bar(self.job_rev)
        self.content_page = Adw.NavigationPage(title="Command Center", child=self.content_tv)
        self.split.set_content(self.content_page)

        tiles = self.pages["dashboard"].tiles
        for condition, collapsed, columns in (("max-width: 900sp", False, 2),
                                              ("max-width: 720sp", True, 2),
                                              ("max-width: 460sp", True, 1)):
            bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(condition))
            bp.add_setter(self.split, "collapsed", collapsed)
            bp.add_setter(tiles, "min-children-per-line", columns)
            bp.add_setter(tiles, "max-children-per-line", columns)
            self.add_breakpoint(bp)

        # ---------------------------------------------------------------- wiring
        self.store.connect(self._queue_refresh)
        self.jobs.connect(self._on_jobs_changed)
        self.connect("close-request", self._on_close_request)
        self._install_shortcuts()
        self.navigate("dashboard")
        self.refresh_all()
        self._on_jobs_changed()

    # ------------------------------------------------------------------ navigation
    def navigate(self, name: str):
        self.stack.set_visible_child_name(name)
        title = next(t for n, t, _ in PAGES if n == name)
        self.window_title.set_title(title)
        self.content_page.set_title(title)
        row = self.nav_rows[name]
        if self.nav.get_selected_row() is not row:
            self.nav.select_row(row)
        self.split.set_show_content(True)
        page = self.pages[name]
        if hasattr(page, "on_shown"):
            page.on_shown()

    def _install_shortcuts(self):
        ctrl = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        for i, (name, _t, _i) in enumerate(PAGES, 1):
            ctrl.add_shortcut(Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(f"<Control>{i}"),
                Gtk.CallbackAction.new(lambda *_a, n=name: (self.navigate(n), True)[1])))
        ctrl.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control>o"),
            Gtk.CallbackAction.new(lambda *_a: (self.pages["shred"].add_files(), True)[1])))
        ctrl.add_shortcut(Gtk.Shortcut.new(
            Gtk.ShortcutTrigger.parse_string("<Control><Shift>o"),
            Gtk.CallbackAction.new(lambda *_a: (self.pages["shred"].add_folders(), True)[1])))
        self.add_controller(ctrl)

    # ------------------------------------------------------------------ refresh plumbing
    def _queue_refresh(self):
        if self._refresh_pending:
            return
        self._refresh_pending = True

        def run():
            self._refresh_pending = False
            self.refresh_all()
            return False
        GLib.idle_add(run)

    def refresh_all(self):
        for page in self.pages.values():
            page.refresh()
        self.update_counts()

    def update_counts(self):
        n = len(self.store.folders)
        self.nav_counts["folders"].set_label(str(n))
        self.nav_counts["folders"].set_visible(n > 0)
        q = len(self.pages["shred"].items)
        self.nav_counts["shred"].set_label(str(q))
        self.nav_counts["shred"].set_visible(q > 0)

    def _on_jobs_changed(self):
        busy = self.jobs.current is not None
        self.job_rev.set_reveal_child(busy)
        self.sidebar_job_rev.set_reveal_child(busy)
        if busy and not self._poll_id:
            self._poll_id = GLib.timeout_add(200, self._poll)
            self._poll()
        for page in self.pages.values():
            if hasattr(page, "on_jobs_changed"):
                page.on_jobs_changed()

    def _poll(self):
        job = self.jobs.current
        if job is None:
            self._poll_id = 0
            self.job_rev.set_reveal_child(False)
            self.sidebar_job_rev.set_reveal_child(False)
            return False
        self.job_panel.update(job)
        self.sidebar_job.update(job)
        return True

    def toast(self, text: str, timeout: int = 4, button: str | None = None, on_button=None):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if button and on_button:
            t.set_button_label(button)
            t.connect("button-clicked", lambda *_: on_button())
        self.toasts.add_toast(t)

    def on_volumes(self, vols):
        self.volumes = vols
        self.pages["dashboard"].set_volumes(vols)
        self.pages["guide"].set_volumes(vols)
        self.pages["shred"].set_volumes(vols)

    # ------------------------------------------------------------------ job results & next steps
    def after_job(self, job: jobs.Job, text: str):
        """Toast the result and, outside guided routines, point at the logical next step."""
        if job.state not in ("done", "warning") or job.group:
            if job.group and job.state in ("done", "warning") and not any(
                    j.group == job.group for j in self.jobs.pending):
                text = "All steps finished — " + job.title
            self.toast(text, timeout=6)
            return
        if job.kind == "shred":
            vols = [system.volume_for(p, self.volumes) for p, _ in job.targets]
            if any(v and (v.is_ssd or v.removable) for v in vols):
                self.toast(f"{job.title} — done. On SSDs, next: wipe free space, then TRIM", timeout=10,
                           button="Show Steps", on_button=lambda: self.pages["guide"].focus("ssd-files"))
                return
        elif job.kind == "wipe":
            vol = next((v for v in self.volumes if v.mountpoint == job.mountpoint), None)
            if vol and vol.is_ssd:
                self.toast("Free space wiped. Last step for SSDs: run TRIM", timeout=12,
                           button="Run TRIM", on_button=lambda: self.request_trim(vol))
                return
        self.toast(text, timeout=6)

    # ------------------------------------------------------------------ guided routines
    def run_routine(self, rid: str):
        home = system.volume_for(system.HOME, self.volumes)
        if rid in ("ssd-files", "hdd-files"):
            common.pick_files(self, lambda paths: self._routine_for_targets(rid, [(p, False) for p in paths]),
                              title="Choose Files to Shred")
        elif rid == "usb":
            common.pick_files(self, lambda paths: self._routine_for_targets(rid, [(p, True) for p in paths]),
                              folders=True, title="Choose the USB Drive (or Folders on It)")
        elif rid == "deleted":
            vol = self.pages["wipe"].volume or home
            if not vol:
                self.toast("Drives are still being scanned — try again in a moment")
                return
            steps = [{"kind": "wipe", "volume": vol, "method": "random"}]
            if vol.is_ssd:
                steps.append({"kind": "trim", "volume": vol})
            self._confirm_routine("Erase previously deleted files", steps)
        elif rid == "traces":
            targets = [(p, True) for p in system.trash_dirs() + system.thumbnail_dirs()]
            targets += [(p, False) for p in system.recent_files()]
            self._confirm_routine("Clear everyday traces", [
                {"kind": "shred", "targets": targets, "method": self.store.settings.get("method"),
                 "title": "Clear everyday traces"}])

    def _routine_for_targets(self, rid: str, targets):
        ok = []
        for path, keep in targets:
            reason = system.check_target(path, keep)
            if reason:
                self.toast(f"Skipped: {reason}")
            else:
                ok.append((path, keep))
        if not ok:
            return
        vols = []
        for p, _ in ok:
            v = system.volume_for(p, self.volumes)
            if v and v not in vols:
                vols.append(v)
        flash = rid in ("ssd-files", "usb")
        method = "random" if flash else self.store.settings.get("method")
        name = os.path.basename(ok[0][0]) if len(ok) == 1 else f"{len(ok)} items"
        steps = [{"kind": "shred", "targets": ok, "method": method, "title": f"Shred {name}"}]
        for v in vols:
            steps.append({"kind": "wipe", "volume": v, "method": "random"})
        if rid == "ssd-files":
            steps += [{"kind": "trim", "volume": v} for v in vols if v.is_ssd]
        titles = {"ssd-files": "Delete files on an SSD", "hdd-files": "Delete files on a hard disk",
                  "usb": "Clean a USB drive"}
        self._confirm_routine(titles[rid], steps)

    def _confirm_routine(self, name: str, steps: list):
        s = self._settings()

        def scan():
            for st in steps:
                if st["kind"] == "shred":
                    st["plan"] = engine.build_plan(st["targets"])
            return steps

        def confirm(result):
            if isinstance(result, Exception):
                self.toast(str(result))
                return
            todo = [st for st in result if st["kind"] != "shred" or st["plan"].item_count or st["plan"].dirs]
            if not todo:
                self.toast("Nothing to do — it's already clean")
                return
            lines = []
            for i, st in enumerate(todo, 1):
                m = engine.get_method(st.get("method"))
                if st["kind"] == "shred":
                    plan = st["plan"]
                    lines.append(f"<b>{i}.</b> Shred {plan.item_count:,} file{'s' if plan.item_count != 1 else ''} "
                                 f"({system.human_size(plan.data_bytes)}) · {GLib.markup_escape_text(m.name)}, "
                                 f"{m.short}")
                elif st["kind"] == "wipe":
                    v = st["volume"]
                    elevated = v.work_dir is None or s.get("wipe_elevated", False)
                    free = v.free_root if elevated else v.free
                    eta = system.human_duration(system.estimate_seconds(free * m.pass_count, v.is_ssd))
                    lines.append(f"<b>{i}.</b> Wipe free space on {GLib.markup_escape_text(v.name)} · "
                                 f"{GLib.markup_escape_text(m.name)}, {m.short} · "
                                 f"{system.human_size(free)}, about {eta}"
                                 + (" · asks for your password" if elevated else ""))
                else:
                    lines.append(f"<b>{i}.</b> TRIM {GLib.markup_escape_text(st['volume'].name)} · "
                                 "asks for your password")
            body = ("\n".join(lines) + "\n\nSteps run one after another. Cancelling a step stops the rest."
                    + ("\n\n<b>Shredded files cannot be recovered.</b>"
                       if any(st["kind"] == "shred" for st in todo) else ""))

            def go():
                group = f"routine-{GLib.get_monotonic_time()}"
                for st in todo:
                    if st["kind"] == "shred":
                        job = jobs.Job(kind="shred", title=st["title"], method_id=st["method"],
                                       targets=st["targets"], verify=s.get("verify", True),
                                       rename_passes=s.get("rename_passes", 3))
                    elif st["kind"] == "wipe":
                        v = st["volume"]
                        job = jobs.Job(kind="wipe", title=f"Wipe free space — {v.name}", method_id=st["method"],
                                       directory=v.work_dir or "", mountpoint=v.mountpoint,
                                       elevated=v.work_dir is None or s.get("wipe_elevated", False),
                                       icon="drive-harddisk-symbolic")
                    else:
                        v = st["volume"]
                        job = jobs.Job(kind="trim", title=f"TRIM — {v.name}", mountpoint=v.mountpoint,
                                       icon="system-run-symbolic")
                    job.group = group
                    self.jobs.submit(job)
                self.toast(f"{name}: {len(todo)} step{'s' if len(todo) != 1 else ''} queued")
            common.confirm(self, f"{name}?", body, "Run Steps", go)
        common.run_async(scan, confirm)

    # ------------------------------------------------------------------ shared actions
    def _settings(self):
        return self.store.settings

    def request_shred(self, targets, *, title: str, method_id: str | None = None,
                      verify: bool | None = None, older_than_days: float = 0,
                      include_hidden: bool = True, folder_ids=(), icon="shreder-symbolic",
                      on_queued=None, heading: str | None = None, quiet_if_empty: bool = False):
        s = self._settings()
        method = engine.get_method(method_id or s.get("method"))
        verify = s.get("verify", True) if verify is None else verify
        ok = []
        for path, keep in targets:
            reason = system.check_target(path, keep)
            if reason:
                self.toast(f"Skipped: {reason}")
            else:
                ok.append((path, keep))
        if not ok:
            return

        def plan():
            return engine.build_plan(ok, older_than_days=older_than_days, include_hidden=include_hidden)

        def confirm(result):
            if isinstance(result, Exception):
                self.toast(f"Could not read items: {result}")
                return
            if result.item_count == 0 and not result.dirs:
                if not quiet_if_empty:
                    self.toast("Nothing to shred — it's already clean")
                return
            n = result.item_count
            what = f"{n:,} file{'s' if n != 1 else ''}" if n else f"{len(result.dirs)} empty folder(s)"
            body = (f"<b>{what}</b> ({system.human_size(result.data_bytes)}) will be overwritten with "
                    f"<b>{GLib.markup_escape_text(method.name)}</b> ({method.short})"
                    f"{', verified,' if verify else ''} renamed and deleted.\n\n"
                    f"<b>This cannot be undone.</b> Shredded data cannot be recovered by any tool.")
            if self.jobs.busy:
                body += "\n\nIt will start when the current task finishes."

            def go():
                job = jobs.Job(kind="shred", title=title, method_id=method.id, targets=ok,
                               older_than_days=older_than_days, include_hidden=include_hidden,
                               verify=verify, rename_passes=s.get("rename_passes", 3),
                               folder_ids=list(folder_ids), icon=icon)
                self.jobs.submit(job)
                if on_queued:
                    on_queued()
            common.confirm(self, heading or f"Shred {what}?", body, "Shred", go,
                           extra=common.method_breakdown(method))
        common.run_async(plan, confirm)

    def shred_folders(self, folders: list[dict]):
        folders = [f for f in folders if os.path.isdir(f["path"])]
        if not folders:
            self.toast("Those folders no longer exist")
            return
        s = self._settings()
        default = s.get("method", engine.DEFAULT_METHOD)

        def scan():
            plans = []
            for f in folders:
                if system.check_target(f["path"], f.get("keep_root", True)):
                    continue
                plans.append((f, engine.build_plan([(f["path"], f.get("keep_root", True))],
                                                   older_than_days=f.get("older_than_days", 0),
                                                   include_hidden=f.get("include_hidden", True))))
            return plans

        def confirm(plans):
            if isinstance(plans, Exception):
                self.toast(str(plans))
                return
            plans = [(f, p) for f, p in plans if p.item_count or p.dirs]
            if not plans:
                self.toast("Nothing to shred — remembered folders are already clean")
                return
            files = sum(p.item_count for _, p in plans)
            size = sum(p.data_bytes for _, p in plans)
            names = ", ".join(GLib.markup_escape_text(f["label"]) for f, _ in plans[:4])
            if len(plans) > 4:
                names += f" and {len(plans) - 4} more"
            body = (f"<b>{files:,} items</b> ({system.human_size(size)}) in {names} will be "
                    f"securely overwritten and deleted.\n\n<b>This cannot be undone.</b>")

            def go():
                for f, _ in plans:
                    method = f.get("method") or default
                    self.jobs.submit(jobs.Job(
                        kind="shred", title=f"Shred {f['label']}", method_id=method,
                        targets=[(f["path"], f.get("keep_root", True))],
                        older_than_days=f.get("older_than_days", 0),
                        include_hidden=f.get("include_hidden", True),
                        verify=s.get("verify", True), rename_passes=s.get("rename_passes", 3),
                        folder_ids=[f["id"]], icon="starred-symbolic"))
            heading = (f"Shred contents of {plans[0][0]['label']}?" if len(plans) == 1
                       else f"Shred {len(plans)} remembered folders?")
            common.confirm(self, heading, body, "Shred", go)
        common.run_async(scan, confirm)

    def request_wipe(self, volume: system.Volume, method_id: str, elevated: bool):
        method = engine.get_method(method_id)
        free = volume.free_root if elevated else volume.free
        writes = free * method.pass_count
        body = (f"About <b>{system.human_size(free)}</b> of free space on <b>{GLib.markup_escape_text(volume.name)}</b> "
                f"will be overwritten {method.pass_count}× with {GLib.markup_escape_text(method.name)} "
                f"(≈{system.human_size(writes)} of writes). Your files are not touched.\n\n"
                "The drive will be <b>completely full</b> while this runs, so other apps may "
                "briefly report low disk space. Cancelling is safe at any time.")
        if elevated:
            body += "\n\nYou'll be asked for your password."

        def go():
            self.jobs.submit(jobs.Job(
                kind="wipe", title=f"Wipe free space — {volume.name}", method_id=method.id,
                directory=volume.work_dir or "", mountpoint=volume.mountpoint, elevated=elevated,
                icon="drive-harddisk-symbolic"))
            self.toast("Free-space wipe started" if not self.jobs.busy else "Wipe queued")
        common.confirm(self, f"Wipe free space on {volume.name}?", body, "Start Wipe", go,
                       extra=common.method_breakdown(method))

    def request_trim(self, volume: system.Volume):
        self.jobs.submit(jobs.Job(kind="trim", title=f"TRIM — {volume.name}", mountpoint=volume.mountpoint,
                                  icon="drive-harddisk-symbolic"))

    def sweep(self, kind: str):
        if kind == "trash":
            paths = system.trash_dirs()
            self.request_shred([(p, True) for p in paths], title="Empty Trash securely",
                               icon="user-trash-full-symbolic", heading="Securely empty the Trash?")
        elif kind == "thumbnails":
            paths = system.thumbnail_dirs()
            self.request_shred([(p, True) for p in paths], title="Shred thumbnail cache",
                               icon="image-x-generic-symbolic", heading="Shred the thumbnail cache?")
        elif kind == "recent":
            paths = system.recent_files()
            if not paths:
                self.toast("Recent files history is already empty")
                return
            self.request_shred([(p, False) for p in paths], title="Clear recent files history",
                               icon="document-open-recent-symbolic", heading="Shred recent files history?")

    def sync_scheduler(self):
        want = self.store.any_scheduled()

        def work():
            if want == system.scheduler_active():
                return None
            return system.set_scheduler(want)

        def done(err):
            if isinstance(err, Exception) or err:
                self.toast(f"Background schedule: {err}")
            elif want:
                self.toast("Background schedule is active")
        common.run_async(work, done)

    # ------------------------------------------------------------------ closing
    def _on_close_request(self, *_):
        if not self.jobs.busy:
            return False

        def stop():
            self.jobs.cancel_all()
            self.toast("Stopping safely…")

            def wait():
                if self.jobs.busy:
                    return True
                self.app.quit()
                return False
            GLib.timeout_add(150, wait)
        common.confirm(self, "Stop and quit?",
                       "A shredding task is still running. Stopping now leaves any file it was "
                       "working on partly overwritten but not deleted. Free-space fill files are "
                       "always cleaned up.", "Stop and Quit", stop)
        return True
