"""Core logic for adding new backups to the LELLX 6 TB backup drive without duplicate files.

Used by both the GUI (backup_adder.py) and the CLI (lellx-add).

The drive: Seagate ST6000VX001 in a USB enclosure that reports 4096-byte sectors, while the
disk was partitioned with 512-byte sectors, so the NTFS partition is reached through a
read-only/read-write loop device with --sector-size 512. If it is ever connected through an
enclosure that reports 512-byte sectors, the partition shows up directly and is used as is.

Index: SQLite table f(path, size, mtime, phash, fhash, err, ino) with paths relative to the
drive root. Kept on the drive (/_INDEX/lellx-index.db) and on the laptop
(~/backup-tools/state/index.db); the newer one wins and both are written after each change.

Dedup rules (same as the 2026-11-12 cleanup):
  * a file is skipped only if a byte-identical file (same size + full sha256) already exists
    on the drive and still exists with the size/mtime recorded in the index
  * VM disks and disk-image backups are never skipped (moving a piece breaks the whole set)
  * empty files are always copied (keeps folder structure readable)
  * files identical to another file in the same new backup are skipped, pointing to that copy
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass, field

MOUNT = "/mnt/lellx"
LABEL = "LELLX"
MODEL_HINT = "ST6000VX001"
HOME_STATE = os.path.expanduser("~/backup-tools/state")
LOCAL_INDEX = os.path.join(HOME_STATE, "index.db")
DRIVE_INDEX_DIR = "_INDEX"
DRIVE_INDEX = os.path.join(DRIVE_INDEX_DIR, "lellx-index.db")
HISTORY = os.path.join(HOME_STATE, "history.log")

CATEGORIES = [
    ("BACKUPS/Laptops", "Laptops"),
    ("BACKUPS/Phones", "Phones"),
    ("BACKUPS/SD cards & USBs", "SD cards & USBs"),
    ("BACKUPS/Collections (mixed, undated)", "Collections (mixed, undated)"),
    ("BACKUPS/Old drives", "Old drives"),
    ("BACKUPS/Projects", "Projects"),
    ("LLO", "LLO (organized library)"),
]

IMAGE_EXT = re.compile(r"\.(vdi|vmdk|vhdx?|vbox|vbox-prev|sav|nvram|qcow2|vmx|vmsn|vmem|adi|afi|tib|tibx|gho|wim)$", re.I)
OWN_FILES = {"_DUPLICATES_REMOVED.txt", "_DUPLICATES_SKIPPED.txt", "_MANIFEST.sha256", "_ADDED_INFO.txt",
             "_README.txt", "_NOW_FOLDER_MOVED.txt", "RENAMES.txt"}
CHUNK = 4 * 1024 * 1024


class DriveError(Exception):
    pass


def log_history(msg):
    os.makedirs(HOME_STATE, exist_ok=True)
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n"
    with open(HISTORY, "a") as f:
        f.write(line)


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


# ----------------------------------------------------------------------------- privileges / drive
def _priv(cmd):
    """Run a root command: passwordless sudo if allowed, else pkexec (graphical password prompt)."""
    r = subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True)
    if r.returncode != 0 and "password" in (r.stderr or "").lower():
        r = subprocess.run(["pkexec"] + cmd, capture_output=True, text=True)
    return r


def find_disk():
    """Return /dev/sdX of the backup drive, or None."""
    r = subprocess.run(["lsblk", "-J", "-d", "-o", "NAME,MODEL,TRAN,SIZE"], capture_output=True, text=True)
    for d in json.loads(r.stdout or "{}").get("blockdevices", []):
        if MODEL_HINT in (d.get("model") or ""):
            return "/dev/" + d["name"]
    # fallback: any partition labelled LELLX
    r = subprocess.run(["lsblk", "-J", "-o", "NAME,LABEL,PKNAME"], capture_output=True, text=True)
    for d in json.loads(r.stdout or "{}").get("blockdevices", []):
        for c in d.get("children") or []:
            if c.get("label") == LABEL:
                return "/dev/" + d["name"]
    return None


def mount_state():
    """'ro', 'rw' or None for MOUNT."""
    with open("/proc/mounts") as f:
        for line in f:
            p = line.split()
            if p[1] == MOUNT:
                return "ro" if "ro" in p[3].split(",") else "rw"
    return None


def _loops_for(disk):
    r = subprocess.run(["/usr/sbin/losetup", "-J", "-l"], capture_output=True, text=True)
    out = []
    for l in json.loads(r.stdout or "{}").get("loopdevices", []):
        raw = l.get("back-file") or ""
        bf = raw.replace(" (deleted)", "")
        # only loops backed by a whole USB disk: ours, or a stale one left after the disk dropped off
        if bf == disk or (raw.endswith("(deleted)") and re.fullmatch(r"/dev/sd[a-z]+", bf)):
            out.append(l["name"])
    return out


def busy_processes():
    r = _priv(["/usr/bin/fuser", "-vm", MOUNT])
    lines = [l for l in (r.stderr or "").splitlines()[1:] if "kernel mount" not in l and l.strip()]
    return lines


def unmount():
    if mount_state():
        subprocess.run(["sync"])
        r = _priv(["/usr/bin/umount", MOUNT])
        if r.returncode != 0:
            busy = busy_processes()
            raise DriveError("The drive is in use and cannot be switched. Close any Files window or program "
                             "showing the drive and try again.\n" + "\n".join(busy))
    disk = find_disk()
    for loop in _loops_for(disk) if disk else []:
        _priv(["/usr/sbin/losetup", "-d", loop])


def mount(mode="ro"):
    """Mount the drive at MOUNT in mode 'ro' or 'rw'. Re-mounts if currently in the other mode."""
    if mount_state() == mode and os.path.isdir(os.path.join(MOUNT, "BACKUPS")):
        return
    disk = find_disk()
    if not disk:
        raise DriveError("The 6 TB backup drive (LELLX) is not connected. Power the enclosure on first, "
                         "wait until the disk spins, then plug in the USB cable.")
    unmount()
    _priv(["/usr/bin/mkdir", "-p", MOUNT])
    # direct partition (enclosure with 512-byte sectors)?
    part = None
    r = subprocess.run(["lsblk", "-J", "-o", "NAME,LABEL", disk], capture_output=True, text=True)
    for d in json.loads(r.stdout or "{}").get("blockdevices", []):
        for c in d.get("children") or []:
            if c.get("label") == LABEL:
                part = "/dev/" + c["name"]
    if not part:
        args = ["/usr/sbin/losetup", "-f", "--show", "-P", "--sector-size", "512"]
        if mode == "ro":
            args.insert(1, "-r")
        r = _priv(args + [disk])
        if r.returncode != 0:
            raise DriveError("Could not attach the drive: " + r.stderr.strip())
        loop = r.stdout.strip()
        time.sleep(1)
        part = loop + "p1"
    opts = f"{mode},uid={os.getuid()},gid={os.getgid()}" + (",big_writes" if mode == "rw" else "")
    r = _priv(["/usr/bin/mount", "-t", "ntfs-3g", "-o", opts, part, MOUNT])
    if r.returncode != 0:
        raise DriveError("Could not mount the drive: " + r.stderr.strip())
    if not os.path.isdir(os.path.join(MOUNT, "BACKUPS")):
        raise DriveError("Mounted a drive, but it does not look like the LELLX backup drive (no BACKUPS folder).")


def eject():
    """Unmount, detach and power off the disk so it can be unplugged safely."""
    disk = find_disk()
    unmount()
    if disk:
        subprocess.run(["sync"])
        r = subprocess.run(["udisksctl", "power-off", "-b", disk], capture_output=True, text=True)
        if r.returncode != 0:
            _priv(["/usr/bin/udisksctl", "power-off", "-b", disk])


def drive_status():
    disk = find_disk()
    st = {"connected": bool(disk), "disk": disk, "mounted": mount_state(), "free": None, "total": None,
          "smart": None, "crc": None, "errors_recent": []}
    if st["mounted"]:
        s = os.statvfs(MOUNT)
        st["free"] = s.f_bavail * s.f_frsize
        st["total"] = s.f_blocks * s.f_frsize
    if disk:
        r = _priv(["/usr/sbin/smartctl", "-d", "sat", "-H", "-A", disk])
        out = r.stdout or ""
        m = re.search(r"test result: (\S+)", out)
        st["smart"] = m.group(1) if m else None
        for name in ("Reallocated_Sector_Ct", "Current_Pending_Sector", "Offline_Uncorrectable", "UDMA_CRC_Error_Count"):
            m = re.search(name + r".*\s(\d+)\s*$", out, re.M)
            if m:
                st[name] = int(m.group(1))
        st["crc"] = st.get("UDMA_CRC_Error_Count")
    r = _priv(["/usr/bin/dmesg", "-T", "--since", "1 hour ago"])
    st["errors_recent"] = [l for l in (r.stdout or "").splitlines()
                           if re.search(r"over-current|DID_ERROR|I/O error, dev (sd|loop)", l)][-5:]
    return st


# ----------------------------------------------------------------------------- index
def _newer_index():
    """Return path of the newest available index (drive or laptop)."""
    cands = []
    d = os.path.join(MOUNT, DRIVE_INDEX)
    for p in (d, LOCAL_INDEX):
        if os.path.exists(p):
            cands.append((os.path.getmtime(p), p))
    if not cands:
        raise DriveError("No index found. Expected " + LOCAL_INDEX)
    return max(cands)[1]


class Index:
    """Working copy of the index on the laptop; published to drive + laptop with save()."""

    def __init__(self):
        os.makedirs(HOME_STATE, exist_ok=True)
        src = _newer_index()
        self.work = os.path.join(HOME_STATE, "index.work.db")
        if src != LOCAL_INDEX or not os.path.exists(self.work) or os.path.getmtime(self.work) < os.path.getmtime(src):
            shutil.copyfile(src, self.work)
        self.db = sqlite3.connect(self.work, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("create index if not exists i_size on f(size)")
        self.db.execute("create index if not exists i_sf on f(size, fhash)")
        self.db.execute("create index if not exists i_path on f(path)")

    def by_size(self, size):
        with self.lock:
            return self.db.execute("select path, mtime, fhash from f where size=?", (size,)).fetchall()

    def set_hash(self, path, h):
        with self.lock:
            self.db.execute("update f set fhash=? where path=?", (h, path))

    def drop(self, path):
        with self.lock:
            self.db.execute("delete from f where path=?", (path,))

    def add(self, path, size, mtime, h):
        with self.lock:
            self.db.execute("insert or replace into f(path,size,mtime,fhash) values(?,?,?,?)", (path, size, mtime, h))

    def commit(self):
        with self.lock:
            self.db.commit()

    def search(self, text, limit=500):
        with self.lock:
            return self.db.execute("select path, size from f where path like ? order by path limit ?",
                                   ("%" + text + "%", limit)).fetchall()

    def stats(self):
        with self.lock:
            return self.db.execute("select count(*), coalesce(sum(size),0) from f").fetchone()

    def save(self, to_drive=True):
        """Write the index to the laptop and (if mounted rw) to the drive."""
        with self.lock:
            self.db.commit()
            tmp = LOCAL_INDEX + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
            self.db.execute("vacuum into ?", (tmp,))
        os.replace(tmp, LOCAL_INDEX)
        if to_drive and mount_state() == "rw":
            os.makedirs(os.path.join(MOUNT, DRIVE_INDEX_DIR), exist_ok=True)
            dst = os.path.join(MOUNT, DRIVE_INDEX)
            shutil.copyfile(LOCAL_INDEX, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
            with open(os.path.join(MOUNT, DRIVE_INDEX_DIR, "README.txt"), "w") as f:
                f.write("Index of every file on this drive (path, size, sha256) used by Backup Adder\n"
                        "to avoid copying duplicate files. Safe to delete; it can be rebuilt with\n"
                        "'Rescan drive' (slow). Updated " + time.strftime("%Y-%m-%d %H:%M") + ".\n")


# ----------------------------------------------------------------------------- hashing
def sha256_file(path, progress=None, cancel=None):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            if cancel and cancel.is_set():
                raise InterruptedError
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
            if progress:
                progress(len(b))
    return h.hexdigest()


# ----------------------------------------------------------------------------- analysis
@dataclass
class Item:
    rel: str                  # path inside the source
    size: int
    action: str = "COPY"      # COPY | SKIP | ERROR
    reason: str = ""
    existing: str = ""        # drive path of the identical file (SKIP)
    sha: str = ""
    mtime: float = 0.0


@dataclass
class Plan:
    source: str
    dest_rel: str             # e.g. BACKUPS/Phones/Samsung A30 - 2027-03-01
    items: list = field(default_factory=list)

    def totals(self):
        t = {"COPY": [0, 0], "SKIP": [0, 0], "ERROR": [0, 0]}
        for i in self.items:
            t[i.action][0] += 1
            t[i.action][1] += i.size
        return t


def make_dest(category, name, date):
    name = re.sub(r"[\\/:*?\"<>|]", "_", name.strip()).strip(". ")
    if not name:
        raise ValueError("Please enter a name for the backup.")
    folder = f"{name} - {date}" if date else f"{name} - no date"
    return f"{category}/{folder}"


def analyze(src, dest_rel, index, status=None, progress=None, cancel=None):
    """Walk src and decide COPY/SKIP for every file. Reads the drive (read-only is enough)."""
    src = os.path.abspath(src)
    plan = Plan(source=src, dest_rel=dest_rel)
    if os.path.exists(os.path.join(MOUNT, dest_rel)):
        raise ValueError(f"A folder with this name already exists on the drive:\n{dest_rel}\nChoose another name or date.")
    # 1. list files
    if status:
        status("Listing files…")
    files = []
    if os.path.isfile(src):
        files.append((os.path.basename(src), src))
    else:
        for dp, dns, fns in os.walk(src):
            dns.sort()
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                files.append((os.path.relpath(full, src), full))
            if cancel and cancel.is_set():
                raise InterruptedError
    total_bytes = 0
    for rel, full in files:
        try:
            if os.path.islink(full):
                plan.items.append(Item(rel, 0, "SKIP", "symbolic link (not copied, NTFS can't store it)"))
                continue
            st = os.stat(full)
            plan.items.append(Item(rel, st.st_size, mtime=st.st_mtime))
            total_bytes += st.st_size
        except OSError as e:
            plan.items.append(Item(rel, 0, "ERROR", f"cannot read: {e.strerror}"))
    # 2. decide
    done_bytes = [0]

    def prog(n):
        done_bytes[0] += n
        if progress:
            progress(done_bytes[0], total_bytes)

    seen = {}  # (size, sha) -> rel inside this backup
    seen_sizes = set()
    for n, it in enumerate(plan.items):
        if cancel and cancel.is_set():
            raise InterruptedError
        if it.action != "COPY":
            continue
        full = plan.source if os.path.isfile(plan.source) else os.path.join(plan.source, it.rel)
        if status and n % 50 == 0:
            status(f"Checking {n + 1:,} / {len(plan.items):,}: {it.rel}")
        if it.size == 0:
            it.reason = "empty file (always copied)"
            prog(0)
            continue
        if IMAGE_EXT.search(it.rel):
            it.reason = "VM / disk-image file (never deduplicated)"
            it.sha = sha256_file(full, prog, cancel)
            continue
        cands = index.by_size(it.size)
        if not cands and it.size not in seen_sizes:
            it.reason = "new: no file of this size on the drive"
            it.sha = sha256_file(full, prog, cancel)
            seen[(it.size, it.sha)] = it.rel
            seen_sizes.add(it.size)
            continue
        try:
            it.sha = sha256_file(full, prog, cancel)
        except OSError as e:
            it.action, it.reason = "ERROR", f"cannot read: {e.strerror}"
            continue
        if (it.size, it.sha) in seen:
            it.action = "SKIP"
            it.existing = dest_rel + "/" + seen[(it.size, it.sha)]
            it.reason = "identical to another file in this same backup"
            continue
        match = _find_match(index, it.size, it.sha, cands, cancel)
        if match:
            it.action, it.existing, it.reason = "SKIP", match, "identical file already on the drive"
        else:
            it.reason = "new: different content from same-size files on the drive"
            seen[(it.size, it.sha)] = it.rel
            seen_sizes.add(it.size)
    index.commit()   # keep hashes computed for drive files, even if nothing gets copied
    if status:
        status("Analysis done.")
    return plan


def _find_match(index, size, sha, cands, cancel):
    """Return a drive path whose content equals sha, verifying the file still exists unchanged."""
    # cheap first: index rows that already have a hash
    for path, mtime, fh in sorted(cands, key=lambda c: (c[2] is None, not c[0].startswith("LLO/"), c[0])):
        if cancel and cancel.is_set():
            raise InterruptedError
        full = os.path.join(MOUNT, path)
        try:
            st = os.stat(full)
        except OSError:
            index.drop(path)          # file no longer on the drive
            continue
        if st.st_size != size:
            index.drop(path)
            continue
        if fh is None or (mtime and abs(st.st_mtime - mtime) > 2):
            try:
                fh = sha256_file(full, None, cancel)     # lazily hash the drive copy, cache it
            except OSError:
                continue
            index.set_hash(path, fh)
        if fh == sha:
            return path
    return None


# ----------------------------------------------------------------------------- copy
def execute(plan, index, delete_source=False, status=None, progress=None, cancel=None):
    """Copy the COPY items with read-back verification, write notes, update index. Needs rw mount."""
    if mount_state() != "rw":
        raise DriveError("Drive is not mounted read-write.")
    dest = os.path.join(MOUNT, plan.dest_rel)
    os.makedirs(dest, exist_ok=False)
    copy_items = [i for i in plan.items if i.action == "COPY"]
    total = sum(i.size for i in copy_items) * 2 or 1    # write + read-back
    done = [0]

    def prog(n):
        done[0] += n
        if progress:
            progress(done[0], total)

    failures = []
    manifest = []
    single = os.path.isfile(plan.source)
    for k, it in enumerate(copy_items):
        if cancel and cancel.is_set():
            break
        src = plan.source if single else os.path.join(plan.source, it.rel)
        dst = os.path.join(dest, it.rel)
        if status:
            status(f"Copying {k + 1:,} / {len(copy_items):,}: {it.rel}")
        ok = False
        for attempt in (1, 2):
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                h = hashlib.sha256()
                with open(src, "rb") as fi, open(dst, "wb") as fo:
                    while True:
                        b = fi.read(CHUNK)
                        if not b:
                            break
                        fo.write(b)
                        h.update(b)
                        prog(len(b))
                    fo.flush()
                    os.fsync(fo.fileno())
                src_sha = h.hexdigest()
                back = sha256_file(dst, prog)
                if back == src_sha and (not it.sha or it.sha == src_sha):
                    ok = True
                    it.sha = src_sha
                    try:
                        os.utime(dst, (it.mtime, it.mtime))
                    except OSError:
                        pass
                    break
            except OSError as e:
                it.reason += f" | copy error: {e.strerror}"
        if ok:
            manifest.append(f"{it.sha}  ./{it.rel}")
            index.add(plan.dest_rel + "/" + it.rel, it.size, it.mtime, it.sha)
        else:
            it.action = "ERROR"
            it.reason = "copy failed verification"
            failures.append(it)
    cancelled = bool(cancel and cancel.is_set())
    # notes
    with open(os.path.join(dest, "_MANIFEST.sha256"), "w") as f:
        f.write("\n".join(manifest) + ("\n" if manifest else ""))
    skipped = [i for i in plan.items if i.action == "SKIP"]
    with open(os.path.join(dest, "_DUPLICATES_SKIPPED.txt"), "w") as f:
        f.write(f"Files of this backup that were NOT copied ({time.strftime('%Y-%m-%d')}), with the reason.\n"
                "Identical files already on the drive are listed with '=>' and the location of the existing copy.\n\n")
        for i in skipped:
            f.write(f"{i.rel}\t=>\t{i.existing or '-'}\t({i.reason})\n")
    t = plan.totals()
    with open(os.path.join(dest, "_ADDED_INFO.txt"), "w") as f:
        f.write(f"Added with Backup Adder on {time.strftime('%Y-%m-%d %H:%M')}\n"
                f"Source: {plan.source}\n"
                f"Copied: {t['COPY'][0]} files ({human(t['COPY'][1])}), verified by sha256 read-back\n"
                f"Skipped: {t['SKIP'][0]} files ({human(t['SKIP'][1])}) - see _DUPLICATES_SKIPPED.txt\n"
                f"Errors: {len(failures) + t['ERROR'][0]}\n"
                + ("CANCELLED before finishing.\n" if cancelled else ""))
    if status:
        status("Saving index…")
    index.save(to_drive=True)
    # optional: delete source files that are safely on the drive
    deleted = 0
    if delete_source and not cancelled and not failures:
        for it in plan.items:
            if it.action in ("COPY", "SKIP") and it.reason != "symbolic link (not copied, NTFS can't store it)":
                p = plan.source if single else os.path.join(plan.source, it.rel)
                try:
                    os.remove(p)
                    deleted += 1
                except OSError:
                    pass
        if not single:
            for dp, dns, fns in os.walk(plan.source, topdown=False):
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
    log_history(f"ADD {plan.source} -> {plan.dest_rel}: copied {t['COPY'][0]} ({human(t['COPY'][1])}), "
                f"skipped {t['SKIP'][0]} ({human(t['SKIP'][1])}), failed {len(failures)}, "
                f"source files deleted {deleted}{' CANCELLED' if cancelled else ''}")
    return {"failures": failures, "cancelled": cancelled, "deleted": deleted}


# ----------------------------------------------------------------------------- rescan
def quick_rescan(index, status=None, cancel=None):
    """Bring the index in line with the drive: add new files (hash lazily later), drop missing ones,
    clear hashes of files whose size/mtime changed. Reads the whole folder tree (slow, ~20-40 min)."""
    seen = set()
    added = changed = 0
    n = 0
    for dp, dns, fns in os.walk(MOUNT):
        rel_dp = os.path.relpath(dp, MOUNT)
        if rel_dp == ".":
            dns[:] = [d for d in dns if d not in ("$RECYCLE.BIN", "System Volume Information", DRIVE_INDEX_DIR)]
        for fn in fns:
            if cancel and cancel.is_set():
                raise InterruptedError
            if fn in OWN_FILES:
                continue
            rel = fn if rel_dp == "." else rel_dp + "/" + fn
            full = os.path.join(dp, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            seen.add(rel)
            with index.lock:
                row = index.db.execute("select size, mtime from f where path=?", (rel,)).fetchone()
                if row is None:
                    index.db.execute("insert into f(path,size,mtime) values(?,?,?)", (rel, st.st_size, st.st_mtime))
                    added += 1
                elif row[0] != st.st_size or (row[1] and abs(row[1] - st.st_mtime) > 2):
                    index.db.execute("update f set size=?, mtime=?, phash=NULL, fhash=NULL where path=?",
                                     (st.st_size, st.st_mtime, rel))
                    changed += 1
            n += 1
            if status and n % 2000 == 0:
                status(f"Rescanning drive: {n:,} files ({added:,} new, {changed:,} changed)")
    with index.lock:
        allp = [r[0] for r in index.db.execute("select path from f")]
    missing = [p for p in allp if p not in seen]
    with index.lock:
        index.db.executemany("delete from f where path=?", [(p,) for p in missing])
    log_history(f"RESCAN files={n} new={added} changed={changed} removed={len(missing)}")
    return {"files": n, "added": added, "changed": changed, "removed": len(missing)}


def export_report(plan, path):
    with open(path, "w") as f:
        f.write("action\tsize\tfile in backup\treason\texisting copy on drive\n")
        for i in plan.items:
            f.write(f"{i.action}\t{i.size}\t{i.rel}\t{i.reason}\t{i.existing}\n")
