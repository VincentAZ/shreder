"""Shred Files page: drop zone, queue, method options, the big red button."""
from __future__ import annotations

import os

from gi.repository import Adw, Gdk, GLib, Gtk, Pango

from .. import system
from . import common


class ShredPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.items: dict[str, dict] = {}

        content = common.page_box()

        # ---------------------------------------------------------- drop zone
        self.drop = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["drop-zone"])
        self.drop.append(Gtk.Image(icon_name="shreder-symbolic", pixel_size=56, css_classes=["drop-icon"]))
        self.drop.append(Gtk.Label(label="Drop files and folders here", css_classes=["drop-title"]))
        self.drop.append(Gtk.Label(
            label="Each item is overwritten with your chosen pattern, renamed to hide its name, then deleted.",
            wrap=True, justify=Gtk.Justification.CENTER, css_classes=["dim-label"]))
        buttons = common.button_row(
            common.pill_button("Add Files…", self.add_files, icon="document-new-symbolic"),
            common.pill_button("Add Folders…", self.add_folders, icon="folder-new-symbolic"), center=True)
        buttons.set_margin_top(6)
        self.drop.append(buttons)
        content.append(self.drop)

        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("drop", self._on_drop)
        target.connect("enter", lambda *_: (self.drop.add_css_class("drag-hover"), Gdk.DragAction.COPY)[1])
        target.connect("leave", lambda *_: self.drop.remove_css_class("drag-hover"))
        self.add_controller(target)

        hint_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.hint = Adw.ActionRow(title="Not sure which steps you need?",
                                  subtitle="The guides walk you through each situation in the right order",
                                  subtitle_lines=0, activatable=True)
        self.hint.add_prefix(Gtk.Image(icon_name="help-about-symbolic"))
        self.hint.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.hint_target = None
        self.hint.connect("activated", lambda *_: self.win.pages["guide"].focus(self.hint_target)
                          if self.hint_target else self.win.navigate("guide"))
        hint_list.append(self.hint)
        content.append(hint_list)

        # ---------------------------------------------------------- queue
        content.append(common.section_header("Shred queue", "Everything here is destroyed when you press Shred"))
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        empty = Adw.ActionRow(title="Nothing queued yet",
                              subtitle="Add files, drag them in, or use “Shred with Shreder” from the Files app")
        empty.add_prefix(Gtk.Image(icon_name="view-list-symbolic", css_classes=["dim-label"]))
        self.list.set_placeholder(empty)
        content.append(self.list)

        # ---------------------------------------------------------- options
        opts = Adw.PreferencesGroup(title="Method")
        self.method = common.method_row(win.store.settings.get("method"))
        opts.add(self.method)
        self.verify = Adw.SwitchRow(title="Verify final pass",
                                    subtitle="Read every block back from the disk to prove it was overwritten",
                                    active=win.store.settings.get("verify", True))
        opts.add(self.verify)
        content.append(opts)


        content.append(Gtk.Label(
            label="Shredding is permanent. On SSDs and copy-on-write filesystems old copies can survive "
                  "elsewhere on the drive — follow up with Wipe Free Space and TRIM.",
            wrap=True, xalign=0, css_classes=["dim-label", "caption"]))
        self.append(common.scrolled_page(content, 860))

        # ---------------------------------------------------------- pinned action bar
        bar = Gtk.Box(spacing=12, css_classes=["page-bar"])
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
        self.count_label = Gtk.Label(xalign=0, css_classes=["page-bar-count"], ellipsize=Pango.EllipsizeMode.END)
        self.size_label = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"],
                                    ellipsize=Pango.EllipsizeMode.END)
        text.append(self.count_label)
        text.append(self.size_label)
        bar.append(text)
        self.clear_btn = common.icon_button("edit-clear-all-symbolic", "Clear the queue", self.clear)
        bar.append(self.clear_btn)
        self.shred_btn = Gtk.Button(css_classes=["pill", "shred-button"], valign=Gtk.Align.CENTER)
        self.shred_btn.set_child(Adw.ButtonContent(icon_name="edit-delete-symbolic", label="Shred"))
        self.shred_btn.connect("clicked", lambda *_: self.shred())
        bar.append(self.shred_btn)
        self.append(bar)
        self._sync()

    # ------------------------------------------------------------------ queue management
    def add_files(self):
        common.pick_files(self.win, self._added_from_dialog)

    def add_folders(self):
        common.pick_files(self.win, self._added_from_dialog, folders=True)

    def _added_from_dialog(self, paths):
        self.add_paths(paths)
        self.win.navigate("shred")

    def _on_drop(self, _target, value, _x, _y):
        self.drop.remove_css_class("drag-hover")
        paths = [f.get_path() for f in value.get_files() if f.get_path()]
        self.add_paths(paths)
        return True

    def add_paths(self, paths):
        added = 0
        for path in paths:
            path = os.path.abspath(path)
            if path in self.items:
                continue
            reason = system.check_target(path)
            if reason:
                self.win.toast(reason)
                continue
            self._add_row(path)
            added += 1
        if added:
            self._sync()
            self.win.update_counts()

    def _add_row(self, path: str):
        is_dir = os.path.isdir(path) and not os.path.islink(path)
        row = Adw.ActionRow(title=os.path.basename(path) or path, use_markup=False,
                            subtitle=system.short_path(os.path.dirname(path)), title_lines=1)
        row.set_tooltip_text(path)
        row.add_prefix(Gtk.Image(icon_name="folder-symbolic" if is_dir else "text-x-generic-symbolic"))
        size = Gtk.Label(label="…", css_classes=["dim-label", "numeric"])
        row.add_suffix(size)
        if is_dir:
            row.add_suffix(common.icon_button("starred-symbolic", "Remember this folder",
                                              lambda p=path: self._remember(p)))
        row.add_suffix(common.icon_button("window-close-symbolic", "Remove from queue",
                                          lambda p=path: self.remove(p)))
        self.list.append(row)
        entry = {"row": row, "size": None, "count": None, "is_dir": is_dir}
        self.items[path] = entry

        def measured(result, p=path):
            if p not in self.items or isinstance(result, Exception):
                return
            b, n = result
            self.items[p]["size"], self.items[p]["count"] = b, n
            size.set_label(f"{n:,} items · {system.human_size(b)}" if is_dir else system.human_size(b))
            self._sync()
        common.run_async(lambda p=path: system.path_size(p), measured)

    def _remember(self, path):
        folder = self.win.store.add_folder(path)
        if folder:
            self.win.toast(f"“{folder['label']}” added to Remembered Folders")
        else:
            self.win.toast("Already remembered")

    def remove(self, path):
        entry = self.items.pop(path, None)
        if entry:
            self.list.remove(entry["row"])
        self._sync()
        self.win.update_counts()

    def clear(self):
        for path in list(self.items):
            self.remove(path)

    def _sync(self):
        n = len(self.items)
        known = [e for e in self.items.values() if e["size"] is not None]
        total = sum(e["size"] for e in known)
        files = sum(e["count"] for e in known)
        if n == 0:
            self.count_label.set_label("Queue is empty")
            self.size_label.set_label("Add or drop files to get started")
        else:
            approx = "" if len(known) == n else "at least "
            self.count_label.set_label(f"{n} item{'s' if n != 1 else ''} queued")
            self.size_label.set_label(f"{approx}{files:,} file{'s' if files != 1 else ''} · "
                                      f"{system.human_size(total)}")
        self.clear_btn.set_visible(n > 0)
        self.shred_btn.set_sensitive(n > 0)
        label = f"Shred {n} Item{'s' if n != 1 else ''}" if n else "Shred"
        self.shred_btn.get_child().set_label(label)

    # ------------------------------------------------------------------ action
    def shred(self):
        if not self.items:
            return
        paths = list(self.items)
        title = (f"Shred {os.path.basename(paths[0])}" if len(paths) == 1
                 else f"Shred {len(paths)} items")

        def queued():
            for p in paths:
                self.remove(p)
        self.win.request_shred([(p, False) for p in paths], title=title,
                               method_id=common.get_method_id(self.method),
                               verify=self.verify.get_active(), on_queued=queued)

    def set_volumes(self, vols):
        home = system.volume_for(system.HOME, vols)
        if home and home.is_ssd:
            self.hint.set_title(f"Your {GLib.markup_escape_text(home.name)} is an SSD — shredding is step 1 of 3")
            self.hint.set_subtitle("Afterwards: wipe free space with one random pass, then run TRIM. "
                                   "Tip: Random Data, 1 pass is enough on SSDs.")
            self.hint_target = "ssd-files"
        elif home:
            self.hint.set_title(f"Your {GLib.markup_escape_text(home.name)} is a hard disk")
            self.hint.set_subtitle("Shredding fully overwrites the file. For older copies, wipe free space "
                                   "afterwards.")
            self.hint_target = "hdd-files"

    def refresh(self):
        pass

    def on_shown(self):
        pass
