"""Wipe Free Space page: choose a drive, method and privilege level."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from .. import engine, system
from . import common


def usage_bar(vol: system.Volume, width: int = 140) -> Gtk.LevelBar:
    used = 1 - (vol.free_root / vol.total) if vol.total else 0
    bar = Gtk.LevelBar(min_value=0, max_value=1, value=used, valign=Gtk.Align.CENTER,
                       css_classes=["usage"], width_request=width)
    bar.remove_offset_value("low")
    bar.remove_offset_value("high")
    bar.remove_offset_value("full")
    if used > 0.95:
        bar.add_css_class("full")
    elif used > 0.85:
        bar.add_css_class("high")
    return bar


def volume_subtitle(vol: system.Volume) -> str:
    bits = [vol.kind, vol.mountpoint, vol.fstype]
    if vol.model:
        bits.append(vol.model)
    return " · ".join(bits)


class WipePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.volumes: list[system.Volume] = []
        self.selected: str | None = None
        self.checks: dict[str, Gtk.CheckButton] = {}

        content = common.page_box()
        refresh = common.icon_button("view-refresh-symbolic", "Rescan drives", self.load)
        content.append(common.section_header(
            "Wipe free space",
            "Deleting a file only forgets where it was — the data stays in free space until something "
            "overwrites it. This fills every free block on the drive with your chosen pattern, then "
            "releases it again. Existing files are never touched.", refresh))

        self.drive_group = Adw.PreferencesGroup(title="Drive")
        self.drive_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        loading = Adw.ActionRow(title="Scanning drives…")
        self.drive_list.set_placeholder(loading)
        self.drive_group.add(self.drive_list)
        content.append(self.drive_group)

        opts = Adw.PreferencesGroup(title="Options")
        self.method = common.method_row(win.store.settings.get("method"))
        self.method.connect("notify::selected", lambda *_: self._sync())
        opts.add(self.method)
        self.elevated = Adw.SwitchRow(
            title="Run with administrator rights",
            subtitle="Also overwrites the space reserved for the system (about 5% on ext4) "
                     "and drives you can't write to. Asks for your password.",
            active=win.store.settings.get("wipe_elevated", False))
        self.elevated.connect("notify::active", lambda *_: self._sync())
        opts.add(self.elevated)
        content.append(opts)

        self.notes = Adw.PreferencesGroup(title="Before you start")
        content.append(self.notes)
        self._note_rows = []

        self.start_btn = Gtk.Button(css_classes=["pill", "shred-button"])
        self.start_btn.set_child(Adw.ButtonContent(icon_name="drive-harddisk-symbolic", label="Wipe Free Space"))
        self.start_btn.connect("clicked", lambda *_: self.start())
        self.trim_btn = common.pill_button("Run TRIM", self.trim, icon="system-run-symbolic")
        self.trim_btn.set_tooltip_text("Tell the SSD to discard all unused blocks (asks for your password)")
        content.append(common.button_row(self.start_btn, self.trim_btn, center=True))
        self.estimate = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"])
        content.append(self.estimate)

        self.append(common.scrolled_page(content, 900))
        self._loaded = False

    # ------------------------------------------------------------------ data
    def on_shown(self):
        if not self._loaded:
            self.load()

    def load(self):
        self._loaded = True
        common.run_async(system.list_volumes, self._got_volumes)

    def _got_volumes(self, vols):
        if isinstance(vols, Exception):
            self.win.toast(f"Could not list drives: {vols}")
            return
        self.volumes = vols
        common.clear_box(self.drive_list)
        self.checks.clear()
        group = None
        if self.selected not in {v.mountpoint for v in vols}:
            self.selected = vols[0].mountpoint if vols else None
        for v in vols:
            row = Adw.ActionRow(title=v.name, subtitle=volume_subtitle(v), activatable=True, use_markup=False)
            check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if group:
                check.set_group(group)
            else:
                group = check
            check.set_active(v.mountpoint == self.selected)
            check.connect("toggled", lambda c, mp=v.mountpoint: c.get_active() and self.select(mp))
            row.add_prefix(Gtk.Image(icon_name=v.icon, pixel_size=24))
            row.add_prefix(check)
            row.set_activatable_widget(check)
            info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
            info.append(Gtk.Label(label=f"{system.human_size(v.free)} free of {system.human_size(v.total)}",
                                  xalign=1, css_classes=["caption", "numeric"]))
            info.append(usage_bar(v, 110))
            row.add_suffix(info)
            self.drive_list.append(row)
            self.checks[v.mountpoint] = check
        self._sync()
        self.win.on_volumes(vols)

    def select(self, mountpoint: str):
        self.selected = mountpoint
        if mountpoint in self.checks and not self.checks[mountpoint].get_active():
            self.checks[mountpoint].set_active(True)
        self._sync()

    @property
    def volume(self) -> system.Volume | None:
        return next((v for v in self.volumes if v.mountpoint == self.selected), None)

    # ------------------------------------------------------------------ ui state
    def _note(self, icon: str, title: str, subtitle: str):
        row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=0)
        row.add_prefix(Gtk.Image(icon_name=icon, valign=Gtk.Align.START, margin_top=14))
        self.notes.add(row)
        self._note_rows.append(row)
        return row

    def _sync(self):
        for row in self._note_rows:
            self.notes.remove(row)
        self._note_rows = []
        v = self.volume
        method = engine.get_method(common.get_method_id(self.method))
        if v is None:
            self.start_btn.set_sensitive(False)
            self.trim_btn.set_visible(False)
            self.estimate.set_label("")
            self.notes.set_visible(False)
            return
        forced = v.work_dir is None
        if forced and not self.elevated.get_active():
            self.elevated.set_active(True)
        elevated = self.elevated.get_active()
        free = v.free_root if elevated else v.free
        writes = free * method.pass_count
        self.start_btn.set_sensitive(True)
        self.trim_btn.set_visible(v.is_ssd)
        mins = system.estimate_seconds(writes, v.is_ssd) / 60
        approx = f"roughly {system.human_duration(mins * 60)}" if mins >= 1 else "under a minute"
        self.estimate.set_label(
            f"{system.human_size(free)} free × {method.pass_count} pass"
            f"{'es' if method.pass_count != 1 else ''} = {system.human_size(writes)} to write · {approx}")

        if v.is_ssd:
            row = self._note("dialog-information-symbolic", "Solid-state drive — recommended order",
                             "SSDs remap writes internally, so overwriting can't guarantee every old block is "
                             "reached. Do it in this order: 1. shred the files, 2. wipe free space with one "
                             "random pass, 3. run TRIM. Extra passes mostly add wear.")
            row.add_suffix(common.pill_button("Show Steps", lambda: self.win.pages["guide"].focus("ssd-files"),
                                              "small"))
        else:
            row = self._note("dialog-information-symbolic", "Hard disk — recommended order",
                             "1. shred the sensitive files, 2. wipe free space to destroy older copies. "
                             "On a hard disk overwriting is fully effective.")
            row.add_suffix(common.pill_button("Show Steps", lambda: self.win.pages["guide"].focus("hdd-files"),
                                              "small"))
        if v.is_cow:
            self._note("dialog-warning-symbolic", f"{v.fstype} is copy-on-write",
                       "Snapshots keep old data alive. Delete snapshots you don't need first; later passes "
                       "may end early because the filesystem can't overwrite in place.")
        if forced:
            self._note("system-lock-screen-symbolic", "Administrator rights required",
                       "You don't have a writable folder on this drive.")
        self._note("dialog-warning-symbolic", "The drive will be full while this runs",
                   "Other apps may briefly report low disk space. Close downloads and editors first. "
                   "Fill files are removed when the wipe finishes, is cancelled, or on next launch after a crash.")
        if method.pass_count > 7:
            self._note("dialog-warning-symbolic", "That's a lot of passes",
                       "35 passes on a large drive can take many hours.")
        self.notes.set_visible(True)

    # ------------------------------------------------------------------ actions
    def start(self):
        v = self.volume
        if not v:
            return
        elevated = self.elevated.get_active()
        if v.work_dir and elevated != self.win.store.settings.get("wipe_elevated"):
            self.win.store.set_setting("wipe_elevated", elevated)  # remember the user's choice
        self.win.request_wipe(v, common.get_method_id(self.method), elevated)

    def trim(self):
        v = self.volume
        if v:
            self.win.request_trim(v)

    def refresh(self):
        pass

    def on_jobs_changed(self):
        # free space changes after a job: rescan quietly when idle
        if self._loaded and not self.win.jobs.busy:
            self.load()
