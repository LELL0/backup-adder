#!/usr/bin/env python3
"""Backup Adder (Linux, GTK4 / libadwaita) — add a backup to any drive without duplicate files.

Choose a drive, choose the backup to add, press Analyze: every file is listed as Copy or Skip
with the reason; skipped files show where the identical copy already is on the drive. Nothing
is written until you press "Copy to drive". A drive's index is built automatically the first
time (slower), then reused and kept up to date.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import backup_core as core  # noqa: E402

APP_ID = "local.lellx.BackupAdder"
OTHER = "Other folder…"


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
        obj, lab = li.get_item(), li.get_child()
        txt = getter(obj)
        lab.set_text(txt)
        lab.set_tooltip_text(txt)
        for c in ("success", "warning", "error"):
            lab.remove_css_class(c)
        if css and css(obj):
            lab.add_css_class(css(obj))

    f.connect("setup", setup)
    f.connect("bind", bind)
    return f


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Backup Adder")
        self.set_default_size(1250, 860)
        self.drives, self.drive, self.index, self.plan = [], None, None, None
        self.cancel = threading.Event()
        self.busy = False
        self._selecting = False

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        menu = Gio.Menu()
        for label, act in (("Open drive in Files", "win.open-drive"), ("Rescan drive index", "win.rescan"),
                           ("Safely eject drive", "win.eject"), ("About", "win.about")):
            menu.append(label, act)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        tv.add_top_bar(header)
        self.toast = Adw.ToastOverlay(child=self.stack)
        tv.set_content(self.toast)
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
        self.refresh_drives()

    # ------------------------------------------------------------------ UI
    def build_add_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        g = Adw.PreferencesGroup(title="Backup drive")
        self.drive_combo = Adw.ComboRow(title="Drive")
        self.drive_combo.set_model(Gtk.StringList.new([]))
        self.drive_combo.connect("notify::selected", lambda *_: self.on_drive_selected())
        btn = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Look for drives again")
        btn.connect("clicked", lambda *_: self.refresh_drives())
        self.drive_combo.add_suffix(btn)
        self.drive_combo.add_prefix(Gtk.Image(icon_name="drive-harddisk-symbolic", pixel_size=24))
        g.add(self.drive_combo)
        self.info_row = Adw.ActionRow(title="Status", subtitle="—")
        self.info_row.set_subtitle_lines(3)
        g.add(self.info_row)
        outer.append(g)

        g = Adw.PreferencesGroup(title="New backup")
        self.src_row = Adw.ActionRow(title="Source", subtitle="Choose the folder (or single file) to add")
        for label, folder in (("Folder…", True), ("File…", False)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda _b, f=folder: self.pick_source(f))
            self.src_row.add_suffix(b)
        g.add(self.src_row)
        self.parent_row = Adw.EntryRow(title="Goes into folder (on the drive)")
        self.parent_row.connect("changed", lambda *_: self.update_dest())
        self.parent_menu = Gtk.MenuButton(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                          tooltip_text="Existing folders on the drive")
        self.parent_pop = Gtk.Popover()
        self.parent_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.parent_list.connect("row-activated", self.on_parent_pick)
        self.parent_pop.set_child(Gtk.ScrolledWindow(child=self.parent_list, min_content_height=300,
                                                     min_content_width=380))
        self.parent_menu.set_popover(self.parent_pop)
        self.parent_row.add_suffix(self.parent_menu)
        g.add(self.parent_row)
        self.name_row = Adw.EntryRow(title="Name (device / owner), e.g. Samsung A30 or Dell Laptop")
        self.name_row.connect("changed", lambda *_: self.update_dest())
        g.add(self.name_row)
        self.date_row = Adw.EntryRow(title="Backup date (YYYY-MM-DD, empty = no date)")
        self.date_row.set_text(time.strftime("%Y-%m-%d"))
        self.date_row.connect("changed", lambda *_: self.update_dest())
        g.add(self.date_row)
        self.rescan_row = Adw.SwitchRow(title="Rescan the drive first",
                                        subtitle="Only needed if files were added or removed outside this app")
        g.add(self.rescan_row)
        self.dest_row = Adw.ActionRow(title="Will be saved as", subtitle="—")
        self.dest_row.add_css_class("property")
        self.btn_analyze = Gtk.Button(label="Analyze", valign=Gtk.Align.CENTER)
        self.btn_analyze.add_css_class("suggested-action")
        self.btn_analyze.connect("clicked", lambda *_: self.on_analyze())
        self.dest_row.add_suffix(self.btn_analyze)
        g.add(self.dest_row)
        outer.append(g)

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

        self.store = Gio.ListStore(item_type=Row)
        self.filter = Gtk.CustomFilter.new(self.match_row)
        cv = Gtk.ColumnView(model=Gtk.SingleSelection(model=Gtk.FilterListModel(model=self.store, filter=self.filter)),
                            show_row_separators=True)
        cv.connect("activate", self.on_row_activate)
        css = lambda o: {"COPY": "success", "SKIP": "warning", "ERROR": "error"}[o.item.action]  # noqa: E731
        for title, fac, expand, width in (
                ("Action", _label_factory(lambda o: {"COPY": "Copy", "SKIP": "Skip", "ERROR": "Error"}[o.item.action],
                                          ellipsize=False, css=css), False, 70),
                ("File in new backup", _label_factory(lambda o: o.item.rel), True, None),
                ("Size", _label_factory(lambda o: core.human(o.item.size), 1.0, False), False, 90),
                ("Reason", _label_factory(lambda o: o.item.reason), False, 300),
                ("Already on drive at", _label_factory(lambda o: o.item.existing), True, None)):
            c = Gtk.ColumnViewColumn(title=title, factory=fac, expand=expand, resizable=True)
            if width:
                c.set_fixed_width(width)
            cv.append_column(c)
        sc = Gtk.ScrolledWindow(vexpand=True, child=cv)
        sc.add_css_class("card")
        outer.append(sc)
        hint = Gtk.Label(xalign=0, label="Tip: double-click a skipped file to open the folder of its existing copy.")
        hint.add_css_class("dim-label")
        outer.append(hint)

        act = Gtk.Box(spacing=8)
        pbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
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
        return outer

    def build_find_page(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        box.append(Gtk.Label(xalign=0, wrap=True, label="Search the selected drive by file or folder name (uses its "
                             "index). Double-click a result to open its folder."))
        self.find_entry = Gtk.SearchEntry(placeholder_text="e.g. IMG_0597 or Samsung A30")
        self.find_entry.connect("activate", lambda *_: self.on_find())
        self.find_entry.connect("search-changed", lambda *_: self.on_find(min_len=3))
        box.append(self.find_entry)
        self.find_store = Gio.ListStore(item_type=FindRow)
        cv = Gtk.ColumnView(model=Gtk.SingleSelection(model=self.find_store), show_row_separators=True)
        cv.connect("activate", self.on_find_activate)
        cv.append_column(Gtk.ColumnViewColumn(title="Path on drive", factory=_label_factory(lambda o: o.path), expand=True))
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
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
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

    def ui(self, fn, *a):
        GLib.idle_add(lambda: (fn(*a), False)[1])

    def set_status(self, text):
        self.ui(self.status.set_text, text)

    def set_progress(self, done, total):
        def upd():
            frac = min(done / total, 1.0) if total else 0
            self.progress.set_fraction(frac)
            self.progress.set_text(f"{frac * 100:.0f}%  ({core.human(done)} / {core.human(total)})")
        self.ui(upd)

    def set_busy(self, busy):
        self.busy = busy
        self.btn_analyze.set_sensitive(not busy)
        self.drive_combo.set_sensitive(not busy)
        self.btn_cancel.set_sensitive(busy)
        self.btn_copy.set_sensitive(not busy and self.plan is not None and not self.plan.copied
                                    and any(i.action == "COPY" for i in self.plan.items))
        self.btn_export.set_sensitive(not busy and self.plan is not None)
        if not busy:
            self.progress.set_fraction(0)
            self.progress.set_text("")

    def run_bg(self, fn):
        if self.busy:
            return
        self.cancel.clear()
        self.set_busy(True)

        def work():
            err = None
            try:
                fn()
            except InterruptedError:
                err = "cancel"
            except Exception as e:  # noqa: BLE001
                err = e
            GLib.idle_add(self._bg_done, err)
        threading.Thread(target=work, daemon=True).start()

    def _bg_done(self, err):
        self.set_busy(False)
        if err == "cancel":
            self.status.set_text("Cancelled.")
        elif err is not None:
            self.status.set_text("")
            self.alert("Something went wrong", str(err))
        self.show_drive_info()
        return False

    # ------------------------------------------------------------------ drives
    def refresh_drives(self):
        try:
            self.drives = core.list_drives()
        except Exception as e:  # noqa: BLE001
            self.drives = []
            self.toast_msg(f"Could not list drives: {e}")
        keep = self.drive.id if self.drive else None
        self._selecting = True
        self.drive_combo.set_model(Gtk.StringList.new([d.title for d in self.drives] + [OTHER]))
        sel = next((i for i, d in enumerate(self.drives) if d.id == keep), 0 if self.drives else None)
        self._selecting = False
        if sel is None:
            self.info_row.set_subtitle("No external drive found. Connect one (power first, then USB) and press ⟳, "
                                       "or choose “Other folder…”.")
            self.drive_combo.set_selected(Gtk.INVALID_LIST_POSITION)
        else:
            self.drive_combo.set_selected(sel)
            self.on_drive_selected()

    def on_drive_selected(self):
        if self._selecting:
            return
        i = self.drive_combo.get_selected()
        if i == Gtk.INVALID_LIST_POSITION:
            return
        if i == len(self.drives):
            dlg = Gtk.FileDialog(title="Choose the destination folder")

            def done(d, res):
                try:
                    f = d.select_folder_finish(res)
                except GLib.Error:
                    return
                self.drives.append(core.folder_drive(f.get_path()))
                self._selecting = True
                self.drive_combo.set_model(Gtk.StringList.new([x.title for x in self.drives] + [OTHER]))
                self._selecting = False
                self.drive_combo.set_selected(len(self.drives) - 1)
            dlg.select_folder(self, None, done)
            return
        self.drive, self.index, self.plan = self.drives[i], None, None
        self.store.remove_all()
        drive = self.drive

        def work():
            self.set_status("Connecting the drive…")
            core.prepare(drive, write=False)
            idx = core.Index(drive)
            parents = core.suggest_parents(drive.root)
            GLib.idle_add(self.drive_ready, drive, idx, parents)
        self.run_bg(work)

    def drive_ready(self, drive, idx, parents):
        if drive is not self.drive:
            return False
        self.index = idx
        self.parent_list.remove_all()
        for p in parents:
            r = Gtk.ListBoxRow(child=Gtk.Label(label=p, xalign=0, margin_start=6, margin_end=6, margin_top=3,
                                               margin_bottom=3))
            r.path = p
            self.parent_list.append(r)
        if not self.parent_row.get_text() and "BACKUPS" in parents:
            self.parent_row.set_text("BACKUPS")
        self.drive_combo.set_model(Gtk.StringList.new([d.title for d in self.drives] + [OTHER]))
        self._selecting = True
        self.drive_combo.set_selected(self.drives.index(drive))
        self._selecting = False
        self.status.set_text("Ready.")
        self.update_dest()
        self.show_drive_info()
        return False

    def on_parent_pick(self, _lb, row):
        self.parent_row.set_text(row.path)
        self.parent_pop.popdown()

    def show_drive_info(self):
        drive, idx = self.drive, self.index
        if not drive:
            return

        def work():
            parts = [drive.root or "not mounted"]
            h = core.drive_health(drive) if drive.root else {}
            if h.get("free") is not None:
                parts.append(f"{core.human(h['free'])} free of {core.human(h['total'])}")
            if h.get("smart"):
                parts.append(f"health {h['smart']}")
            if drive.kind == "lellx":
                parts.append("read-only except while copying")
            if idx:
                n, _size, hashed = idx.stats()
                built = idx.info("built")
                parts.append(f"index: {n:,} files ({hashed:,} hashed), updated {built or '—'}" if n or built
                             else "no index yet: built automatically on the first Analyze (can take a while)")
            parts += ["⚠ " + w for w in h.get("warnings", [])]
            GLib.idle_add(lambda: (self.info_row.set_subtitle("  ·  ".join(parts)), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def on_open_drive(self, *_):
        if self.drive and self.drive.root:
            core.open_in_file_manager(self.drive.root)
        else:
            self.toast_msg("Choose a connected drive first.")

    def on_eject(self, *_):
        drive = self.drive
        if not drive:
            return

        def work():
            if self.index:
                self.index.commit()
            core.eject(drive)
            self.index = None
            self.set_status("Drive ejected. You can unplug it now.")
        self.run_bg(work)

    def on_rescan(self, *_):
        if not self.index:
            self.toast_msg("Choose a drive first.")
            return
        d = Adw.AlertDialog(heading="Rescan the drive?",
                            body="Reads the folder tree of the whole drive and updates its index with changes made "
                                 "outside Backup Adder. Can take a while on a big drive. Files are not modified.")
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Rescan")
        d.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)

        def resp(_d, r):
            if r != "go":
                return
            drive, idx = self.drive, self.index

            def work():
                res = core.scan(idx, self.set_status, self.cancel)
                self.publish_index(drive, idx)
                self.set_status(f"Index updated: {res['files']:,} files, {res['added']:,} new, "
                                f"{res['changed']:,} changed, {res['removed']:,} removed.")
            self.run_bg(work)
        d.connect("response", resp)
        d.present(self)

    def publish_index(self, drive, idx):
        try:
            core.prepare(drive, write=True)
            idx.save()
        except core.DriveError:
            idx.save(to_drive=False)
        finally:
            core.finish(drive)

    def on_about(self, *_):
        Adw.AboutDialog(application_name="Backup Adder", version="2.0", application_icon="drive-harddisk",
                        comments="Adds backups to a drive without duplicate files.\nIndex: .backup-adder/ on each drive "
                                 "and ~/backup-tools/state/drives/.").present(self)

    # ------------------------------------------------------------------ add flow
    def pick_source(self, folder):
        dlg = Gtk.FileDialog(title="Choose the backup to add")

        def done(d, res):
            try:
                f = d.select_folder_finish(res) if folder else d.open_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            self.src_row.set_subtitle(path)
            self.source = path
            if not self.name_row.get_text():
                self.name_row.set_text(os.path.basename(path.rstrip("/")))
            self.plan = None
            self.store.remove_all()
            self.set_busy(False)
        (dlg.select_folder if folder else dlg.open)(self, None, done)

    def dest_rel(self):
        return core.make_dest(self.parent_row.get_text(), self.name_row.get_text(), self.date_row.get_text().strip())

    def update_dest(self):
        try:
            d = self.dest_rel()
            self.dest_row.set_subtitle(os.path.join(self.drive.root, *d.split("/")) if self.drive and self.drive.root else d)
        except ValueError as e:
            self.dest_row.set_subtitle(str(e))

    def on_analyze(self):
        if not self.index:
            self.alert("No drive", "Choose a drive first.")
            return
        src = getattr(self, "source", None)
        if not src:
            self.alert("No source chosen", "Choose the folder or file you want to add first.")
            return
        try:
            dest = self.dest_rel()
        except ValueError as e:
            self.alert("Check the folder, name and date", str(e))
            return
        self.store.remove_all()
        self.plan = None
        drive, idx, rescan = self.drive, self.index, self.rescan_row.get_active()

        def work():
            first = idx.empty
            if first:
                self.set_status("Building the index for this drive (first time only, can take a while)…")
            plan = core.analyze(src, dest, idx, self.set_status, self.set_progress, self.cancel, rescan)
            if first or rescan:
                self.publish_index(drive, idx)
            GLib.idle_add(self.show_plan, plan)
        self.run_bg(work)

    def show_plan(self, plan):
        self.plan = plan
        self.store.splice(0, 0, [Row(i) for i in plan.items])
        t = plan.totals()
        err = f"<span foreground='red'>{t['ERROR'][0]} errors</span>" if t["ERROR"][0] else "0 errors"
        self.summary.set_markup(
            f"<b>Will copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])})</b>   ·   "
            f"Skip {t['SKIP'][0]:,} files ({core.human(t['SKIP'][1])}) already on the drive   ·   {err}")
        self.set_busy(False)
        self.status.set_text("Review the list, then press “Copy to drive”. Nothing has been written yet.")
        return False

    def match_row(self, row):
        want = {0: None, 1: "COPY", 2: "SKIP", 3: "ERROR"}[self.filter_dd.get_selected()] if hasattr(self, "filter_dd") else None
        if want and row.item.action != want:
            return False
        q = self.filter_entry.get_text().lower() if hasattr(self, "filter_entry") else ""
        return not q or q in row.item.rel.lower() or q in row.item.existing.lower()

    def refilter(self):
        self.filter.changed(Gtk.FilterChange.DIFFERENT)

    def open_existing(self, rel):
        if not (self.drive and self.drive.root):
            self.toast_msg("Connect the drive first.")
            return
        p = core.safe_join(self.drive.root, rel)
        if p:
            core.open_in_file_manager(p if os.path.isdir(p) else os.path.dirname(p))

    def on_row_activate(self, cv, pos):
        it = cv.get_model().get_item(pos).item
        if not it.existing:
            return
        if self.plan and it.existing.startswith(self.plan.dest_rel + "/"):
            self.toast_msg("That copy is inside this same backup.")
        else:
            self.open_existing(it.existing)

    def on_copy(self):
        t = self.plan.totals()
        delete = self.del_check.get_active()
        body = (f"Copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])}) to\n{self.plan.dest_rel}\n\n"
                f"Each file is read back and checked after copying. {t['SKIP'][0]:,} files already on the drive are "
                "skipped and listed in _DUPLICATES_SKIPPED.txt inside the new folder.\n\n"
                "Do not unplug the drive or its power until it finishes.")
        if delete:
            body += ("\n\n⚠ The original files on this computer will be DELETED after everything is verified "
                     "(only if there are no errors). This cannot be undone.")
        d = Adw.AlertDialog(heading="Copy to the backup drive?", body=body)
        d.add_response("cancel", "Cancel")
        d.add_response("go", "Copy and delete originals" if delete else "Copy")
        d.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if delete else Adw.ResponseAppearance.SUGGESTED)
        plan, drive, idx = self.plan, self.drive, self.index

        def resp(_d, r):
            if r != "go":
                return

            def work():
                self.set_status("Preparing the drive for writing…")
                core.prepare(drive, write=True)
                try:
                    res = core.execute(plan, idx, delete, self.set_status, self.set_progress, self.cancel)
                finally:
                    self.set_status("Finishing…")
                    core.finish(drive)
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
            msg = f"Done: {t['COPY'][0]:,} files copied and verified, {t['SKIP'][0]:,} duplicates skipped."
            if res["deleted"]:
                msg += f" {res['deleted']:,} original files deleted."
            if res.get("kept"):
                msg += f" {res['kept']:,} originals kept because their drive copy could not be re-confirmed."
        self.store.remove_all()
        self.store.splice(0, 0, [Row(i) for i in self.plan.items])
        self.status.set_text(msg)
        self.alert("Copy finished" if not res["failures"] else "Copy finished with problems", msg)
        self.set_busy(False)
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
        if not self.index:
            self.find_status.set_text("Choose a drive first.")
            return
        rows = self.index.search(q)
        self.find_store.remove_all()
        self.find_store.splice(0, 0, [FindRow(p, s) for p, s in rows])
        self.find_status.set_text(f"{len(rows)} result(s)" + (" (first 500 shown)" if len(rows) == 500 else ""))

    def on_find_activate(self, cv, pos):
        self.open_existing(cv.get_model().get_item(pos).path)

    def on_page(self, *_):
        if self.stack.get_visible_child_name() == "history":
            try:
                with open(core.HISTORY, encoding="utf-8") as f:
                    txt = f.read()
            except OSError:
                txt = "No history yet."
            self.hist_view.get_buffer().set_text(txt)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        (self.props.active_window or Window(self)).present()

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
