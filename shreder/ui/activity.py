"""Activity page: queued jobs and the operation history."""
from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import engine, system
from . import common

STATE_STYLE = {
    "done": ("object-select-symbolic", "success", "Completed"),
    "warning": ("dialog-warning-symbolic", "warning", "Completed with problems"),
    "failed": ("dialog-error-symbolic", "error", "Failed"),
    "cancelled": ("process-stop-symbolic", "dim-label", "Cancelled"),
}


def history_row(entry: dict) -> Gtk.Widget:
    icon, css, label = STATE_STYLE.get(entry.get("state"), STATE_STYLE["done"])
    method = engine.METHODS.get(entry.get("method"))
    bits = [common.format_when(entry.get("time", 0))]
    if entry.get("kind") != "trim" and method:
        bits.append(f"{method.name} ({method.short})")
    bits.append(system.human_duration(entry.get("duration")))
    summary = entry.get("summary") or ""
    if entry.get("state") == "failed" and entry.get("error"):
        summary = entry["error"]
    elif entry.get("state") == "cancelled":
        summary = "Cancelled — " + summary
    subtitle = GLib.markup_escape_text(f"{summary}\n{' · '.join(bits)}")
    stats = entry.get("stats", {})
    problems = (stats.get("errors") or []) + (stats.get("warnings") or [])
    title = GLib.markup_escape_text(entry.get("title", ""))
    if problems:
        row = Adw.ExpanderRow(title=title, subtitle=subtitle, subtitle_lines=2)
        for msg in problems[:60]:
            r = Adw.ActionRow(title=GLib.markup_escape_text(msg), title_lines=3, css_classes=["mono"])
            r.add_css_class("caption")
            row.add_row(r)
    else:
        row = Adw.ActionRow(title=title, subtitle=subtitle, subtitle_lines=2)
    img = Gtk.Image(icon_name=icon, css_classes=[css], tooltip_text=label)
    row.add_prefix(img)
    written = stats.get("bytes_written", 0)
    if written:
        row.add_suffix(Gtk.Label(label=f"{system.human_size(written)} written",
                                 css_classes=["dim-label", "caption", "numeric"]))
    return row


class ActivityPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        content = common.page_box()

        self.queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.queue_box.append(common.section_header("Up next", "Tasks waiting for the current one to finish"))
        self.queue_list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        self.queue_box.append(self.queue_list)
        content.append(self.queue_box)

        self.clear_btn = common.pill_button("Clear History", self.clear, "flat")
        content.append(common.section_header(
            "History", "Every shred and wipe, with any files that could not be processed.", self.clear_btn))
        self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        empty = Adw.ActionRow(title="No activity yet", subtitle="Finished tasks will show up here")
        self.list.set_placeholder(empty)
        content.append(self.list)
        self.append(common.scrolled_page(content, 920))

    def refresh(self):
        common.clear_box(self.list)
        entries = self.win.store.history(200)
        for e in entries:
            self.list.append(history_row(e))
        self.clear_btn.set_visible(bool(entries))
        self.on_jobs_changed()

    def on_jobs_changed(self):
        common.clear_box(self.queue_list)
        pending = self.win.jobs.pending
        self.queue_box.set_visible(bool(pending))
        for job in pending:
            row = Adw.ActionRow(title=GLib.markup_escape_text(job.title),
                                subtitle=f"{job.method.name} · queued")
            row.add_prefix(Gtk.Image(icon_name=job.icon))
            row.add_suffix(common.icon_button("window-close-symbolic", "Remove from queue",
                                              lambda j=job: self.win.jobs.cancel(j)))
            self.queue_list.append(row)

    def clear(self):
        common.confirm(self.win, "Clear activity history?",
                       "The list of past operations is removed. Statistics on the Command Center are kept.",
                       "Clear", self.win.store.clear_history)
