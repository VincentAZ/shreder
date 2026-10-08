"""Guides page: step-by-step recipes for common situations, tailored to the user's drives."""
from __future__ import annotations

import shutil
import subprocess

from gi.repository import Adw, GLib, Graphene, Gtk, Pango

from .. import system
from . import common

# Each step: (title, explanation, optional (button label, action key)).
# Action keys are handled by GuidePage._do.
SCENARIOS = [
    {
        "id": "ssd-files",
        "icon": "shreder-symbolic",
        "title": "Delete sensitive files on an SSD",
        "when": "The files are on an SSD, NVMe drive or USB flash drive.",
        "why": "An SSD spreads writes across spare memory cells, so overwriting a file can land on fresh "
               "cells while the old ones keep the data until the drive erases them. Shredding removes the "
               "file, wiping free space catches temporary and autosave copies the filesystem still controls, "
               "and TRIM tells the drive to erase every block that is no longer in use.",
        "steps": [
            ("Shred the files", "Use Random Data, 1 pass. More passes only add wear on flash memory "
                                "without reaching any more cells.", ("Shred Files…", "shred")),
            ("Wipe free space", "One random pass over the drive's free space covers copies left behind "
                                "by apps (temp files, autosaves, old versions).", ("Open Wipe Free Space", "wipe")),
            ("Run TRIM", "The drive discards every unused block itself. Asks for your password.",
             ("Run TRIM", "trim")),
        ],
        "routine": "ssd-files",
    },
    {
        "id": "hdd-files",
        "icon": "drive-harddisk-symbolic",
        "title": "Delete sensitive files on a hard disk",
        "when": "The files are on a spinning hard disk (HDD).",
        "why": "Hard disks overwrite data exactly where it was, so shredding is fully effective for the file "
               "itself. A free-space wipe afterwards destroys older copies the file may have left behind.",
        "steps": [
            ("Shred the files", "DoD 5220.22-M (3 passes) is the standard choice. One pass is already "
                                "enough on modern disks; Gutmann is only for very old drives.", ("Shred Files…", "shred")),
            ("Wipe free space (recommended)", "Overwrites leftovers from earlier saves, temp files and files "
                                              "you deleted normally.", ("Open Wipe Free Space", "wipe")),
        ],
        "routine": "hdd-files",
    },
    {
        "id": "deleted",
        "icon": "user-trash-symbolic",
        "title": "Files I already deleted the normal way",
        "when": "You emptied the Trash or pressed Shift+Delete, and want the data really gone.",
        "why": "A normal delete only removes the file's name. Its data stays in free space until something "
               "overwrites it. The file can't be shredded any more because it no longer has a name, but "
               "wiping free space overwrites exactly the blocks where it was.",
        "steps": [
            ("Wipe free space on that drive", "One random pass is enough. The drive will be full while this "
                                              "runs.", ("Open Wipe Free Space", "wipe")),
            ("SSD only: run TRIM", "Lets the drive erase cells that still hold old copies.", ("Run TRIM", "trim")),
        ],
        "routine": "deleted",
    },
    {
        "id": "traces",
        "icon": "preferences-system-privacy-symbolic",
        "title": "Clear everyday traces",
        "when": "You want to remove what normal use leaves behind: trash, previews and history.",
        "why": "The Trash keeps deleted files intact, the thumbnail cache keeps small previews of images and "
               "documents you've viewed, and the recent-files list records what you opened.",
        "steps": [
            ("Empty the Trash securely", "Shreds everything in the Trash, including trash folders on other "
                                         "drives.", ("Empty Trash", "trash")),
            ("Shred the thumbnail cache", "Previews are rebuilt automatically when needed.",
             ("Shred Thumbnails", "thumbnails")),
            ("Clear recent files", "Removes the list of recently opened documents.", ("Clear Recent", "recent")),
            ("Optional: wipe free space", "Also catches anything deleted earlier without Shreder.",
             ("Open Wipe Free Space", "wipe")),
        ],
        "routine": "traces",
    },
    {
        "id": "downloads",
        "icon": "folder-download-symbolic",
        "title": "Keep Downloads clean automatically",
        "when": "A folder keeps filling up with things you don't need to keep, like Downloads or exports.",
        "why": "Remembered folders can be shredded on a schedule in the background, even when Shreder is "
               "closed. An age filter keeps anything recent.",
        "steps": [
            ("Remember the folder", "Choose Downloads (or any folder).", ("Remember a Folder…", "remember")),
            ("Keep recent files", "In its settings, set “Only items older than” to 7 or 30 days.", None),
            ("Put it on a schedule", "Choose Every day or Every week. The folder itself is always kept.", None),
        ],
    },
    {
        "id": "usb",
        "icon": "media-removable-symbolic",
        "title": "Clean a USB stick or SD card before giving it away",
        "when": "You're handing a removable drive to someone else.",
        "why": "Flash cards and sticks have the same limits as SSDs, but most can't be trimmed. The safest "
               "option is to erase the whole device in GNOME Disks. If you want to keep some files on it, "
               "shred the rest and wipe its free space.",
        "steps": [
            ("Shred the drive's contents", "Choose the drive (or folders on it). The drive's top folder is "
                                           "kept and everything inside it is shredded.", ("Shred Folders…", "shred-folder")),
            ("Wipe its free space", "Select the drive on the Wipe Free Space page and use one random pass.",
             ("Open Wipe Free Space", "wipe")),
            ("Most thorough: erase the whole device", "In Disks, choose the drive → ⋮ → Format Disk → "
                                                      "Erase: Overwrite existing data with zeroes.", ("Open Disks", "disks")),
        ],
        "routine": "usb",
    },
    {
        "id": "dispose",
        "icon": "computer-symbolic",
        "title": "Selling or recycling a whole computer or drive",
        "when": "The entire drive is leaving your hands.",
        "why": "Shredding files can't reach the operating system's own copies, swap or the SSD's hidden spare "
               "area. Erase the whole drive from outside the running system instead.",
        "steps": [
            ("Back up what you need", "Everything on the drive will be destroyed.", None),
            ("Boot from a live USB", "So the drive isn't in use while it's erased.", None),
            ("Use the drive's built-in erase", "NVMe: “nvme format -s 1” (or “nvme sanitize”). SATA SSD: "
                                               "“hdparm --security-erase”. Hard disk: Disks → Format Disk → "
                                               "Overwrite with zeroes.", None),
            ("If the drive is encrypted (LUKS)", "Destroying the encryption header (“cryptsetup erase”) makes "
                                                 "all data unreadable within seconds.", None),
        ],
    },
    {
        "id": "cow",
        "icon": "drive-multidisk-symbolic",
        "title": "btrfs or ZFS drives (snapshots)",
        "when": "Your drive uses a copy-on-write filesystem, or you use Timeshift or Snapper snapshots.",
        "why": "These filesystems never overwrite data in place, and snapshots keep old versions of every "
               "file. Shredding the file you can see leaves the copies in snapshots untouched.",
        "steps": [
            ("Delete snapshots that contain the files", "Use Timeshift, Snapper or “btrfs subvolume delete”.", None),
            ("Shred the files", "Random Data, 1 pass.", ("Shred Files…", "shred")),
            ("Wipe free space, then TRIM if it's an SSD", "Some passes may end early on these filesystems; "
                                                          "that's expected.", ("Open Wipe Free Space", "wipe")),
        ],
    },
]


class ScenarioCard(Gtk.Box):
    def __init__(self, page, sc: dict):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, css_classes=["card", "scenario-card"])
        self.page = page
        self.sc = sc

        header = Gtk.Button(css_classes=["flat", "scenario-header"])
        hbox = Gtk.Box(spacing=14)
        hbox.append(Gtk.Image(icon_name=sc["icon"], pixel_size=22, valign=Gtk.Align.CENTER,
                              css_classes=["tile-icon"]))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(Gtk.Label(label=sc["title"], xalign=0, wrap=True, css_classes=["tile-title"]))
        text.append(Gtk.Label(label=sc["when"], xalign=0, wrap=True, css_classes=["tile-sub"]))
        hbox.append(text)
        self.badge = common.badge("FOR YOUR DRIVE", "success")
        self.badge.set_visible(False)
        hbox.append(self.badge)
        self.chevron = Gtk.Image(icon_name="pan-end-symbolic", valign=Gtk.Align.CENTER)
        hbox.append(self.chevron)
        header.set_child(hbox)
        header.connect("clicked", lambda *_: self.set_expanded(not self.revealer.get_reveal_child()))
        self.append(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["scenario-body"])
        why = Gtk.Box(spacing=10, css_classes=["scenario-why"])
        why.append(Gtk.Image(icon_name="dialog-information-symbolic", valign=Gtk.Align.START))
        why.append(Gtk.Label(label=sc["why"], xalign=0, wrap=True, hexpand=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR))
        body.append(why)

        for i, (title, detail, action) in enumerate(sc["steps"], 1):
            row = Gtk.Box(spacing=14)
            num = Gtk.Label(label=str(i), valign=Gtk.Align.START, css_classes=["step-num"])
            row.append(num)
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
            col.append(Gtk.Label(label=title, xalign=0, wrap=True, css_classes=["step-title"]))
            col.append(Gtk.Label(label=detail, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR,
                                 css_classes=["dim-label"]))
            if action:
                label, key = action
                btn = Gtk.Button(label=label, halign=Gtk.Align.START, margin_top=4,
                                 css_classes=["pill", "small", "step-button"])
                btn.connect("clicked", lambda *_, k=key: page.do(k))
                col.append(btn)
            row.append(col)
            body.append(row)

        if sc.get("routine"):
            foot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=4)
            run = Gtk.Button(css_classes=["pill", "suggested-action"], halign=Gtk.Align.START)
            run.set_child(Adw.ButtonContent(icon_name="media-playback-start-symbolic", label="Run All Steps…"))
            run.connect("clicked", lambda *_: page.win.run_routine(sc["routine"]))
            foot.append(run)
            foot.append(Gtk.Label(label="Shreder queues every step in order and shows one summary to confirm. "
                                        "Cancelling a step stops the rest.",
                                  xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
            body.append(foot)

        self.revealer = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.revealer.set_child(body)
        self.append(self.revealer)

    def set_expanded(self, expanded: bool):
        self.revealer.set_reveal_child(expanded)
        self.chevron.set_from_icon_name("pan-down-symbolic" if expanded else "pan-end-symbolic")

    def set_recommended(self, on: bool, label: str = "FOR YOUR DRIVE"):
        self.badge.set_label(label)
        self.badge.set_visible(on)


class GuidePage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.content = common.page_box()

        self.content.append(common.section_header(
            "Which steps do I need?",
            "Pick the situation that matches yours. Each guide explains why the steps matter and in "
            "what order to do them."))

        # the three tools, in the order they're used
        flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                           min_children_per_line=1, max_children_per_line=3, column_spacing=10, row_spacing=10)
        for n, icon, title, text in (
            ("1", "shreder-symbolic", "Shred",
             "Overwrites a file's own data, scrambles its name and deletes it."),
            ("2", "drive-harddisk-symbolic", "Wipe free space",
             "Overwrites the space where files deleted earlier still linger."),
            ("3", "system-run-symbolic", "TRIM (SSDs)",
             "Tells the SSD to erase unused cells that software can't reach."),
        ):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["card", "tool-card"])
            head = Gtk.Box(spacing=8)
            head.append(Gtk.Label(label=n, css_classes=["step-num"]))
            head.append(Gtk.Image(icon_name=icon))
            head.append(Gtk.Label(label=title, xalign=0, css_classes=["step-title"]))
            card.append(head)
            card.append(Gtk.Label(label=text, xalign=0, wrap=True, max_width_chars=24,
                                  css_classes=["dim-label", "caption"]))
            flow.append(card)
        for child in common._children(flow):
            child.set_focusable(False)
        self.content.append(flow)

        self.setup = Adw.ActionRow(title="Checking your drives…", subtitle_lines=0)
        self.setup.add_prefix(Gtk.Image(icon_name="computer-symbolic", pixel_size=24))
        setup_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        setup_list.append(self.setup)
        self.content.append(setup_list)

        self.content.append(common.section_header("Guides"))
        self.cards: dict[str, ScenarioCard] = {}
        for sc in SCENARIOS:
            card = ScenarioCard(self, sc)
            self.cards[sc["id"]] = card
            self.content.append(card)

        self.scroller = common.scrolled_page(self.content, 860)
        self.append(self.scroller)
        self._recommended: str | None = None

    # ------------------------------------------------------------------ drive awareness
    def set_volumes(self, vols):
        home = system.volume_for(system.HOME, vols)
        for card in self.cards.values():
            card.set_recommended(False)
        rec = None
        if home:
            if home.is_ssd or home.removable:
                rec = "ssd-files"
                advice = "shred → wipe free space (1 random pass) → TRIM."
            else:
                rec = "hdd-files"
                advice = "shred → wipe free space. Shredding alone fully overwrites the file on a hard disk."
            self.setup.set_title(GLib.markup_escape_text(
                f"Your {home.name} is {'an' if home.kind[0] in 'AEIOUN' else 'a'} {home.kind} ({home.fstype})"))
            self.setup.set_subtitle(GLib.markup_escape_text(f"Recommended order for sensitive files: {advice}"))
            if home.is_cow:
                self.cards["cow"].set_recommended(True)
        else:
            self.setup.set_title("Couldn't identify your system drive")
            self.setup.set_subtitle("Use the SSD guide if unsure — it is safe for every drive type.")
        if any(v.removable or v.transport == "usb" for v in vols):
            self.cards["usb"].set_recommended(True, "USB DRIVE CONNECTED")
        if rec:
            self.cards[rec].set_recommended(True)
            if self._recommended is None:
                self.cards[rec].set_expanded(True)
            self._recommended = rec

    # ------------------------------------------------------------------ actions
    def focus(self, scenario_id: str):
        """Open the guides page with one scenario expanded and scrolled into view."""
        self.win.navigate("guide")
        card = self.cards.get(scenario_id)
        if not card:
            return
        card.set_expanded(True)

        def scroll():
            ok, pt = card.compute_point(self.content, Graphene.Point())
            if ok:
                self.scroller.get_vadjustment().set_value(max(0, pt.y - 12))
            return False
        GLib.timeout_add(250, scroll)

    def do(self, key: str):
        win = self.win
        home = system.volume_for(system.HOME, win.volumes)
        if key == "shred":
            win.pages["shred"].add_files()
        elif key == "shred-folder":
            win.pages["shred"].add_folders()
        elif key == "wipe":
            win.navigate("wipe")
        elif key == "trim":
            vol = win.pages["wipe"].volume or home
            if vol and vol.is_ssd:
                win.request_trim(vol)
            else:
                win.toast("TRIM only applies to SSDs — select an SSD on the Wipe Free Space page")
                win.navigate("wipe")
        elif key in ("trash", "thumbnails", "recent"):
            win.sweep(key)
        elif key == "remember":
            win.pages["folders"].add()
        elif key == "disks":
            if shutil.which("gnome-disks"):
                subprocess.Popen(["gnome-disks"], start_new_session=True)
            else:
                win.toast("GNOME Disks isn't installed (sudo apt install gnome-disk-utility)")

    def refresh(self):
        pass
