"""Shared widgets and helpers for the Shreder UI."""
from __future__ import annotations

import os
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk, Pango  # noqa: E402

from .. import engine, system  # noqa: E402

METHOD_IDS = list(engine.METHODS)


def run_async(fn, callback):
    """Run fn() on a worker thread and deliver its result (or exception) on the main loop."""
    def worker():
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001
            result = e

        def deliver():
            callback(result)
            return False
        GLib.idle_add(deliver)
    threading.Thread(target=worker, daemon=True).start()


def time_ago(ts) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)} min ago"
    if delta < 86400:
        h = int(delta // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    d = int(delta // 86400)
    if d < 14:
        return f"{d} day{'s' if d != 1 else ''} ago"
    return time.strftime("%b %d, %Y", time.localtime(ts))


def format_when(ts) -> str:
    lt = time.localtime(ts)
    if time.strftime("%Y%m%d", lt) == time.strftime("%Y%m%d"):
        return "Today " + time.strftime("%H:%M", lt)
    return time.strftime("%b %d, %H:%M", lt)


# --------------------------------------------------------------------------- layout

def scrolled_page(child: Gtk.Widget, max_width: int = 1000) -> Gtk.ScrolledWindow:
    clamp = Adw.Clamp(maximum_size=max_width, tightening_threshold=max_width - 200)
    clamp.set_child(child)
    sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
    sw.set_child(clamp)
    return sw


def page_box() -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24,
                   margin_top=24, margin_bottom=32, margin_start=12, margin_end=12)


def section_header(title: str, subtitle: str | None = None, suffix: Gtk.Widget | None = None) -> Gtk.Box:
    box = Gtk.Box(spacing=12, margin_bottom=2)
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
    t = Gtk.Label(label=title, xalign=0, css_classes=["section-title"])
    text.append(t)
    if subtitle is not None:
        s = Gtk.Label(label=subtitle, xalign=0, wrap=True, css_classes=["section-sub"])
        text.append(s)
    box.append(text)
    if suffix:
        suffix.set_valign(Gtk.Align.CENTER)
        box.append(suffix)
    return box


def badge(text: str, kind: str = "") -> Gtk.Label:
    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER, css_classes=["badge"] + ([kind] if kind else []))
    return lbl


def icon_button(icon: str, tooltip: str, callback, *classes) -> Gtk.Button:
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER,
                   css_classes=["flat"] + list(classes))
    b.connect("clicked", lambda *_: callback())
    return b


def button_row(*buttons, center: bool = False) -> Adw.WrapBox:
    """A row of buttons that wraps onto several lines on narrow windows."""
    box = Adw.WrapBox(child_spacing=10, line_spacing=10, valign=Gtk.Align.CENTER,
                      halign=Gtk.Align.CENTER if center else Gtk.Align.END)
    for b in buttons:
        box.append(b)
    return box


def menu_button(items, icon: str = "view-more-symbolic", tooltip: str = "More") -> Gtk.MenuButton:
    """A small popover menu; items are (label, callback) or None for a separator."""
    pop = Gtk.Popover(has_arrow=True)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4,
                  margin_start=4, margin_end=4)
    for item in items:
        if item is None:
            box.append(Gtk.Separator(margin_top=4, margin_bottom=4))
            continue
        label, cb, *extra = item
        b = Gtk.Button(css_classes=["flat"] + list(extra))
        b.set_child(Gtk.Label(label=label, xalign=0))

        def clicked(_b, cb=cb):
            pop.popdown()
            cb()
        b.connect("clicked", clicked)
        box.append(b)
    pop.set_child(box)
    return Gtk.MenuButton(icon_name=icon, tooltip_text=tooltip, popover=pop, valign=Gtk.Align.CENTER,
                          css_classes=["flat"])


def pill_button(label: str, callback, *classes, icon: str | None = None) -> Gtk.Button:
    b = Gtk.Button(css_classes=["pill"] + list(classes), valign=Gtk.Align.CENTER)
    if icon:
        b.set_child(Adw.ButtonContent(icon_name=icon, label=label))
    else:
        b.set_label(label)
    b.connect("clicked", lambda *_: callback())
    return b


class StatCard(Gtk.Box):
    def __init__(self, icon: str, label: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                         css_classes=["card", "stat-card"], hexpand=True)
        head = Gtk.Box(spacing=8)
        head.append(Gtk.Image(icon_name=icon, css_classes=["stat-icon"]))
        head.append(Gtk.Label(label=label, xalign=0, css_classes=["stat-label"],
                              ellipsize=Pango.EllipsizeMode.END))
        self.append(head)
        self.value = Gtk.Label(label="0", xalign=0, css_classes=["stat-value"])
        self.append(self.value)

    def set_value(self, text: str):
        self.value.set_label(text)


class ActionTile(Gtk.Button):
    def __init__(self, icon: str, title: str, subtitle: str, callback, danger: bool = False):
        super().__init__(css_classes=["card", "action-tile"] + (["danger"] if danger else []),
                         hexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.START)
        box.append(Gtk.Image(icon_name=icon, pixel_size=22, halign=Gtk.Align.START,
                             css_classes=["tile-icon"], margin_bottom=8))
        box.append(Gtk.Label(label=title, xalign=0, css_classes=["tile-title"], wrap=True,
                             max_width_chars=16))
        self.sub = Gtk.Label(label=subtitle, xalign=0, yalign=0, css_classes=["tile-sub"], wrap=True,
                             wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2, max_width_chars=20,
                             ellipsize=Pango.EllipsizeMode.END)
        box.append(self.sub)
        self.set_child(box)
        self.connect("clicked", lambda *_: callback())

    def set_subtitle(self, text: str):
        self.sub.set_label(text)


def tile_grid(tiles, min_per_line=3, max_per_line=3) -> Gtk.FlowBox:
    fb = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                     min_children_per_line=min_per_line, max_children_per_line=max_per_line,
                     column_spacing=12, row_spacing=12)
    for t in tiles:
        fb.append(t)
    for child in list(_children(fb)):
        child.set_focusable(False)
    return fb


def _children(widget):
    c = widget.get_first_child()
    while c is not None:
        yield c
        c = c.get_next_sibling()


def clear_box(box):
    """Remove every child of a Gtk.Box / Gtk.ListBox / Gtk.FlowBox."""
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


# --------------------------------------------------------------------------- method chooser

def method_row(selected_id: str | None = None, title: str = "Overwrite method") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title)
    row.set_model(Gtk.StringList.new([m.name for m in engine.METHODS.values()]))
    row.set_subtitle_lines(2)

    def sync(*_):
        m = engine.METHODS[METHOD_IDS[row.get_selected()]]
        row.set_subtitle(f"{m.short} — {m.summary}")
    row.connect("notify::selected", sync)
    set_method(row, selected_id or engine.DEFAULT_METHOD)
    sync()
    return row


def get_method_id(row: Adw.ComboRow) -> str:
    idx = row.get_selected()
    return METHOD_IDS[idx] if 0 <= idx < len(METHOD_IDS) else engine.DEFAULT_METHOD


def set_method(row: Adw.ComboRow, method_id: str):
    row.set_selected(METHOD_IDS.index(method_id) if method_id in METHOD_IDS else
                     METHOD_IDS.index(engine.DEFAULT_METHOD))


def string_combo(title: str, options: list[tuple[str, str]], selected: str, subtitle: str = "") -> Adw.ComboRow:
    row = Adw.ComboRow(title=title, subtitle=subtitle)
    row.set_model(Gtk.StringList.new([label for _, label in options]))
    keys = [k for k, _ in options]
    row.set_selected(keys.index(selected) if selected in keys else 0)
    row._keys = keys
    return row


def combo_key(row: Adw.ComboRow) -> str:
    return row._keys[row.get_selected()]


# --------------------------------------------------------------------------- dialogs

def confirm(parent, heading: str, body: str, action: str, on_confirm, destructive: bool = True,
            extra: Gtk.Widget | None = None):
    dlg = Adw.AlertDialog(heading=heading, body=body)
    dlg.set_body_use_markup(True)
    dlg.add_response("cancel", "Cancel")
    dlg.add_response("go", action)
    dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                                else Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("cancel")
    dlg.set_close_response("cancel")
    if extra:
        dlg.set_extra_child(extra)

    def on_response(_d, response):
        if response == "go":
            on_confirm()
    dlg.connect("response", on_response)
    dlg.present(parent)
    return dlg


def method_breakdown(method: engine.Method) -> Gtk.Widget:
    """A compact pass-by-pass description used inside confirmation dialogs."""
    passes = method.describe_passes()
    if len(passes) > 7:
        text = f"{len(passes)} passes: random ×4, 27 fixed bit patterns, random ×4"
    else:
        text = "  →  ".join(f"{i}. {p}" for i, p in enumerate(passes, 1))
    return Gtk.Label(label=text, wrap=True, justify=Gtk.Justification.CENTER,
                     css_classes=["method-pass", "dim-label"])


def pick_files(parent, callback, folders: bool = False, title: str | None = None):
    dialog = Gtk.FileDialog(title=title or ("Choose Folders" if folders else "Choose Files"), modal=True)

    def done(dlg, result):
        try:
            model = dlg.select_multiple_folders_finish(result) if folders else dlg.open_multiple_finish(result)
        except GLib.Error:
            return
        paths = []
        for i in range(model.get_n_items()):
            f = model.get_item(i)
            if f.get_path():
                paths.append(f.get_path())
        if paths:
            callback(paths)
    if folders:
        dialog.select_multiple_folders(parent, None, done)
    else:
        dialog.open_multiple(parent, None, done)


def open_folder(parent, path: str):
    target = path if os.path.isdir(path) else os.path.dirname(path)
    Gtk.FileLauncher.new(Gio.File.new_for_path(target)).launch(parent, None, None, None)
