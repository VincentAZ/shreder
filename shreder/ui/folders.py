"""Remembered Folders page and the per-folder settings dialog."""
from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from .. import engine, system
from . import common

SCHEDULE_OPTIONS = [("off", "Manual only"), ("daily", "Every day"), ("weekly", "Every week")]
MODE_OPTIONS = [("contents", "Shred contents, keep the folder"), ("whole", "Shred the folder itself")]


def folder_summary(folder: dict, default_method: str) -> str:
    method = engine.get_method(folder.get("method") or default_method)
    bits = [f"{method.name} ({method.short})"]
    bits.append("contents only" if folder.get("keep_root", True) else "entire folder")
    if folder.get("older_than_days"):
        bits.append(f"older than {folder['older_than_days']:g} days")
    if not folder.get("include_hidden", True):
        bits.append("skips hidden")
    sched = dict(SCHEDULE_OPTIONS).get(folder.get("schedule", "off"), "Manual only")
    bits.append(sched.lower() if folder.get("schedule", "off") != "off" else "manual")
    return " · ".join(bits)


class FolderDialog(Adw.Dialog):
    def __init__(self, win, folder: dict, is_new: bool = False):
        super().__init__(title="Add Folder" if is_new else "Folder Settings", content_width=560)
        self.win = win
        self.folder = dict(folder)

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        save = Gtk.Button(label="Add" if is_new else "Save", css_classes=["suggested-action"])
        save.connect("clicked", lambda *_: self._save())
        header.pack_end(save)
        tv.add_top_bar(header)

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        self.name = Adw.EntryRow(title="Name", text=self.folder["label"])
        g.add(self.name)
        self.path_row = Adw.ActionRow(title="Location", use_markup=False,
                                      subtitle=system.short_path(self.folder["path"]))
        self.path_row.add_css_class("property")
        change = Gtk.Button(label="Change…", valign=Gtk.Align.CENTER, css_classes=["flat"])
        change.connect("clicked", lambda *_: common.pick_files(self.win, self._pick, folders=True))
        self.path_row.add_suffix(change)
        g.add(self.path_row)
        page.add(g)

        g = Adw.PreferencesGroup(title="Shredding")
        default = engine.get_method(win.store.settings.get("method"))
        method_opts = [("", f"Use default ({default.name})")] + [
            (m.id, f"{m.name}  ·  {m.short}") for m in engine.METHODS.values()]
        self.method = common.string_combo("Method", method_opts, self.folder.get("method") or "")
        g.add(self.method)
        self.mode = common.string_combo("What to shred", MODE_OPTIONS,
                                        "contents" if self.folder.get("keep_root", True) else "whole")
        self.mode.connect("notify::selected", lambda *_: self._sync())
        g.add(self.mode)
        self.age = Adw.SpinRow.new_with_range(0, 3650, 1)
        self.age.set_title("Only items older than (days)")
        self.age.set_subtitle("0 shreds everything. Great for Downloads: keep this week's files.")
        self.age.set_value(self.folder.get("older_than_days", 0))
        g.add(self.age)
        self.hidden = Adw.SwitchRow(title="Include hidden files", active=self.folder.get("include_hidden", True))
        g.add(self.hidden)
        page.add(g)

        g = Adw.PreferencesGroup(title="Schedule",
                                 description="Scheduled runs happen in the background, even when Shreder "
                                             "is closed, and always keep the folder itself.")
        self.schedule = common.string_combo("Automatically shred", SCHEDULE_OPTIONS,
                                            self.folder.get("schedule", "off"))
        g.add(self.schedule)
        page.add(g)

        tv.set_content(page)
        self.set_child(tv)
        self._sync()

    def _pick(self, paths):
        path = paths[0]
        reason = system.check_target(path, keep_root=True)
        if reason:
            self.win.toast(reason)
            return
        self.folder["path"] = path
        self.path_row.set_subtitle(system.short_path(path))
        if not self.name.get_text().strip():
            self.name.set_text(os.path.basename(path))

    def _sync(self):
        whole = common.combo_key(self.mode) == "whole"
        self.schedule.set_sensitive(not whole)
        if whole:
            self.schedule.set_selected(0)
            self.schedule.set_subtitle("Not available when the folder itself is shredded")
        else:
            self.schedule.set_subtitle("")

    def _save(self):
        f = self.folder
        f["label"] = self.name.get_text().strip() or os.path.basename(f["path"])
        f["method"] = common.combo_key(self.method) or None
        f["keep_root"] = common.combo_key(self.mode) == "contents"
        f["older_than_days"] = self.age.get_value()
        f["include_hidden"] = self.hidden.get_active()
        f["schedule"] = common.combo_key(self.schedule) if f["keep_root"] else "off"
        self.win.store.save_folder(f)
        self.win.sync_scheduler()
        self.close()


class FoldersPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.sizes: dict[str, tuple] = {}

        self.stack = Gtk.Stack()
        self.append(self.stack)

        empty = Adw.StatusPage(icon_name="starred-symbolic", title="No Remembered Folders",
                               description="Remember folders you clean out regularly — Downloads, "
                                           "a scratch folder, an export directory — and shred them in "
                                           "one click or on a schedule.")
        empty.set_child(common.pill_button("Add Folder…", self.add, "suggested-action"))
        self.stack.add_named(empty, "empty")

        content = common.page_box()
        self.shred_all_btn = common.pill_button("Shred All", self.shred_all, "destructive-action",
                                                icon="edit-delete-symbolic")
        actions = common.button_row(common.pill_button("Add Folder…", self.add, icon="list-add-symbolic"),
                                    self.shred_all_btn)
        content.append(common.section_header(
            "Remembered folders",
            "Click a folder to change its method, age filter or schedule.", actions))
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        content.append(self.list)
        self.sched_label = Gtk.Label(wrap=True, xalign=0, css_classes=["dim-label", "caption"])
        content.append(self.sched_label)
        self.stack.add_named(common.scrolled_page(content, 920), "list")

    def add(self):
        def picked(paths):
            for path in paths:
                reason = system.check_target(path, keep_root=True)
                if reason:
                    self.win.toast(reason)
                    continue
                if any(f["path"] == os.path.abspath(path) for f in self.win.store.folders):
                    self.win.toast("That folder is already remembered")
                    continue
                if len(paths) == 1:
                    from ..store import new_folder
                    FolderDialog(self.win, new_folder(path), is_new=True).present(self.win)
                else:
                    self.win.store.add_folder(path)
        common.pick_files(self.win, picked, folders=True, title="Remember Folders")

    def shred_all(self):
        self.win.shred_folders(list(self.win.store.folders))

    def refresh(self):
        folders = self.win.store.folders
        self.stack.set_visible_child_name("list" if folders else "empty")
        common.clear_box(self.list)
        default = self.win.store.settings.get("method")
        for f in folders:
            self.list.append(self._row(f, default))
        self.shred_all_btn.set_sensitive(bool(folders))
        n_sched = sum(1 for f in folders if f.get("schedule", "off") != "off")
        self.sched_label.set_label(
            f"{n_sched} folder{'s' if n_sched != 1 else ''} on a schedule. A background timer checks hourly "
            "and shreds whatever is due." if n_sched else
            "Tip: put a folder on a schedule to have it shredded automatically in the background.")

    def _row(self, f: dict, default: str) -> Gtk.Widget:
        exists = os.path.isdir(f["path"])
        row = Adw.ActionRow(title=GLib.markup_escape_text(f["label"]), activatable=True, subtitle_lines=3)
        subtitle = (f"{system.short_path(f['path'])}\n{folder_summary(f, default)}"
                    f" · last shredded {common.time_ago(f.get('last_run'))}")
        row.set_subtitle(GLib.markup_escape_text(subtitle))
        row.set_tooltip_text(f["path"])
        icon = Gtk.Image(icon_name="folder-symbolic" if exists else "dialog-warning-symbolic",
                         pixel_size=24)
        row.add_prefix(icon)
        row.connect("activated", lambda *_: FolderDialog(self.win, f).present(self.win))

        if not exists:
            row.add_suffix(common.badge("MISSING", "warning"))
        else:
            size = Gtk.Label(css_classes=["dim-label", "numeric"], xalign=1)
            cached = self.sizes.get(f["path"])
            size.set_label(system.human_size(cached[0]) if cached else "…")
            row.add_suffix(size)

            def measured(res, lbl=size, p=f["path"], hidden=f.get("include_hidden", True)):
                if isinstance(res, Exception):
                    return
                self.sizes[p] = res
                lbl.set_label(system.human_size(res[0]))
                lbl.set_tooltip_text(f"{res[1]:,} items")
            common.run_async(lambda p=f["path"], h=f.get("include_hidden", True): system.path_size(p, h),
                             measured)
            shred = Gtk.Button(label="Shred", valign=Gtk.Align.CENTER,
                               css_classes=["destructive-action"])
            shred.connect("clicked", lambda *_: self.win.shred_folders([f]))
            row.add_suffix(shred)
        items = [("Settings…", lambda: FolderDialog(self.win, f).present(self.win))]
        if exists:
            items.append(("Open Folder", lambda: common.open_folder(self.win, f["path"])))
        items += [None, ("Forget Folder", lambda: self._forget(f), "error")]
        row.add_suffix(common.menu_button(items, tooltip="Folder actions"))
        return row

    def _forget(self, f):
        self.win.store.remove_folder(f["id"])
        self.win.sync_scheduler()
        self.win.toast(f"Forgot “{f['label']}” — the folder itself was not touched")
