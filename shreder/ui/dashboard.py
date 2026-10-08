"""Command Center: status, stats, one-click actions, drives and recent activity."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import system
from . import common
from .activity import history_row
from .wipe import usage_bar


class DashboardPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        content = common.page_box()

        # ---------------------------------------------------------- hero
        hero = Gtk.Box(spacing=16)
        self.hero_icon = Gtk.Image(icon_name=win.app.get_application_id(), pixel_size=64)
        hero.append(self.hero_icon)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label="Command Center", xalign=0, wrap=True, css_classes=["hero-title"]))
        text.append(Gtk.Label(label="Destroy files beyond recovery, wipe free space and clear traces.",
                              xalign=0, wrap=True, css_classes=["hero-sub"]))
        hero.append(text)
        self.status = Gtk.Label(label="Ready", valign=Gtk.Align.CENTER, css_classes=["status-pill"])
        hero.append(self.status)
        content.append(hero)

        # ---------------------------------------------------------- stats
        self.stat_files = common.StatCard("edit-delete-symbolic", "Files shredded")
        self.stat_data = common.StatCard("security-high-symbolic", "Data destroyed")
        self.stat_written = common.StatCard("drive-harddisk-symbolic", "Total overwritten")
        self.stat_ops = common.StatCard("document-open-recent-symbolic", "Operations")
        stats = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                            min_children_per_line=2, max_children_per_line=4,
                            column_spacing=12, row_spacing=12)
        for card in (self.stat_files, self.stat_data, self.stat_written, self.stat_ops):
            stats.append(card)
        for child in common._children(stats):
            child.set_focusable(False)
        content.append(stats)

        # ---------------------------------------------------------- quick actions
        content.append(common.section_header("Quick actions"))
        self.tile_trash = common.ActionTile("user-trash-full-symbolic", "Empty Trash Securely", "Checking…",
                                            lambda: win.sweep("trash"), danger=True)
        self.tile_thumbs = common.ActionTile("image-x-generic-symbolic", "Shred Thumbnail Cache",
                                             "Checking…", lambda: win.sweep("thumbnails"), danger=True)
        self.tile_saved = common.ActionTile("starred-symbolic", "Shred Remembered Folders", "",
                                            self._shred_saved, danger=True)
        tiles = [
            common.ActionTile("document-new-symbolic", "Shred Files…", "Pick files to destroy",
                              lambda: win.pages["shred"].add_files()),
            common.ActionTile("folder-new-symbolic", "Shred Folders…", "Pick whole folders to destroy",
                              lambda: win.pages["shred"].add_folders()),
            common.ActionTile("drive-harddisk-symbolic", "Wipe Free Space",
                              "Erase traces of files deleted the normal way", lambda: win.navigate("wipe")),
            self.tile_trash,
            self.tile_thumbs,
            self.tile_saved,
            common.ActionTile("document-open-recent-symbolic", "Clear Recent Files",
                              "Shred the list of recently opened documents", lambda: win.sweep("recent"),
                              danger=True),
            common.ActionTile("list-add-symbolic", "Remember a Folder…",
                              "Save a folder for one-click or scheduled shredding",
                              lambda: win.pages["folders"].add()),
            common.ActionTile("help-about-symbolic", "Which Steps Do I Need?",
                              "Step-by-step guides for SSDs, hard disks, USB sticks and more",
                              lambda: win.navigate("guide")),
        ]
        self.tiles = common.tile_grid(tiles)
        content.append(self.tiles)

        # ---------------------------------------------------------- remembered folders
        manage = common.pill_button("Manage", lambda: win.navigate("folders"), "flat")
        content.append(common.section_header("Remembered folders", None, manage))
        self.folder_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        ph = Adw.ActionRow(title="No remembered folders",
                           subtitle="Save Downloads or any folder you clean out often", activatable=True)
        ph.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        ph.connect("activated", lambda *_: win.pages["folders"].add())
        self.folder_list.set_placeholder(ph)
        content.append(self.folder_list)

        # ---------------------------------------------------------- drives
        content.append(common.section_header("Drives", None,
                                             common.pill_button("Wipe…", lambda: win.navigate("wipe"), "flat")))
        self.drive_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.drive_list.set_placeholder(Adw.ActionRow(title="Scanning drives…"))
        content.append(self.drive_list)

        # ---------------------------------------------------------- activity
        content.append(common.section_header("Recent activity", None,
                                             common.pill_button("View All", lambda: win.navigate("activity"),
                                                                "flat")))
        self.recent_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.recent_list.set_placeholder(Adw.ActionRow(title="Nothing shredded yet",
                                                       subtitle="Your first operation will appear here"))
        content.append(self.recent_list)

        self.append(common.scrolled_page(content, 1040))
        GLib.idle_add(lambda: (win.pages["wipe"].load(), False)[1])

    # ------------------------------------------------------------------ updates
    def _shred_saved(self):
        if not self.win.store.folders:
            self.win.pages["folders"].add()
        else:
            self.win.shred_folders(list(self.win.store.folders))

    def refresh(self):
        s = self.win.store.stats
        self.stat_files.set_value(f"{s.get('files', 0):,}")
        self.stat_data.set_value(system.human_size(s.get("bytes_data", 0)))
        self.stat_written.set_value(system.human_size(s.get("bytes_written", 0)))
        self.stat_ops.set_value(f"{s.get('operations', 0):,}")

        folders = self.win.store.folders
        n = len(folders)
        self.tile_saved.set_subtitle(f"{n} folder{'s' if n != 1 else ''} in one click" if n
                                     else "No folders remembered yet")
        common.clear_box(self.folder_list)
        default = self.win.store.settings.get("method")
        from .folders import folder_summary
        for f in folders[:5]:
            row = Adw.ActionRow(title=GLib.markup_escape_text(f["label"]),
                                subtitle=GLib.markup_escape_text(
                                    f"{system.short_path(f['path'])} · last shredded "
                                    f"{common.time_ago(f.get('last_run'))}"),
                                activatable=True)
            row.set_tooltip_text(folder_summary(f, default))
            row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
            if f.get("schedule", "off") != "off":
                row.add_suffix(common.badge(f["schedule"].upper(), "accent"))
            btn = Gtk.Button(label="Shred", valign=Gtk.Align.CENTER,
                             css_classes=["destructive-action", "pill", "small"])
            btn.connect("clicked", lambda *_, f=f: self.win.shred_folders([f]))
            row.add_suffix(btn)
            row.connect("activated", lambda *_: self.win.navigate("folders"))
            self.folder_list.append(row)

        common.clear_box(self.recent_list)
        for e in self.win.store.history(4):
            self.recent_list.append(history_row(e))
        self._refresh_sweeps()
        self.on_jobs_changed()

    def _refresh_sweeps(self):
        def measure():
            return system.dirs_size(system.trash_dirs()), system.dirs_size(system.thumbnail_dirs())

        def done(res):
            if isinstance(res, Exception):
                return
            (tb, tn), (hb, hn) = res
            self.tile_trash.set_subtitle(f"{tn:,} items · {system.human_size(tb)}" if tn else "Trash is empty")
            self.tile_thumbs.set_subtitle(f"{hn:,} previews · {system.human_size(hb)}" if hn
                                          else "No cached previews")
        common.run_async(measure, done)

    def set_volumes(self, vols):  # called by the window when drives are rescanned
        common.clear_box(self.drive_list)
        for v in vols:
            row = Adw.ActionRow(title=GLib.markup_escape_text(v.name),
                                subtitle=GLib.markup_escape_text(
                                    f"{v.mountpoint} · {v.fstype} · {system.human_size(v.free)} free "
                                    f"of {system.human_size(v.total)}"),
                                activatable=True)
            row.add_prefix(Gtk.Image(icon_name=v.icon, pixel_size=24))
            row.add_suffix(usage_bar(v, 120))
            row.add_suffix(common.badge(v.kind, "accent" if v.is_ssd else ""))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))

            def open_wipe(*_, mp=v.mountpoint):
                self.win.pages["wipe"].select(mp)
                self.win.navigate("wipe")
            row.connect("activated", open_wipe)
            self.drive_list.append(row)

    def on_jobs_changed(self):
        jobs = self.win.jobs
        if jobs.current:
            extra = f" +{len(jobs.pending)}" if jobs.pending else ""
            self.status.set_label(f"Working{extra}")
            self.status.add_css_class("busy")
        else:
            self.status.set_label("Ready")
            self.status.remove_css_class("busy")
            if jobs.finished:
                self._refresh_sweeps()
