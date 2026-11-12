"""Backup Adder core: add a backup to any drive (or folder) without copying duplicate files.

Works on Linux and Windows. Used by the GTK app (Linux), the Tk app (Windows/Linux) and the CLI.

Index (per drive): SQLite table f(path, size, mtime, phash, fhash, err, ino), paths relative to
the drive root with "/" separators. Stored on the drive in .backup-adder/index.db (hidden) and
cached on the computer. First use builds it by listing names and sizes only (no hashing);
a file is hashed only when a new file of the same size has to be compared with it, and the
hash is kept, so every later run is faster.

Special case "LELLX" (Linux): a 6 TB disk in an enclosure that reports 4096-byte sectors while
the disk was partitioned with 512-byte sectors; it is attached through a loop device with
--sector-size 512, mounted read-only, and switched to read-write only while copying.

Dedup rules:
  * skip a file only if a byte-identical file (size + full sha256) already exists on the drive
    and still exists unchanged (size/mtime as in the index, otherwise re-hashed first)
  * VM disks and disk images are always copied (a set breaks if a piece is missing)
  * empty files are always copied; symbolic links are skipped
  * a file identical to another file of the same new backup is skipped, pointing to that copy
  * copies inside "preferred" folders (drive setting, e.g. LLO) are reported first
"""
import ctypes
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

WINDOWS = os.name == "nt"
APP_DIR_NAME = ".backup-adder"
INDEX_NAME = "index.db"
LEGACY_INDEX = "_INDEX/lellx-index.db"     # older LELLX index location, still honoured
LELLX_LABEL = "LELLX"
LELLX_MODEL = "ST6000VX001"
LELLX_MOUNT = "/mnt/lellx"

if WINDOWS:
    STATE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BackupAdder")
else:
    STATE = os.path.expanduser("~/backup-tools/state")
HISTORY = os.path.join(STATE, "history.log")

IMAGE_EXT = re.compile(r"\.(vdi|vmdk|vhdx?|vbox|vbox-prev|sav|nvram|qcow2|vmx|vmsn|vmem|adi|afi|tib|tibx|gho|wim)$", re.I)
OWN_FILES = {"_DUPLICATES_REMOVED.txt", "_DUPLICATES_SKIPPED.txt", "_MANIFEST.sha256", "_ADDED_INFO.txt",
             "_README.txt", "_NOW_FOLDER_MOVED.txt", "RENAMES.txt"}
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", APP_DIR_NAME, "_INDEX", "lost+found",
             ".Trash-1000", ".Trashes", ".Spotlight-V100", ".fseventsd", "$Recycle.Bin", "RECYCLER"}
CHUNK = 4 * 1024 * 1024
SCHEMA = "create table if not exists f(path text primary key, size int, mtime real, phash text, fhash text, err text, ino int)"


class DriveError(Exception):
    pass


def human(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000


def log_history(msg):
    os.makedirs(STATE, exist_ok=True)
    with open(HISTORY, "a", encoding="utf-8") as f:
        f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def lp(path):
    """Long-path safe form on Windows (paths over 260 characters)."""
    if WINDOWS:
        path = os.path.abspath(path)
        if not path.startswith("\\\\?\\"):
            return "\\\\?\\" + path if not path.startswith("\\\\") else "\\\\?\\UNC\\" + path[2:]
    return path


def join(root, rel):
    return os.path.join(root, *rel.split("/"))


def safe_join(root, rel):
    """join() for paths coming from the index: refuse absolute paths, '..' and anything that
    resolves outside root (a tampered or corrupt index must never point elsewhere)."""
    parts = rel.split("/")
    if not rel or rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or ".." in parts or "\\" in rel:
        return None
    full = os.path.join(root, *parts)
    r_root = os.path.realpath(root)
    r_full = os.path.realpath(full)
    try:
        if os.path.commonpath([r_root, r_full]) != r_root:
            return None
    except ValueError:
        return None
    return full


def _hide(path):
    if WINDOWS:
        try:
            ctypes.windll.kernel32.SetFileAttributesW(path, 0x2)
        except Exception:  # noqa: BLE001
            pass


# =============================================================================== drives
@dataclass
class Drive:
    id: str                 # stable id: filesystem UUID / volume serial / folder path hash
    label: str
    root: str               # where it is (or will be) accessible; "" if not mounted yet
    device: str = ""        # Linux block device of the partition (or whole disk for LELLX)
    kind: str = "drive"     # drive | lellx | folder
    size: int = 0
    fstype: str = ""

    @property
    def title(self):
        name = self.label or os.path.basename(self.root.rstrip("/\\")) or self.root or self.device
        extra = f" ({human(self.size)})" if self.size else ""
        where = f" — {self.root}" if self.root else " — not mounted"
        return f"{name}{extra}{where}"


def folder_drive(path):
    path = os.path.abspath(path)
    return Drive(id="folder-" + hashlib.sha1(path.encode()).hexdigest()[:12], label=os.path.basename(path) or path,
                 root=path, kind="folder")


def _priv(cmd):
    r = subprocess.run(["sudo", "-n"] + cmd, capture_output=True, text=True)
    if r.returncode != 0 and "password" in (r.stderr or "").lower():
        r = subprocess.run(["pkexec"] + cmd, capture_output=True, text=True)
    return r


def list_drives():
    """External / secondary drives the user can add backups to (system drive excluded)."""
    return _list_windows() if WINDOWS else _list_linux()


def _list_windows():
    out = []
    k32 = ctypes.windll.kernel32
    sysdrive = os.environ.get("SystemDrive", "C:").upper()
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        letter = f"{chr(65 + i)}:"
        root = letter + "\\"
        dtype = k32.GetDriveTypeW(root)
        if dtype not in (2, 3, 4) or letter.upper() == sysdrive:      # removable, fixed, network
            continue
        name = ctypes.create_unicode_buffer(261)
        fsn = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32()
        if not k32.GetVolumeInformationW(root, name, 261, ctypes.byref(serial), None, None, fsn, 261):
            continue
        try:
            total = shutil.disk_usage(root).total
        except OSError:
            total = 0
        out.append(Drive(id=f"vol-{serial.value:08X}", label=name.value or letter, root=root,
                         kind="drive", size=total, fstype=fsn.value))
    return out


def _lsblk():
    r = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,MODEL,LABEL,UUID,FSTYPE,SIZE,MOUNTPOINTS,TYPE,TRAN,PTTYPE,RM,PARTTYPENAME"],
                       capture_output=True, text=True)
    return json.loads(r.stdout or "{}").get("blockdevices", [])


def _system_mount(mps):
    return any(m in ("/", "/home", "/boot", "/boot/efi", "[SWAP]") or (m or "").startswith(("/snap", "/var", "/usr"))
               for m in mps if m)


def _list_linux():
    out = []
    for d in _lsblk():
        if d.get("type") != "disk" or (d.get("name") or "").startswith(("loop", "zram", "sr")):
            continue
        kids = d.get("children") or []
        if any(_system_mount(c.get("mountpoints") or []) for c in kids):
            continue                                   # internal system disk
        model = d.get("model") or ""
        if LELLX_MODEL in model and not any(c.get("label") == LELLX_LABEL for c in kids):
            # the 4K-enclosure case: no visible partition, needs the loop workaround
            out.append(Drive(id="lellx-" + LELLX_LABEL, label=LELLX_LABEL, root=LELLX_MOUNT if _mounted_at(LELLX_MOUNT) else "",
                             device=d["path"], kind="lellx", size=int(d.get("size") or 0), fstype="ntfs"))
            continue
        for c in kids or ([d] if d.get("fstype") else []):
            if not c.get("fstype") or c.get("fstype") in ("swap", "crypto_LUKS", "LVM2_member"):
                continue
            ptn = (c.get("parttypename") or "").lower()
            if int(c.get("size") or 0) < 2 * 1024 ** 3 or any(k in ptn for k in ("efi", "reserved", "recovery", "bios")):
                continue                               # EFI / recovery / tiny system partitions
            mps = [m for m in (c.get("mountpoints") or []) if m]
            out.append(Drive(id=f"uuid-{c.get('uuid') or c['name']}", label=c.get("label") or model.strip() or c["name"],
                             root=mps[0] if mps else "", device=c["path"], kind="drive",
                             size=int(c.get("size") or 0), fstype=c.get("fstype") or ""))
    return out


def _mounted_at(path):
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if p[1] == path:
                    return "ro" if "ro" in p[3].split(",") else "rw"
    except OSError:
        pass
    return None


def prepare(drive, write=False):
    """Make the drive accessible (and writable if write=True). Returns its root path."""
    if drive.kind == "folder" or WINDOWS:
        if not os.path.isdir(drive.root):
            raise DriveError(f"{drive.root} is not available. Is the drive connected?")
        return drive.root
    if drive.kind == "lellx":
        _lellx_mount("rw" if write else "ro", drive.device)
        drive.root = LELLX_MOUNT
        return drive.root
    if not drive.root:
        r = subprocess.run(["udisksctl", "mount", "-b", drive.device], capture_output=True, text=True)
        if r.returncode != 0:
            raise DriveError("Could not mount the drive: " + (r.stderr or r.stdout).strip())
        m = re.search(r" at (.+?)\.?$", r.stdout.strip())
        drive.root = m.group(1) if m else ""
        if not drive.root:
            for d in _list_linux():
                if d.device == drive.device:
                    drive.root = d.root
    if write and not os.access(drive.root, os.W_OK):
        raise DriveError(f"{drive.root} is mounted read-only or not writable by you.")
    return drive.root


def finish(drive):
    """After writing: put the LELLX drive back to read-only. Other drives stay as they are."""
    if drive.kind == "lellx" and not WINDOWS:
        try:
            _lellx_mount("ro", drive.device)
        except DriveError:
            pass


def eject(drive):
    if drive.kind == "folder":
        return
    if WINDOWS:
        letter = drive.root[:2]
        ps = (f"(New-Object -comObject Shell.Application).Namespace(17).ParseName('{letter}').InvokeVerb('Eject')")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
        return
    subprocess.run(["sync"])
    if drive.kind == "lellx":
        _lellx_unmount(drive.device)
        disk = drive.device
    else:
        if drive.root:
            r = subprocess.run(["udisksctl", "unmount", "-b", drive.device], capture_output=True, text=True)
            if r.returncode != 0:
                raise DriveError("Could not unmount: " + r.stderr.strip() + "\nClose any window using the drive.")
        disk = "/dev/" + subprocess.run(["lsblk", "-no", "PKNAME", drive.device], capture_output=True,
                                        text=True).stdout.strip() if drive.device else ""
    if disk:
        r = subprocess.run(["udisksctl", "power-off", "-b", disk], capture_output=True, text=True)
        if r.returncode != 0:
            _priv(["/usr/bin/udisksctl", "power-off", "-b", disk])
    drive.root = ""


def drive_health(drive):
    """Free space, SMART (Linux, if allowed) and recent USB/power errors."""
    st = {"free": None, "total": None, "smart": None, "warnings": []}
    if drive.root and os.path.isdir(drive.root):
        u = shutil.disk_usage(drive.root)
        st["free"], st["total"] = u.free, u.total
    if WINDOWS or drive.kind == "folder":
        return st
    disk = drive.device if drive.kind == "lellx" else ""
    if drive.device and not disk:
        pk = subprocess.run(["lsblk", "-no", "PKNAME", drive.device], capture_output=True, text=True).stdout.strip()
        disk = "/dev/" + pk if pk else drive.device
    if disk and os.path.exists("/usr/sbin/smartctl"):
        r = subprocess.run(["sudo", "-n", "/usr/sbin/smartctl", "-d", "sat", "-H", "-A", disk], capture_output=True, text=True)
        m = re.search(r"test result: (\S+)", r.stdout or "")
        st["smart"] = m.group(1) if m else None
        for name in ("Reallocated_Sector_Ct", "Current_Pending_Sector"):
            m = re.search(name + r".*\s(\d+)\s*$", r.stdout or "", re.M)
            if m and int(m.group(1)) > 0:
                st["warnings"].append(f"{name} = {m.group(1)}")
    r = subprocess.run(["sudo", "-n", "/usr/bin/dmesg", "-T", "--since", "1 hour ago"], capture_output=True, text=True)
    if any(re.search(r"over-current|DID_ERROR|I/O error, dev (sd|loop)", l) for l in (r.stdout or "").splitlines()):
        st["warnings"].append("recent USB/power errors in the kernel log: check the enclosure power and cable")
    return st


# ---- LELLX loop handling (Linux only)
def _lellx_loops(disk):
    r = subprocess.run(["/usr/sbin/losetup", "-J", "-l"], capture_output=True, text=True)
    out = []
    for l in json.loads(r.stdout or "{}").get("loopdevices", []):
        raw = l.get("back-file") or ""
        bf = raw.replace(" (deleted)", "")
        if bf == disk or (raw.endswith("(deleted)") and re.fullmatch(r"/dev/sd[a-z]+", bf)):
            out.append(l["name"])
    return out


def _lellx_unmount(disk):
    if _mounted_at(LELLX_MOUNT):
        subprocess.run(["sync"])
        r = _priv(["/usr/bin/umount", LELLX_MOUNT])
        if r.returncode != 0:
            raise DriveError("The drive is in use. Close any Files window or program showing it and try again.")
    for loop in _lellx_loops(disk):
        _priv(["/usr/sbin/losetup", "-d", loop])


def _lellx_mount(mode, disk):
    if _mounted_at(LELLX_MOUNT) == mode:
        return
    _lellx_unmount(disk)
    _priv(["/usr/bin/mkdir", "-p", LELLX_MOUNT])
    args = ["/usr/sbin/losetup", "-f", "--show", "-P", "--sector-size", "512"] + (["-r"] if mode == "ro" else [])
    r = _priv(args + [disk])
    if r.returncode != 0:
        raise DriveError("Could not attach the drive: " + r.stderr.strip())
    time.sleep(1)
    opts = f"{mode},uid={os.getuid()},gid={os.getgid()}" + (",big_writes" if mode == "rw" else "")
    r = _priv(["/usr/bin/mount", "-t", "ntfs-3g", "-o", opts, r.stdout.strip() + "p1", LELLX_MOUNT])
    if r.returncode != 0:
        raise DriveError("Could not mount the drive: " + r.stderr.strip())


# =============================================================================== index
class Index:
    """Per-drive index. Working copy lives on the computer; save() publishes to drive + cache."""

    def __init__(self, drive):
        self.drive = drive
        self.cache_dir = os.path.join(STATE, "drives", re.sub(r"[^\w.-]", "_", drive.id))
        os.makedirs(self.cache_dir, exist_ok=True)
        self.cache = os.path.join(self.cache_dir, INDEX_NAME)
        self.work = os.path.join(self.cache_dir, "work.db")
        self._migrate_legacy()
        src = self._newest_source()
        if src and (not os.path.exists(self.work) or os.path.getmtime(self.work) < os.path.getmtime(src)):
            shutil.copyfile(src, self.work)
        self.db = sqlite3.connect(self.work, check_same_thread=False)
        self.lock = threading.RLock()
        self.db.execute(SCHEMA)
        self.db.execute("create table if not exists info(k text primary key, v text)")
        for sql in ("create index if not exists i_size on f(size)", "create index if not exists i_sf on f(size, fhash)"):
            self.db.execute(sql)
        if self.info("built") is None and self.stats()[0]:
            self.info("built", "imported " + time.strftime("%Y-%m-%d %H:%M"))
        self.db.commit()

    def _migrate_legacy(self):
        """Use the old LELLX index locations if this drive has no new-style index yet."""
        if os.path.exists(self.cache):
            return
        legacy_local = os.path.expanduser("~/backup-tools/state/index.db")
        if self.drive.kind == "lellx" and os.path.exists(legacy_local):
            shutil.copyfile(legacy_local, self.cache)

    def _newest_source(self):
        cands = []
        root = self.drive.root
        for p in ([join(root, APP_DIR_NAME + "/" + INDEX_NAME), join(root, LEGACY_INDEX)] if root else []) + [self.cache]:
            if os.path.exists(p):
                cands.append((os.path.getmtime(p), p))
        return max(cands)[1] if cands else None

    @property
    def empty(self):
        return self.stats()[0] == 0 and self.info("built") is None

    def info(self, k, v=None):
        with self.lock:
            if v is None:
                r = self.db.execute("select v from info where k=?", (k,)).fetchone()
                return r[0] if r else None
            self.db.execute("insert or replace into info values(?,?)", (k, str(v)))

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

    def search(self, text, limit=500):
        with self.lock:
            return self.db.execute("select path, size from f where path like ? order by path limit ?",
                                   ("%" + text + "%", limit)).fetchall()

    def stats(self):
        with self.lock:
            return self.db.execute("select count(*), coalesce(sum(size),0), count(fhash) from f").fetchone()

    def commit(self):
        with self.lock:
            self.db.commit()

    def save(self, to_drive=True):
        with self.lock:
            self.db.commit()
            tmp = self.cache + ".tmp"
            if os.path.exists(tmp):
                os.remove(tmp)
            self.db.execute("vacuum into ?", (tmp,))
        os.replace(tmp, self.cache)
        if to_drive and self.drive.root:
            d = join(self.drive.root, APP_DIR_NAME)
            try:
                os.makedirs(d, exist_ok=True)
                _hide(d)
                shutil.copyfile(self.cache, os.path.join(d, INDEX_NAME + ".tmp"))
                os.replace(os.path.join(d, INDEX_NAME + ".tmp"), os.path.join(d, INDEX_NAME))
                with open(os.path.join(d, "README.txt"), "w", encoding="utf-8") as f:
                    f.write("Backup Adder index of every file on this drive (path, size, sha256).\n"
                            "Lets new backups be added without duplicate files. Safe to delete:\n"
                            "it is rebuilt automatically (slower first run). Updated "
                            + time.strftime("%Y-%m-%d %H:%M") + ".\n")
                settings = os.path.join(d, "settings.json")
                if not os.path.exists(settings):
                    with open(settings, "w", encoding="utf-8") as f:
                        json.dump({"prefer": self.prefer()}, f, indent=2)
            except OSError:
                pass   # read-only drive: cache on the computer is still saved

    def prefer(self):
        """Folders whose copies are preferred as 'the existing copy' (drive setting)."""
        if self.drive.root:
            p = join(self.drive.root, APP_DIR_NAME + "/settings.json")
            try:
                with open(p, encoding="utf-8") as f:
                    return list(json.load(f).get("prefer", []))
            except (OSError, ValueError):
                pass
            if os.path.isdir(join(self.drive.root, "LLO")):
                return ["LLO"]
        return []


def scan(index, status=None, cancel=None):
    """Build or refresh the index from the drive's folder tree. Lists names/sizes only; keeps hashes
    of unchanged files. First run on a big drive takes minutes to tens of minutes."""
    root = index.drive.root
    first = index.empty
    seen = set()
    n = added = changed = 0
    batch = []
    t0 = time.time()
    with index.lock:
        known = {p: (s, m) for p, s, m in index.db.execute("select path, size, mtime from f")}
    for dp, dns, fns in os.walk(lp(root) if WINDOWS else root):
        rel_dp = os.path.relpath(dp, lp(root) if WINDOWS else root)
        rel_dp = "" if rel_dp == "." else rel_dp.replace(os.sep, "/")
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not (rel_dp == "" and d.startswith("."))]
        for fn in fns:
            if cancel and cancel.is_set():
                raise InterruptedError
            if fn in OWN_FILES:
                continue
            rel = fn if not rel_dp else rel_dp + "/" + fn
            try:
                st = os.stat(os.path.join(dp, fn), follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(os.path.join(dp, fn)) or os.path.islink(os.path.join(dp, fn)):
                continue
            seen.add(rel)
            old = known.get(rel)
            if old is None:
                batch.append(("ins", rel, st.st_size, st.st_mtime))
                added += 1
            elif old[0] != st.st_size or (old[1] and abs(old[1] - st.st_mtime) > 2):
                batch.append(("upd", rel, st.st_size, st.st_mtime))
                changed += 1
            n += 1
            if len(batch) >= 5000:
                _flush(index, batch)
            if status and n % 2000 == 0:
                status(("Building the index for this drive (first time only)… " if first else "Updating index… ")
                       + f"{n:,} files ({int(time.time() - t0)} s)")
    _flush(index, batch)
    missing = [p for p in known if p not in seen]
    with index.lock:
        index.db.executemany("delete from f where path=?", [(p,) for p in missing])
        index.info("built", time.strftime("%Y-%m-%d %H:%M"))
        index.db.commit()
    log_history(f"SCAN {index.drive.label}: files={n} new={added} changed={changed} removed={len(missing)}")
    return {"files": n, "added": added, "changed": changed, "removed": len(missing), "first": first}


def _flush(index, batch):
    with index.lock:
        for op, rel, size, mtime in batch:
            if op == "ins":
                index.db.execute("insert or replace into f(path,size,mtime) values(?,?,?)", (rel, size, mtime))
            else:
                index.db.execute("update f set size=?, mtime=?, phash=NULL, fhash=NULL where path=?", (size, mtime, rel))
        index.db.commit()
    batch.clear()


# =============================================================================== hashing / analysis
def sha256_file(path, progress=None, cancel=None):
    h = hashlib.sha256()
    with open(lp(path), "rb") as f:
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


@dataclass
class Item:
    rel: str
    size: int
    action: str = "COPY"      # COPY | SKIP | ERROR
    reason: str = ""
    existing: str = ""
    sha: str = ""
    mtime: float = 0.0


@dataclass
class Plan:
    source: str
    dest_rel: str
    items: list = field(default_factory=list)
    copied: bool = False
    match: str = "content"

    def totals(self):
        t = {"COPY": [0, 0], "SKIP": [0, 0], "ERROR": [0, 0]}
        for i in self.items:
            t[i.action][0] += 1
            t[i.action][1] += i.size
        return t


def make_dest(parent, name, date):
    name = re.sub(r"[\\/:*?\"<>|]", "_", (name or "").strip()).strip(". ")
    if not name:
        raise ValueError("Please enter a name for the backup.")
    if date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError("Date must look like 2027-03-01 (or be empty for 'no date').")
    folder = f"{name} - {date}" if date else f"{name} - no date"
    parent = (parent or "").strip().strip("/\\").replace("\\", "/")
    return f"{parent}/{folder}" if parent else folder


def suggest_parents(root, depth=2, limit=200):
    """Existing folders on the drive (to pick where a backup goes)."""
    out = []
    for dp, dns, _ in os.walk(root):
        rel = os.path.relpath(dp, root)
        lvl = 0 if rel == "." else rel.count(os.sep) + 1
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith((".", "$")))
        if lvl >= depth:
            dns[:] = []
        if rel != ".":
            out.append(rel.replace(os.sep, "/"))
        if len(out) >= limit:
            break
    return out


MATCH_MODES = {
    "content": "Identical content (byte-identical, sha256)",
    "name+size": "Same file name and size",
    "name": "Same file name only",
}


def _name_map(index):
    """file name (case-insensitive) -> [(path, size)] for every file in the index."""
    names = {}
    with index.lock:
        for path, size in index.db.execute("select path, size from f"):
            names.setdefault(path.rsplit("/", 1)[-1].casefold(), []).append((path, size))
    return names


def _find_by_name(index, root, rel, size, names, prefer, with_size):
    cands = names.get(rel.rsplit("/", 1)[-1].casefold(), [])
    if with_size:
        cands = [c for c in cands if c[1] == size]

    def key(c):
        return (not any(c[0] == p or c[0].startswith(p + "/") for p in prefer), c[0])
    for path, isize in sorted(cands, key=key):
        full = safe_join(root, path)
        if full is None:
            continue
        try:
            st = os.stat(lp(full))
        except OSError:
            continue                  # gone from the drive since the index was made
        if with_size and st.st_size != size:
            continue
        return path, st.st_size
    return None


def analyze(src, dest_rel, index, status=None, progress=None, cancel=None, rescan=False, match="content"):
    """Decide COPY/SKIP for every file of src. Builds the drive index first if there is none.

    match: "content" = skip only byte-identical files (default);
           "name+size" = skip if a file with the same name and size is on the drive;
           "name" = skip if a file with the same name is on the drive (content not compared).
    """
    if match not in MATCH_MODES:
        raise ValueError(f"unknown compare mode: {match}")
    root = index.drive.root
    if index.empty or rescan:
        scan(index, status, cancel)
    src = os.path.abspath(src)
    plan = Plan(source=src, dest_rel=dest_rel, match=match)
    if os.path.exists(join(root, dest_rel)):
        raise ValueError(f"A folder with this name already exists on the drive:\n{dest_rel}\nChoose another name or date.")
    r_root, r_src = os.path.realpath(root), os.path.realpath(src)
    try:
        inside = os.path.commonpath([r_src, r_root]) == r_root
    except ValueError:            # different drive letters on Windows
        inside = False
    if inside:
        raise ValueError("The source is on the destination drive itself. Choose a folder or file on another disk.")
    if status:
        status("Listing files…")
    files = []
    if os.path.isfile(src):
        files.append((os.path.basename(src), src))
    else:
        for dp, dns, fns in os.walk(lp(src) if WINDOWS else src):
            dns.sort()
            for fn in sorted(fns):
                full = os.path.join(dp, fn)
                files.append((os.path.relpath(full, lp(src) if WINDOWS else src).replace(os.sep, "/"), full))
            if cancel and cancel.is_set():
                raise InterruptedError
    total = 0
    for rel, full in files:
        try:
            if os.path.islink(full):
                plan.items.append(Item(rel, 0, "SKIP", "symbolic link (not copied)"))
                continue
            st = os.stat(full)
            plan.items.append(Item(rel, st.st_size, mtime=st.st_mtime))
            total += st.st_size
        except OSError as e:
            plan.items.append(Item(rel, 0, "ERROR", f"cannot read: {e.strerror}"))
    done = [0]

    def prog(n):
        done[0] += n
        if progress:
            progress(done[0], total)

    prefer = index.prefer()
    names = None
    if match != "content":
        if status:
            status("Loading file names of the drive…")
        names = _name_map(index)
    seen, seen_sizes = {}, set()
    for n, it in enumerate(plan.items):
        if cancel and cancel.is_set():
            raise InterruptedError
        if it.action != "COPY":
            continue
        full = src if os.path.isfile(src) else join(src, it.rel)
        if status and n % 50 == 0:
            status(f"Checking {n + 1:,} / {len(plan.items):,}: {it.rel}")
        try:
            if it.size == 0:
                it.reason = "empty file (always copied)"
                continue
            if IMAGE_EXT.search(it.rel):
                it.reason = "VM / disk-image file (never deduplicated)"
                it.sha = sha256_file(full, prog, cancel)
                continue
            if names is not None:
                hit = _find_by_name(index, root, it.rel, it.size, names, prefer, match == "name+size")
                if hit:
                    path, dsize = hit
                    it.action, it.existing = "SKIP", path
                    it.reason = ("same name and size already on the drive (content not compared)"
                                 if match == "name+size" else
                                 "same name already on the drive (content not compared"
                                 + (", same size)" if dsize == it.size else f", size differs: {human(dsize)} there)"))
                    prog(it.size)
                    continue
                it.sha = sha256_file(full, prog, cancel)
                if (it.size, it.sha) in seen:
                    it.action, it.reason = "SKIP", "identical to another file in this same backup"
                    it.existing = dest_rel + "/" + seen[(it.size, it.sha)]
                else:
                    it.reason = "new: no file with this name" + (" and size" if match == "name+size" else "") + " on the drive"
                    seen[(it.size, it.sha)] = it.rel
                continue
            cands = index.by_size(it.size)
            if not cands and it.size not in seen_sizes:
                it.reason = "new: no file of this size on the drive"
                it.sha = sha256_file(full, prog, cancel)
                seen[(it.size, it.sha)] = it.rel
                seen_sizes.add(it.size)
                continue
            it.sha = sha256_file(full, prog, cancel)
        except OSError as e:
            it.action, it.reason = "ERROR", f"cannot read: {e.strerror}"
            continue
        if (it.size, it.sha) in seen:
            it.action, it.reason = "SKIP", "identical to another file in this same backup"
            it.existing = dest_rel + "/" + seen[(it.size, it.sha)]
            continue
        match = _find_match(index, root, it.size, it.sha, cands, prefer, cancel)
        if match:
            same_name = match.rsplit("/", 1)[-1] == it.rel.rsplit("/", 1)[-1]
            it.action, it.existing = "SKIP", match
            it.reason = "identical file already on the drive" + ("" if same_name else " (under another name)")
        else:
            it.reason = "new: different content from same-size files on the drive"
            seen[(it.size, it.sha)] = it.rel
            seen_sizes.add(it.size)
    index.commit()
    if status:
        status("Analysis done.")
    return plan


def _find_match(index, root, size, sha, cands, prefer, cancel):
    def key(c):
        pref = not any(c[0] == p or c[0].startswith(p + "/") for p in prefer)
        return (c[2] is None, pref, c[0])
    for path, mtime, fh in sorted(cands, key=key):
        if cancel and cancel.is_set():
            raise InterruptedError
        full = safe_join(root, path)
        if full is None:
            index.drop(path)          # invalid / out-of-drive path in the index
            continue
        try:
            st = os.stat(lp(full))
        except OSError:
            index.drop(path)
            continue
        if st.st_size != size:
            index.drop(path)
            continue
        if fh is None or (mtime and abs(st.st_mtime - mtime) > 2):
            try:
                fh = sha256_file(full, None, cancel)
            except OSError:
                continue
            index.set_hash(path, fh)
        if fh == sha:
            return path
    return None


# =============================================================================== copy
def execute(plan, index, delete_source=False, status=None, progress=None, cancel=None):
    """Copy COPY items with sha256 read-back verification, write notes, update + save index."""
    root = index.drive.root
    dest = join(root, plan.dest_rel)
    os.makedirs(lp(dest), exist_ok=False)
    items = [i for i in plan.items if i.action == "COPY"]
    total = sum(i.size for i in items) * 2 or 1
    done = [0]

    def prog(n):
        done[0] += n
        if progress:
            progress(done[0], total)

    single = os.path.isfile(plan.source)
    failures, manifest = [], []
    for k, it in enumerate(items):
        if cancel and cancel.is_set():
            break
        src = plan.source if single else join(plan.source, it.rel)
        dst = join(dest, it.rel)
        if status:
            status(f"Copying {k + 1:,} / {len(items):,}: {it.rel}")
        ok = False
        for _attempt in (1, 2):
            try:
                os.makedirs(lp(os.path.dirname(dst)), exist_ok=True)
                h = hashlib.sha256()
                with open(lp(src), "rb") as fi, open(lp(dst), "wb") as fo:
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
                if sha256_file(dst, prog) == src_sha and (not it.sha or it.sha == src_sha):
                    it.sha, ok = src_sha, True
                    try:
                        os.utime(lp(dst), (it.mtime, it.mtime))
                    except OSError:
                        pass
                    break
            except OSError as e:
                it.reason += f" | copy error: {e.strerror}"
        if ok:
            manifest.append(f"{it.sha}  ./{it.rel}")
            index.add(plan.dest_rel + "/" + it.rel, it.size, it.mtime, it.sha)
        else:
            it.action, it.reason = "ERROR", "copy failed verification"
            failures.append(it)
    cancelled = bool(cancel and cancel.is_set())
    with open(lp(os.path.join(dest, "_MANIFEST.sha256")), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(manifest) + ("\n" if manifest else ""))
    with open(lp(os.path.join(dest, "_DUPLICATES_SKIPPED.txt")), "w", encoding="utf-8") as f:
        f.write(f"Files of this backup that were NOT copied ({time.strftime('%Y-%m-%d')}), with the reason.\n"
                "Identical files already on the drive are listed with '=>' and the location of the existing copy.\n\n")
        for i in plan.items:
            if i.action == "SKIP":
                f.write(f"{i.rel}\t=>\t{i.existing or '-'}\t({i.reason})\n")
    t = plan.totals()
    with open(lp(os.path.join(dest, "_ADDED_INFO.txt")), "w", encoding="utf-8") as f:
        f.write(f"Added with Backup Adder on {time.strftime('%Y-%m-%d %H:%M')}\n"
                f"Source: {plan.source}\n"
                f"Compared by: {MATCH_MODES[plan.match]}\n"
                f"Copied: {t['COPY'][0]} files ({human(t['COPY'][1])}), verified by sha256 read-back\n"
                f"Skipped: {t['SKIP'][0]} files ({human(t['SKIP'][1])}) - see _DUPLICATES_SKIPPED.txt\n"
                f"Errors: {len(failures) + t['ERROR'][0]}\n" + ("CANCELLED before finishing.\n" if cancelled else ""))
    if status:
        status("Saving index…")
    index.save(to_drive=True)
    deleted = 0
    kept = 0
    if delete_source and not cancelled and not failures:
        if status:
            status("Re-checking copies before deleting originals…")
        for it in plan.items:
            if it.action not in ("COPY", "SKIP") or it.reason.startswith("symbolic link"):
                continue
            src_path = plan.source if single else join(plan.source, it.rel)
            # the copy on the drive that makes deleting this original safe
            if it.action == "COPY":
                other = join(dest, it.rel)
            elif it.existing.startswith(plan.dest_rel + "/"):          # twin inside this same backup
                other = join(dest, it.existing[len(plan.dest_rel) + 1:])
            else:
                other = safe_join(root, it.existing)
            try:
                if (other is None or not os.path.exists(lp(other)) or os.path.samefile(lp(src_path), lp(other))
                        or (it.size and (not it.sha or sha256_file(other) != it.sha))):
                    # not byte-identical (or only matched by name): the original is kept
                    kept += 1
                    continue
                os.remove(lp(src_path))
                deleted += 1
            except OSError:
                kept += 1
        if not single:
            for dp, _, _ in os.walk(plan.source, topdown=False):
                try:
                    os.rmdir(dp)
                except OSError:
                    pass
    plan.copied = True
    log_history(f"ADD {plan.source} -> [{index.drive.label}] {plan.dest_rel}: copied {t['COPY'][0]} "
                f"({human(t['COPY'][1])}), skipped {t['SKIP'][0]} ({human(t['SKIP'][1])}), failed {len(failures)}, "
                f"originals deleted {deleted} kept {kept}{' CANCELLED' if cancelled else ''}")
    return {"failures": failures, "cancelled": cancelled, "deleted": deleted, "kept": kept}


def export_report(plan, path):
    with open(path, "w", encoding="utf-8") as f:
        f.write("action\tsize\tfile in backup\treason\texisting copy on drive\n")
        for i in plan.items:
            f.write(f"{i.action}\t{i.size}\t{i.rel}\t{i.reason}\t{i.existing}\n")


def open_in_file_manager(path):
    if WINDOWS:
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", path])
