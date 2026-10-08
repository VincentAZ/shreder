"""Preferences dialog."""
from __future__ import annotations

from gi.repository import Adw, Gtk

from .. import system
from . import common


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences", search_enabled=False)
        self.win = win
        store = win.store
        s = store.settings

        page = Adw.PreferencesPage(title="General", icon_name="preferences-system-symbolic")

        g = Adw.PreferencesGroup(title="Defaults",
                                 description="Used for new shreds, remembered folders set to “default”, "
                                             "and scheduled runs.")
        self.method = common.method_row(s.get("method"), "Default method")
        self.method.connect("notify::selected",
                            lambda r, _p: store.set_setting("method", common.get_method_id(r)))
        g.add(self.method)
        verify = Adw.SwitchRow(title="Verify final pass",
                               subtitle="Read data back from disk after overwriting. Slower, but proves it worked.",
                               active=s.get("verify", True))
        verify.connect("notify::active", lambda r, _p: store.set_setting("verify", r.get_active()))
        g.add(verify)
        rename = Adw.SpinRow.new_with_range(0, 12, 1)
        rename.set_title("Name scrambling passes")
        rename.set_subtitle("Rename each item to random names before deleting so the original name "
                            "doesn't linger in the folder index")
        rename.set_value(s.get("rename_passes", 3))
        rename.connect("notify::value", lambda r, _p: store.set_setting("rename_passes", int(r.get_value())))
        g.add(rename)
        page.add(g)

        g = Adw.PreferencesGroup(title="Notifications")
        notify = Adw.SwitchRow(title="Notify when a task finishes", active=s.get("notify", True))
        notify.connect("notify::active", lambda r, _p: store.set_setting("notify", r.get_active()))
        g.add(notify)
        page.add(g)

        g = Adw.PreferencesGroup(title="Integration")
        fm = Adw.SwitchRow(title="“Shred with Shreder” in Files",
                           subtitle="Adds an entry to the Files app's right-click → Scripts menu",
                           active=system.file_manager_action_installed())

        def toggle_fm(row, _p):
            err = system.install_file_manager_action(row.get_active())
            if err:
                win.toast(err)
        fm.connect("notify::active", toggle_fm)
        g.add(fm)
        self.sched = Adw.ActionRow(title="Background scheduler", subtitle="Checking…")
        g.add(self.sched)
        common.run_async(system.scheduler_active, self._sched_status)
        page.add(g)

        g = Adw.PreferencesGroup(title="Data")
        reset = Adw.ButtonRow(title="Reset Statistics")
        reset.add_css_class("destructive-action")
        reset.connect("activated", lambda *_: common.confirm(
            self, "Reset statistics?", "Counters on the Command Center go back to zero.", "Reset",
            store.reset_stats))
        g.add(reset)
        clear = Adw.ButtonRow(title="Clear Activity History")
        clear.add_css_class("destructive-action")
        clear.connect("activated", lambda *_: common.confirm(
            self, "Clear activity history?", "The list of past operations is removed.", "Clear",
            store.clear_history))
        g.add(clear)
        page.add(g)

        self.add(page)

    def _sched_status(self, active):
        n = sum(1 for f in self.win.store.folders if f.get("schedule", "off") != "off")
        if active is True:
            self.sched.set_subtitle(f"Active — checks hourly · {n} scheduled folder{'s' if n != 1 else ''}")
        else:
            self.sched.set_subtitle("Off — turns on automatically when you schedule a folder")
