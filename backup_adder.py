#!/usr/bin/env python3
"""Backup Adder — add a new backup to the LELLX 6 TB drive without duplicate files.

Pick a folder (or a single file), choose where it belongs, press Analyze: every file is
listed as COPY or SKIP with the reason, and for skipped files the location of the identical
copy already on the drive. Nothing is written until you press "Copy to drive".
"""
import os
import sys
import threading
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import lellx_core as core  # noqa: E402

APP_ID = "local.lellx.BackupAdder"


class Row(GObject.Object):
    __gtype_name__ = "BARow"

    def __init__(self, item):
        super().__init__()
        self.item = item


class FindRow(GObject.Object):
    __gtype_name__ = "BAFindRow"

    def __init__(self, path, size):
        super().__init__()
        self.path, self.size = path, size


def _label_factory(getter, xalign=0.0, ellipsize=True, css=None):
    f = Gtk.SignalListItemFactory()

    def setup(_f, li):
        lab = Gtk.Label(xalign=xalign)
        if ellipsize:
            lab.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        li.set_child(lab)

    def bind(_f, li):
        obj = li.get_item()
        lab = li.get_child()
        txt = getter(obj)
        lab.set_text(txt)
        lab.set_tooltip_text(txt)
        for c in ("success", "warning", "error"):
            lab.remove_css_class(c)
        if css:
            c = css(obj)
            if c:
                lab.add_css_class(c)

    f.connect("setup", setup)
    f.connect("bind", bind)
    return f


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Backup Adder")
        self.set_default_size(1200, 820)
        self.index = None
        self.plan = None
        self.source = None
        self.cancel = threading.Event()
        self.busy = False

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        sw = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(sw)
        menu = Gio.Menu()
        menu.append("Open drive in Files", "win.open-drive")
        menu.append("Rescan drive index (slow)", "win.rescan")
        menu.append("Safely eject drive", "win.eject")
        menu.append("About", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        tv.add_top_bar(header)

        self.toast = Adw.ToastOverlay()
        tv.set_content(self.toast)
        self.toast.set_child(self.stack)
        self.set_content(tv)

        for name, cb in (("open-drive", self.on_open_drive), ("rescan", self.on_rescan),
                         ("eject", self.on_eject), ("about", self.on_about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

        self.stack.add_titled_with_icon(self.build_add_page(), "add", "Add backup", "document-save-symbolic")
        self.stack.add_titled_with_icon(self.build_find_page(), "find", "Find on drive", "system-search-symbolic")
        self.stack.add_titled_with_icon(self.build_history_page(), "history", "History", "document-open-recent-symbolic")
        self.stack.connect("notify::visible-child-name", self.on_page)

        self.refresh_drive()

    # ------------------------------------------------------------------ UI building
    def build_add_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

        # drive status
        self.drive_group = Adw.PreferencesGroup(title="Backup drive")
        self.drive_row = Adw.ActionRow(title="Checking…", subtitle="")
        self.drive_icon = Gtk.Image(icon_name="drive-harddisk-symbolic", pixel_size=32)
        self.drive_row.add_prefix(self.drive_icon)
        self.btn_connect = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
        self.btn_connect.connect("clicked", lambda *_: self.run_bg(self.task_connect))
        self.btn_refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                                      tooltip_text="Refresh drive status")
        self.btn_refresh.connect("clicked", lambda *_: self.refresh_drive())
        self.drive_row.add_suffix(self.btn_connect)
        self.drive_row.add_suffix(self.btn_refresh)
        self.drive_group.add(self.drive_row)
        outer.append(self.drive_group)

        # source + destination
        g = Adw.PreferencesGroup(title="New backup")
        self.src_row = Adw.ActionRow(title="Source", subtitle="Choose the folder (or single file) to add")
        b1 = Gtk.Button(label="Folder…", valign=Gtk.Align.CENTER)
        b1.connect("clicked", lambda *_: self.pick_source(folder=True))
        b2 = Gtk.Button(label="File…", valign=Gtk.Align.CENTER)
        b2.connect("clicked", lambda *_: self.pick_source(folder=False))
        self.src_row.add_suffix(b1)
        self.src_row.add_suffix(b2)
        g.add(self.src_row)

        self.cat_row = Adw.ComboRow(title="Goes into")
        self.cat_row.set_model(Gtk.StringList.new([c[1] for c in core.CATEGORIES]))
        self.cat_row.set_selected(1)
        self.cat_row.connect("notify::selected", lambda *_: self.update_dest())
        g.add(self.cat_row)

        self.name_row = Adw.EntryRow(title="Name (device / owner), e.g. Samsung A30 or Dell Laptop")
        self.name_row.connect("changed", lambda *_: self.update_dest())
        g.add(self.name_row)

        self.date_row = Adw.EntryRow(title="Backup date (YYYY-MM-DD)")
        self.date_row.set_text(time.strftime("%Y-%m-%d"))
        self.date_row.connect("changed", lambda *_: self.update_dest())
        g.add(self.date_row)

        self.dest_row = Adw.ActionRow(title="Will be saved as", subtitle="—")
        self.dest_row.add_css_class("property")
        self.btn_analyze = Gtk.Button(label="Analyze", valign=Gtk.Align.CENTER)
        self.btn_analyze.add_css_class("suggested-action")
        self.btn_analyze.connect("clicked", lambda *_: self.on_analyze())
        self.dest_row.add_suffix(self.btn_analyze)
        g.add(self.dest_row)
        outer.append(g)

        # summary + filter bar
        bar = Gtk.Box(spacing=8)
        self.summary = Gtk.Label(xalign=0, hexpand=True, wrap=True)
        self.summary.set_markup("<i>Analyze a backup to see what will be copied and what is already on the drive.</i>")
        bar.append(self.summary)
        self.filter_dd = Gtk.DropDown.new_from_strings(["All", "Will copy", "Skipped", "Errors"])
        self.filter_dd.connect("notify::selected", lambda *_: self.refilter())
        bar.append(self.filter_dd)
        self.filter_entry = Gtk.SearchEntry(placeholder_text="Filter by name")
        self.filter_entry.connect("search-changed", lambda *_: self.refilter())
        bar.append(self.filter_entry)
        outer.append(bar)

        # results table
        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self.match_row)
        fmodel = Gtk.FilterListModel(model=self.store, filter=self.filter)
        sel = Gtk.SingleSelection(model=fmodel)
        cv = Gtk.ColumnView(model=sel, show_row_separators=True, reorderable=False)
        cv.connect("activate", self.on_row_activate)

        def col(title, factory, expand=False, width=None):
            c = Gtk.ColumnViewColumn(title=title, factory=factory, expand=expand, resizable=True)
            if width:
                c.set_fixed_width(width)
            cv.append_column(c)

        css = lambda o: {"COPY": "success", "SKIP": "warning", "ERROR": "error"}[o.item.action]  # noqa: E731
        col("Action", _label_factory(lambda o: {"COPY": "Copy", "SKIP": "Skip", "ERROR": "Error"}[o.item.action],
                                     ellipsize=False, css=css), width=70)
        col("File in new backup", _label_factory(lambda o: o.item.rel), expand=True)
        col("Size", _label_factory(lambda o: core.human(o.item.size), xalign=1.0, ellipsize=False), width=90)
        col("Reason", _label_factory(lambda o: o.item.reason), width=280)
        col("Already on drive at", _label_factory(lambda o: o.item.existing), expand=True)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=cv)
        scroll.add_css_class("card")
        outer.append(scroll)
        hint = Gtk.Label(xalign=0, label="Tip: double-click a skipped file to open the folder of its existing copy.")
        hint.add_css_class("dim-label")
        outer.append(hint)

        # progress + actions
        act = Gtk.Box(spacing=8)
        self.progress = Gtk.ProgressBar(hexpand=True, show_text=True, valign=Gtk.Align.CENTER)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE, hexpand=True)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        pbox.append(self.progress)
        pbox.append(self.status)
        act.append(pbox)
        self.del_check = Gtk.CheckButton(label="Delete originals after verified copy")
        act.append(self.del_check)
        self.btn_export = Gtk.Button(label="Export report…", sensitive=False)
        self.btn_export.connect("clicked", lambda *_: self.on_export())
        act.append(self.btn_export)
        self.btn_cancel = Gtk.Button(label="Cancel", sensitive=False)
        self.btn_cancel.connect("clicked", lambda *_: self.cancel.set())
        act.append(self.btn_cancel)
        self.btn_copy = Gtk.Button(label="Copy to drive", sensitive=False)
        self.btn_copy.add_css_class("suggested-action")
        self.btn_copy.connect("clicked", lambda *_: self.on_copy())
        act.append(self.btn_copy)
        outer.append(act)
        self.update_dest()
        return outer

    def build_find_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        lab = Gtk.Label(xalign=0, wrap=True,
                        label="Search the drive by file or folder name (uses the index, works even when the drive "
                              "is unplugged). Double-click a result to open its folder.")
        box.append(lab)
        self.find_entry = Gtk.SearchEntry(placeholder_text="e.g. IMG_0597 or Samsung A30")
        self.find_entry.connect("activate", lambda *_: self.on_find())
        self.find_entry.connect("search-changed", lambda *_: self.on_find(min_len=3))
        box.append(self.find_entry)
        self.find_store = Gio.ListStore(item_type=FindRow)
        sel = Gtk.SingleSelection(model=self.find_store)
        cv = Gtk.ColumnView(model=sel, show_row_separators=True)
        cv.connect("activate", self.on_find_activate)
        c = Gtk.ColumnViewColumn(title="Path on drive", factory=_label_factory(lambda o: o.path), expand=True)
        cv.append_column(c)
        c = Gtk.ColumnViewColumn(title="Size", factory=_label_factory(lambda o: core.human(o.size), 1.0, False))
        c.set_fixed_width(100)
        cv.append_column(c)
        sc = Gtk.ScrolledWindow(vexpand=True, child=cv)
        sc.add_css_class("card")
        box.append(sc)
        self.find_status = Gtk.Label(xalign=0)
        self.find_status.add_css_class("dim-label")
        box.append(self.find_status)
        return box

    def build_history_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.hist_view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        sc = Gtk.ScrolledWindow(vexpand=True, child=self.hist_view)
        sc.add_css_class("card")
        box.append(sc)
        return box

    # ------------------------------------------------------------------ helpers
    def toast_msg(self, text):
        self.toast.add_toast(Adw.Toast(title=text, timeout=5))

    def alert(self, heading, body):
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("ok", "OK")
        d.present(self)

    def set_busy(self, busy, cancellable=True):
        self.busy = busy
        for w in (self.btn_analyze, self.btn_connect, self.btn_refresh):
            w.set_sensitive(not busy)
        self.btn_copy.set_sensitive(not busy and self.plan is not None and not getattr(self.plan, "copied", False)
                                    and any(i.action == "COPY" for i in self.plan.items))
        self.btn_export.set_sensitive(not busy and self.plan is not None)
        self.btn_cancel.set_sensitive(busy and cancellable)
        if not busy:
            self.progress.set_fraction(0)
            self.progress.set_text("")

    def run_bg(self, fn, *args, cancellable=True):
        if self.busy:
            return
        self.cancel.clear()
        self.set_busy(True, cancellable)

        def work():
            try:
                res = fn(*args)
                GLib.idle_add(self._bg_done, None, res)
            except InterruptedError:
                GLib.idle_add(self._bg_done, "cancel", None)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._bg_done, e, None)

        threading.Thread(target=work, daemon=True).start()

    def _bg_done(self, err, res):
        self.set_busy(False)
        if err == "cancel":
            self.status.set_text("Cancelled.")
        elif err is not None:
            self.status.set_text("")
            self.alert("Something went wrong", str(err))
        self.refresh_drive()
        return False

    def ui(self, fn, *a):
        GLib.idle_add(lambda: (fn(*a), False)[1])

    def set_status(self, text):
        self.ui(self.status.set_text, text)

    def set_progress(self, done, total):
        def upd():
            frac = done / total if total else 0
            self.progress.set_fraction(min(frac, 1.0))
            self.progress.set_text(f"{frac * 100:.0f}%  ({core.human(done)} / {core.human(total)})")
        self.ui(upd)

    def ensure_index(self):
        if self.index is None:
            self.set_status("Loading index…")
            self.index = core.Index()
        return self.index

    # ------------------------------------------------------------------ drive
    def refresh_drive(self):
        def work():
            st = core.drive_status()
            GLib.idle_add(self.show_drive, st)
        threading.Thread(target=work, daemon=True).start()

    def show_drive(self, st):
        if not st["connected"]:
            self.drive_row.set_title("Not connected")
            self.drive_row.set_subtitle("Power the enclosure on, wait for the disk to spin, then plug in the USB cable.")
            self.drive_icon.set_from_icon_name("drive-harddisk-symbolic")
            self.btn_connect.set_sensitive(False)
            return False
        mode = {"ro": "read-only (safe)", "rw": "read-write", None: "not mounted"}[st["mounted"]]
        title = f"LELLX 6 TB — {mode}"
        sub = []
        if st["free"] is not None:
            sub.append(f"{core.human(st['free'])} free of {core.human(st['total'])}")
        if st["smart"]:
            sub.append(f"health: {st['smart']}")
        if st.get("Reallocated_Sector_Ct") or st.get("Current_Pending_Sector"):
            sub.append(f"⚠ bad sectors: {st.get('Reallocated_Sector_Ct', 0)} reallocated, "
                       f"{st.get('Current_Pending_Sector', 0)} pending")
        if st["errors_recent"]:
            sub.append("⚠ recent USB/power errors in the kernel log — check the enclosure power")
        self.drive_row.set_title(title)
        self.drive_row.set_subtitle(" · ".join(sub))
        self.btn_connect.set_label("Connected" if st["mounted"] else "Connect")
        self.btn_connect.set_sensitive(not self.busy and not st["mounted"])
        return False

    def task_connect(self):
        self.set_status("Connecting the drive (read-only)…")
        core.mount("ro")
        self.ensure_index()
        self.set_status("Drive connected (read-only).")

    def on_eject(self, *_):
        def work():
            self.set_status("Ejecting…")
            if self.index:
                self.index.commit()
            core.eject()
            self.set_status("Drive ejected. You can unplug it now.")
        self.run_bg(work, cancellable=False)

    def on_open_drive(self, *_):
        if core.mount_state():
            subprocess.Popen(["xdg-open", core.MOUNT])
        else:
            self.toast_msg("Connect the drive first.")

    def on_rescan(self, *_):
        d = Adw.AlertDialog(heading="Rescan the whole drive?",
                            body="This reads the folder list of the entire drive (about 20–40 minutes) and brings "
                                 "the index up to date with any change made outside Backup Adder. Nothing on the "
                                 "drive is changed except the saved index.")
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Rescan")
        d.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "go":
                return

            def work():
                core.mount("ro")
                idx = self.ensure_index()
                res = core.quick_rescan(idx, self.set_status, self.cancel)
                self.set_status("Saving index…")
                core.mount("rw")
                idx.save(to_drive=True)
                core.mount("ro")
                self.set_status(f"Rescan done: {res['files']:,} files, {res['added']:,} new, "
                                f"{res['changed']:,} changed, {res['removed']:,} removed from index.")
            self.run_bg(work)
        d.connect("response", resp)
        d.present(self)

    def on_about(self, *_):
        a = Adw.AboutDialog(application_name="Backup Adder", version="1.0",
                            comments="Adds backups to the LELLX 6 TB drive without duplicate files.\n"
                                     "Index: ~/backup-tools/state/index.db and /_INDEX on the drive.",
                            application_icon="drive-harddisk")
        a.present(self)

    # ------------------------------------------------------------------ add flow
    def pick_source(self, folder):
        dlg = Gtk.FileDialog(title="Choose the backup to add")

        def done(d, res):
            try:
                f = d.select_folder_finish(res) if folder else d.open_finish(res)
            except GLib.Error:
                return
            self.source = f.get_path()
            self.src_row.set_subtitle(self.source)
            if not self.name_row.get_text():
                self.name_row.set_text(os.path.basename(self.source.rstrip("/")))
            self.plan = None
            self.store.remove_all()
            self.set_busy(False)
        if folder:
            dlg.select_folder(self, None, done)
        else:
            dlg.open(self, None, done)

    def dest_rel(self):
        cat = core.CATEGORIES[self.cat_row.get_selected()][0]
        date = self.date_row.get_text().strip()
        if date and not (len(date) == 10 and date[4] == "-" and date[7] == "-" and date.replace("-", "").isdigit()):
            raise ValueError("Date must look like 2027-03-01 (or be empty for “no date”).")
        return core.make_dest(cat, self.name_row.get_text(), date)

    def update_dest(self):
        try:
            self.dest_row.set_subtitle(self.dest_rel())
        except ValueError as e:
            self.dest_row.set_subtitle(str(e))

    def on_analyze(self):
        if not self.source:
            self.alert("No source chosen", "Choose the folder or file you want to add first.")
            return
        try:
            dest = self.dest_rel()
        except ValueError as e:
            self.alert("Check the name and date", str(e))
            return
        self.store.remove_all()
        self.plan = None

        def work():
            core.mount("ro")
            idx = self.ensure_index()
            plan = core.analyze(self.source, dest, idx, self.set_status, self.set_progress, self.cancel)
            GLib.idle_add(self.show_plan, plan)
        self.run_bg(work)

    def show_plan(self, plan):
        self.plan = plan
        self.store.splice(0, 0, [Row(i) for i in plan.items])
        t = plan.totals()
        self.summary.set_markup(
            f"<b>Will copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])})</b>   ·   "
            f"Skip {t['SKIP'][0]:,} files ({core.human(t['SKIP'][1])}) already on the drive   ·   "
            f"{'<span foreground=\"red\">' if t['ERROR'][0] else ''}{t['ERROR'][0]} errors"
            f"{'</span>' if t['ERROR'][0] else ''}\n"
            f"Destination: <tt>{GLib.markup_escape_text(plan.dest_rel)}</tt>")
        self.set_busy(False)
        self.status.set_text("Review the list, then press “Copy to drive”. Nothing has been written yet.")
        return False

    def match_row(self, row):
        sel = self.filter_dd.get_selected() if hasattr(self, "filter_dd") else 0
        want = {0: None, 1: "COPY", 2: "SKIP", 3: "ERROR"}[sel]
        if want and row.item.action != want:
            return False
        q = self.filter_entry.get_text().lower() if hasattr(self, "filter_entry") else ""
        return not q or q in row.item.rel.lower() or q in row.item.existing.lower()

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def open_folder_of(self, rel_on_drive):
        if not core.mount_state():
            self.toast_msg("Connect the drive first.")
            return
        p = os.path.join(core.MOUNT, rel_on_drive)
        target = os.path.dirname(p) if not os.path.isdir(p) else p
        subprocess.Popen(["xdg-open", target])

    def on_row_activate(self, cv, pos):
        row = cv.get_model().get_item(pos)
        if row.item.existing:
            if row.item.existing.startswith(self.plan.dest_rel + "/"):
                self.toast_msg("That copy is inside this same backup.")
            else:
                self.open_folder_of(row.item.existing)

    def on_copy(self):
        t = self.plan.totals()
        delete = self.del_check.get_active()
        body = (f"Copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])}) to\n{self.plan.dest_rel}\n\n"
                f"Each file is read back and checked after copying. {t['SKIP'][0]:,} files already on the "
                f"drive are skipped and listed in _DUPLICATES_SKIPPED.txt inside the new folder.\n\n"
                "Do not unplug the drive or its power until it finishes.")
        if delete:
            body += ("\n\n⚠ The original files on this computer will be DELETED after everything is verified "
                     "(only if there are no errors). This cannot be undone.")
        d = Adw.AlertDialog(heading="Copy to the backup drive?", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Copy and delete originals" if delete else "Copy")
        d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if delete else Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "go":
                return

            def work():
                self.set_status("Switching the drive to read-write…")
                core.mount("rw")
                try:
                    res = core.execute(self.plan, self.ensure_index(), delete, self.set_status,
                                       self.set_progress, self.cancel)
                finally:
                    self.set_status("Switching the drive back to read-only…")
                    try:
                        core.mount("ro")
                    except core.DriveError:
                        pass
                GLib.idle_add(self.copy_done, res)
            self.run_bg(work)
        d.connect("response", resp)
        d.present(self)

    def copy_done(self, res):
        t = self.plan.totals()
        if res["cancelled"]:
            msg = "Copy cancelled. Files copied so far are verified and listed in _MANIFEST.sha256."
        elif res["failures"]:
            msg = f"{len(res['failures'])} files failed verification and were marked as errors. Check the drive connection."
        else:
            msg = (f"Done: {t['COPY'][0]:,} files copied and verified, {t['SKIP'][0]:,} duplicates skipped."
                   + (f" {res['deleted']:,} original files deleted." if res["deleted"] else ""))
        self.store.remove_all()
        self.store.splice(0, 0, [Row(i) for i in self.plan.items])
        self.status.set_text(msg)
        self.alert("Copy finished" if not res["failures"] else "Copy finished with problems", msg)
        self.plan.copied = True
        self.btn_copy.set_sensitive(False)
        return False

    def on_export(self):
        dlg = Gtk.FileDialog(title="Save report", initial_name=os.path.basename(self.plan.dest_rel) + " - report.tsv")

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            core.export_report(self.plan, f.get_path())
            self.toast_msg("Report saved.")
        dlg.save(self, None, done)

    # ------------------------------------------------------------------ find / history
    def on_find(self, min_len=1):
        q = self.find_entry.get_text().strip()
        if len(q) < min_len:
            return
        try:
            idx = self.ensure_index()
        except core.DriveError as e:
            self.find_status.set_text(str(e))
            return
        rows = idx.search(q)
        self.find_store.remove_all()
        self.find_store.splice(0, 0, [FindRow(p, s) for p, s in rows])
        self.find_status.set_text(f"{len(rows)} result(s)" + (" (first 500 shown)" if len(rows) == 500 else ""))

    def on_find_activate(self, cv, pos):
        self.open_folder_of(cv.get_model().get_item(pos).path)

    def on_page(self, *_):
        if self.stack.get_visible_child_name() == "history":
            try:
                txt = open(core.HISTORY).read()
            except OSError:
                txt = "No history yet."
            self.hist_view.get_buffer().set_text(txt)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()

    def do_shutdown(self):
        for w in self.get_windows():
            if getattr(w, "index", None):
                try:
                    w.index.commit()
                except Exception:  # noqa: BLE001
                    pass
        Adw.Application.do_shutdown(self)


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
