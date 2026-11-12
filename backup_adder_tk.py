#!/usr/bin/env python3
"""Backup Adder (Windows / cross-platform Tk version).

Choose a drive, choose the backup to add, press Analyze: every file is listed as Copy or Skip
with the reason, and skipped files show where the identical copy already is on the drive.
Nothing is written until you press "Copy to drive". The drive's index is created automatically
the first time (slower), then reused.
"""
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__)))
import backup_core as core  # noqa: E402

ACTIONS = {"COPY": "Copy", "SKIP": "Skip", "ERROR": "Error"}
OTHER = "Other folder (network share, folder on a disk)…"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Backup Adder")
        self.geometry("1250x820")
        self.minsize(900, 600)
        try:
            ttk.Style(self).theme_use("vista" if core.WINDOWS else "clam")
        except tk.TclError:
            pass
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.drives = []
        self.drive = None
        self.index = None
        self.plan = None
        self.after(100, self.pump)
        self.build()
        self.refresh_drives()

    # ------------------------------------------------------------------ layout
    def build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.nb = nb
        add = ttk.Frame(nb, padding=8)
        find = ttk.Frame(nb, padding=8)
        hist = ttk.Frame(nb, padding=8)
        nb.add(add, text="  Add backup  ")
        nb.add(find, text="  Find on drive  ")
        nb.add(hist, text="  History  ")
        nb.bind("<<NotebookTabChanged>>", self.on_tab)

        # drive
        df = ttk.LabelFrame(add, text="Backup drive", padding=8)
        df.pack(fill="x")
        self.drive_var = tk.StringVar()
        self.drive_cb = ttk.Combobox(df, textvariable=self.drive_var, state="readonly", width=70)
        self.drive_cb.grid(row=0, column=0, sticky="we")
        self.drive_cb.bind("<<ComboboxSelected>>", lambda e: self.on_drive_selected())
        ttk.Button(df, text="Refresh", command=self.refresh_drives).grid(row=0, column=1, padx=4)
        ttk.Button(df, text="Open", command=self.open_drive).grid(row=0, column=2, padx=4)
        ttk.Button(df, text="Safely eject", command=self.on_eject).grid(row=0, column=3, padx=4)
        ttk.Button(df, text="Rescan index", command=self.on_rescan).grid(row=0, column=4, padx=4)
        self.drive_info = ttk.Label(df, text="", foreground="#555")
        self.drive_info.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        df.columnconfigure(0, weight=1)

        # new backup
        bf = ttk.LabelFrame(add, text="New backup", padding=8)
        bf.pack(fill="x", pady=8)
        ttk.Label(bf, text="Source:").grid(row=0, column=0, sticky="w")
        self.src_var = tk.StringVar()
        ttk.Entry(bf, textvariable=self.src_var).grid(row=0, column=1, columnspan=3, sticky="we", padx=4)
        ttk.Button(bf, text="Folder…", command=lambda: self.pick_source(True)).grid(row=0, column=4)
        ttk.Button(bf, text="File…", command=lambda: self.pick_source(False)).grid(row=0, column=5, padx=4)
        ttk.Label(bf, text="Goes into folder:").grid(row=1, column=0, sticky="w", pady=4)
        self.parent_var = tk.StringVar()
        self.parent_cb = ttk.Combobox(bf, textvariable=self.parent_var)
        self.parent_cb.grid(row=1, column=1, sticky="we", padx=4)
        ttk.Label(bf, text="Name:").grid(row=1, column=2, sticky="e")
        self.name_var = tk.StringVar()
        ttk.Entry(bf, textvariable=self.name_var, width=28).grid(row=1, column=3, sticky="we", padx=4)
        ttk.Label(bf, text="Date (YYYY-MM-DD):").grid(row=1, column=4, sticky="e")
        self.date_var = tk.StringVar(value=time.strftime("%Y-%m-%d"))
        ttk.Entry(bf, textvariable=self.date_var, width=12).grid(row=1, column=5, sticky="w", padx=4)
        self.dest_lbl = ttk.Label(bf, text="", font=("TkFixedFont", 9))
        self.dest_lbl.grid(row=2, column=0, columnspan=6, sticky="w")
        opts = ttk.Frame(bf)
        opts.grid(row=3, column=0, columnspan=6, sticky="we", pady=(6, 0))
        self.rescan_var = tk.BooleanVar()
        ttk.Checkbutton(opts, text="Rescan the drive first (if files were changed outside this app)",
                        variable=self.rescan_var).pack(side="left")
        ttk.Label(opts, text="   Compare by:").pack(side="left")
        self.match_var = tk.StringVar(value=core.MATCH_MODES["content"])
        ttk.Combobox(opts, textvariable=self.match_var, values=list(core.MATCH_MODES.values()),
                     state="readonly", width=38).pack(side="left", padx=4)
        self.btn_analyze = ttk.Button(opts, text="Analyze", command=self.on_analyze)
        self.btn_analyze.pack(side="right")
        bf.columnconfigure(1, weight=2)
        bf.columnconfigure(3, weight=1)
        for v in (self.parent_var, self.name_var, self.date_var):
            v.trace_add("write", lambda *_: self.update_dest())

        # results
        rf = ttk.Frame(add)
        rf.pack(fill="both", expand=True)
        top = ttk.Frame(rf)
        top.pack(fill="x")
        self.summary = ttk.Label(top, text="Analyze a backup to see what will be copied and what is already on the drive.")
        self.summary.pack(side="left")
        self.filter_q = tk.StringVar()
        e = ttk.Entry(top, textvariable=self.filter_q, width=24)
        e.pack(side="right")
        self.filter_q.trace_add("write", lambda *_: self.fill_table())
        self.filter_kind = ttk.Combobox(top, values=["All", "Will copy", "Skipped", "Errors"], state="readonly", width=10)
        self.filter_kind.current(0)
        self.filter_kind.pack(side="right", padx=4)
        self.filter_kind.bind("<<ComboboxSelected>>", lambda e: self.fill_table())
        ttk.Label(top, text="Show:").pack(side="right")
        cols = ("action", "file", "size", "reason", "existing")
        tf = ttk.Frame(rf)
        tf.pack(fill="both", expand=True, pady=4)
        self.tree = ttk.Treeview(tf, columns=cols, show="headings")
        for c, t, w in zip(cols, ("Action", "File in new backup", "Size", "Reason", "Already on drive at"),
                           (60, 380, 80, 300, 380)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, stretch=c in ("file", "existing", "reason"), anchor="e" if c == "size" else "w")
        self.tree.tag_configure("COPY", foreground="#1a7f37")
        self.tree.tag_configure("SKIP", foreground="#9a6700")
        self.tree.tag_configure("ERROR", foreground="#cf222e")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self.on_row)
        ttk.Label(rf, text="Tip: double-click a skipped file to open the folder of its existing copy.",
                  foreground="#666").pack(anchor="w")

        # bottom
        bot = ttk.Frame(add)
        bot.pack(fill="x", pady=(6, 0))
        self.pbar = ttk.Progressbar(bot, mode="determinate", maximum=1000)
        self.pbar.pack(fill="x")
        row = ttk.Frame(bot)
        row.pack(fill="x", pady=4)
        self.status = ttk.Label(row, text="")
        self.status.pack(side="left", fill="x", expand=True)
        self.del_var = tk.BooleanVar()
        ttk.Checkbutton(row, text="Delete originals after verified copy", variable=self.del_var).pack(side="left", padx=6)
        self.btn_export = ttk.Button(row, text="Export report…", command=self.on_export, state="disabled")
        self.btn_export.pack(side="left", padx=2)
        self.btn_cancel = ttk.Button(row, text="Cancel", command=self.cancel.set, state="disabled")
        self.btn_cancel.pack(side="left", padx=2)
        self.btn_copy = ttk.Button(row, text="Copy to drive", command=self.on_copy, state="disabled")
        self.btn_copy.pack(side="left", padx=2)

        # find
        ttk.Label(find, text="Search the selected drive by file or folder name (uses its index). "
                             "Double-click a result to open its folder.").pack(anchor="w")
        self.find_var = tk.StringVar()
        fe = ttk.Entry(find, textvariable=self.find_var)
        fe.pack(fill="x", pady=6)
        fe.bind("<Return>", lambda e: self.on_find())
        self.find_tree = ttk.Treeview(find, columns=("path", "size"), show="headings")
        self.find_tree.heading("path", text="Path on drive")
        self.find_tree.heading("size", text="Size")
        self.find_tree.column("path", width=900, stretch=True)
        self.find_tree.column("size", width=90, anchor="e")
        self.find_tree.pack(fill="both", expand=True)
        self.find_tree.bind("<Double-1>", self.on_find_row)
        self.find_status = ttk.Label(find, text="")
        self.find_status.pack(anchor="w")

        # history
        self.hist = tk.Text(hist, wrap="word", font=("TkFixedFont", 9))
        self.hist.pack(fill="both", expand=True)

    # ------------------------------------------------------------------ threading helpers
    def pump(self):
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(100, self.pump)

    def ui(self, fn, *args):
        self.q.put((fn, args))

    def set_status(self, text):
        self.ui(self.status.configure, {"text": text})

    def set_progress(self, done, total):
        self.ui(self.pbar.configure, {"value": int(1000 * done / total) if total else 0})

    def set_busy(self, busy):
        self.busy = busy
        st = "disabled" if busy else "normal"
        self.btn_analyze.configure(state=st)
        self.btn_cancel.configure(state="normal" if busy else "disabled")
        can_copy = (not busy and self.plan is not None and not self.plan.copied
                    and any(i.action == "COPY" for i in self.plan.items))
        self.btn_copy.configure(state="normal" if can_copy else "disabled")
        self.btn_export.configure(state="normal" if (not busy and self.plan) else "disabled")
        if not busy:
            self.pbar.configure(value=0)

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
            self.ui(self.bg_done, err)
        threading.Thread(target=work, daemon=True).start()

    def bg_done(self, err):
        self.set_busy(False)
        if err == "cancel":
            self.status.configure(text="Cancelled.")
        elif err is not None:
            self.status.configure(text="")
            messagebox.showerror("Something went wrong", str(err), parent=self)
        self.show_drive_info()

    # ------------------------------------------------------------------ drives
    def refresh_drives(self):
        try:
            self.drives = core.list_drives()
        except Exception as e:  # noqa: BLE001
            self.drives = []
            self.status.configure(text=f"Could not list drives: {e}")
        names = [d.title for d in self.drives] + [OTHER]
        self.drive_cb.configure(values=names)
        keep = self.drive.id if self.drive else None
        sel = next((i for i, d in enumerate(self.drives) if d.id == keep), 0 if self.drives else None)
        if sel is not None:
            self.drive_cb.current(sel)
            self.on_drive_selected()
        else:
            self.drive_var.set("")
            self.drive_info.configure(text="No external drive found. Connect one and press Refresh, or choose "
                                           "“Other folder…”.")

    def on_drive_selected(self):
        i = self.drive_cb.current()
        if i == len(self.drives):
            path = filedialog.askdirectory(title="Choose the destination folder", parent=self)
            if not path:
                return
            d = core.folder_drive(path)
            self.drives.append(d)
            self.drive_cb.configure(values=[x.title for x in self.drives] + [OTHER])
            self.drive_cb.current(len(self.drives) - 1)
        else:
            d = self.drives[i]
        self.drive, self.index, self.plan = d, None, None
        self.tree.delete(*self.tree.get_children())

        def work():
            self.set_status("Connecting the drive…")
            core.prepare(self.drive, write=False)
            self.index = core.Index(self.drive)
            parents = core.suggest_parents(self.drive.root)
            self.ui(self.parent_cb.configure, {"values": parents})
            if not self.parent_var.get() and "BACKUPS" in parents:
                self.ui(self.parent_var.set, "BACKUPS")
            self.set_status("Ready.")
            self.ui(self.update_dest)
        self.run_bg(work)

    def show_drive_info(self):
        if not self.drive:
            return
        parts = [self.drive.root or "not mounted"]

        def work():
            h = core.drive_health(self.drive) if self.drive.root else {}
            if h.get("free") is not None:
                parts.append(f"{core.human(h['free'])} free of {core.human(h['total'])}")
            if h.get("smart"):
                parts.append(f"health {h['smart']}")
            if self.index:
                n, size, hashed = self.index.stats()
                built = self.index.info("built")
                parts.append(f"index: {n:,} files, {hashed:,} hashed, updated {built}" if n or built
                             else "no index yet: it is built automatically on the first Analyze (can take a while)")
            for w in h.get("warnings", []):
                parts.append("⚠ " + w)
            self.ui(self.drive_info.configure, {"text": "   ·   ".join(parts)})
        threading.Thread(target=work, daemon=True).start()

    def open_drive(self):
        if self.drive and self.drive.root:
            core.open_in_file_manager(self.drive.root)

    def on_eject(self):
        if not self.drive:
            return

        def work():
            if self.index:
                self.index.commit()
            core.eject(self.drive)
            self.index = None
            self.set_status("Drive ejected. You can unplug it now.")
        self.run_bg(work)

    def on_rescan(self):
        if not self.drive:
            return
        if not messagebox.askokcancel("Rescan drive", "Read the whole folder tree of the drive and update its index? "
                                      "This can take a while on a big drive.", parent=self):
            return

        def work():
            core.prepare(self.drive, write=True)
            try:
                res = core.scan(self.index, self.set_status, self.cancel)
                self.index.save()
            finally:
                core.finish(self.drive)
            self.set_status(f"Index updated: {res['files']:,} files, {res['added']:,} new, {res['removed']:,} removed.")
        self.run_bg(work)

    # ------------------------------------------------------------------ add flow
    def pick_source(self, folder):
        p = filedialog.askdirectory(parent=self) if folder else filedialog.askopenfilename(parent=self)
        if p:
            self.src_var.set(os.path.normpath(p))
            if not self.name_var.get():
                self.name_var.set(os.path.basename(os.path.normpath(p)))

    def dest_rel(self):
        return core.make_dest(self.parent_var.get(), self.name_var.get(), self.date_var.get().strip())

    def update_dest(self):
        try:
            d = self.dest_rel()
            root = self.drive.root if self.drive else ""
            self.dest_lbl.configure(text="Will be saved as:  " + os.path.join(root, *d.split("/")), foreground="")
        except ValueError as e:
            self.dest_lbl.configure(text=str(e), foreground="#cf222e")

    def on_analyze(self):
        if not self.drive or not self.index:
            messagebox.showinfo("No drive", "Choose a drive first.", parent=self)
            return
        src = self.src_var.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showinfo("No source", "Choose the folder or file you want to add.", parent=self)
            return
        try:
            dest = self.dest_rel()
        except ValueError as e:
            messagebox.showwarning("Check name and date", str(e), parent=self)
            return
        self.plan = None
        self.tree.delete(*self.tree.get_children())
        rescan = self.rescan_var.get()
        match = {v: k for k, v in core.MATCH_MODES.items()}[self.match_var.get()]

        def work():
            first = self.index.empty
            if first:
                self.set_status("Building the index for this drive (first time only, can take a while)…")
            plan = core.analyze(src, dest, self.index, self.set_status, self.set_progress, self.cancel, rescan, match)
            if first or rescan:
                try:
                    core.prepare(self.drive, write=True)
                    self.index.save()
                except core.DriveError:
                    self.index.save(to_drive=False)
                finally:
                    core.finish(self.drive)
            self.ui(self.show_plan, plan)
        self.run_bg(work)

    def show_plan(self, plan):
        self.plan = plan
        t = plan.totals()
        self.summary.configure(text=f"Will copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])})   ·   "
                                    f"Skip {t['SKIP'][0]:,} ({core.human(t['SKIP'][1])}) already on the drive   ·   "
                                    f"{t['ERROR'][0]} errors")
        self.fill_table()
        self.set_busy(False)
        self.status.configure(text="Review the list, then press “Copy to drive”. Nothing has been written yet.")

    def fill_table(self):
        self.tree.delete(*self.tree.get_children())
        if not self.plan:
            return
        want = {0: None, 1: "COPY", 2: "SKIP", 3: "ERROR"}[self.filter_kind.current()]
        q = self.filter_q.get().lower()
        rows = [i for i in self.plan.items if (not want or i.action == want)
                and (not q or q in i.rel.lower() or q in i.existing.lower())]
        self._rows = rows
        for n, i in enumerate(rows[:20000]):
            self.tree.insert("", "end", iid=str(n), values=(ACTIONS[i.action], i.rel, core.human(i.size), i.reason,
                                                            i.existing), tags=(i.action,))
        if len(rows) > 20000:
            self.tree.insert("", "end", iid="more", values=("", f"… {len(rows) - 20000:,} more rows: use the filter "
                                                            "or Export report", "", "", ""))

    def on_row(self, _e):
        sel = self.tree.focus()
        if not sel or sel == "more":
            return
        it = self._rows[int(sel)]
        if it.existing and self.drive and self.drive.root:
            if it.existing.startswith(self.plan.dest_rel + "/"):
                self.status.configure(text="That copy is inside this same backup.")
                return
            p = core.safe_join(self.drive.root, it.existing)
            if p:
                core.open_in_file_manager(os.path.dirname(p))

    def on_copy(self):
        t = self.plan.totals()
        msg = (f"Copy {t['COPY'][0]:,} files ({core.human(t['COPY'][1])}) to\n{self.plan.dest_rel}\n\n"
               f"Each file is read back and checked after copying. {t['SKIP'][0]:,} files already on the drive "
               "are skipped and listed in _DUPLICATES_SKIPPED.txt in the new folder.\n\n"
               "Do not unplug the drive until it finishes.")
        delete = self.del_var.get()
        if delete:
            msg += ("\n\nWARNING: the original files on this computer will be DELETED after everything is "
                    "verified (only if there are no errors). This cannot be undone.")
        if not messagebox.askokcancel("Copy to the backup drive?", msg, icon="warning" if delete else "question",
                                      parent=self):
            return
        plan = self.plan

        def work():
            self.set_status("Preparing the drive for writing…")
            core.prepare(self.drive, write=True)
            try:
                res = core.execute(plan, self.index, delete, self.set_status, self.set_progress, self.cancel)
            finally:
                core.finish(self.drive)
            self.ui(self.copy_done, res)
        self.run_bg(work)

    def copy_done(self, res):
        t = self.plan.totals()
        if res["cancelled"]:
            msg = "Copy cancelled. Files copied so far are verified and listed in _MANIFEST.sha256."
        elif res["failures"]:
            msg = f"{len(res['failures'])} files failed verification and were marked as errors. Check the drive."
        else:
            msg = f"Done: {t['COPY'][0]:,} files copied and verified, {t['SKIP'][0]:,} duplicates skipped."
            if res["deleted"]:
                msg += f" {res['deleted']:,} original files deleted."
            if res.get("kept"):
                msg += f" {res['kept']:,} originals kept because their drive copy could not be re-confirmed."
        self.fill_table()
        self.status.configure(text=msg)
        (messagebox.showwarning if res["failures"] else messagebox.showinfo)("Copy finished", msg, parent=self)
        self.set_busy(False)

    def on_export(self):
        p = filedialog.asksaveasfilename(parent=self, defaultextension=".tsv",
                                         initialfile=os.path.basename(self.plan.dest_rel) + " - report.tsv")
        if p:
            core.export_report(self.plan, p)
            self.status.configure(text="Report saved: " + p)

    # ------------------------------------------------------------------ find / history
    def on_find(self):
        if not self.index:
            self.find_status.configure(text="Choose a drive first.")
            return
        rows = self.index.search(self.find_var.get().strip())
        self.find_tree.delete(*self.find_tree.get_children())
        for n, (p, s) in enumerate(rows):
            self.find_tree.insert("", "end", iid=str(n), values=(p, core.human(s)))
        self._find_rows = rows
        self.find_status.configure(text=f"{len(rows)} result(s)" + (" (first 500)" if len(rows) == 500 else ""))

    def on_find_row(self, _e):
        sel = self.find_tree.focus()
        if sel and self.drive and self.drive.root:
            p = core.safe_join(self.drive.root, self._find_rows[int(sel)][0])
            if p:
                core.open_in_file_manager(os.path.dirname(p))

    def on_tab(self, _e):
        if self.nb.index(self.nb.select()) == 2:
            try:
                with open(core.HISTORY, encoding="utf-8") as f:
                    txt = f.read()
            except OSError:
                txt = "No history yet."
            self.hist.delete("1.0", "end")
            self.hist.insert("1.0", txt)


if __name__ == "__main__":
    App().mainloop()
