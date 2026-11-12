# Backup Adder

Add new backups to **any drive** (or folder / network share) **without copying files that are already on it**.
Every file of the new backup is listed as **Copy** or **Skip**, with the reason, and for skipped files the
location of the identical copy that is already on the drive. Nothing is written until you confirm.

| | Linux | Windows |
|---|---|---|
| App | **Backup Adder** (GTK4 / libadwaita), app menu | **BackupAdder.exe** (Tk) — see [WINDOWS.md](WINDOWS.md) |
| Command line | `lellx-add` | `backup-adder-cli.exe` |
| Also runs | `backup_adder_tk.py` (same Tk app) | `py backup_adder_tk.py` from source |

---

## Contents
1. [How it works](#1-how-it-works)
2. [Install (Linux)](#2-install-linux)
3. [Connecting a drive](#3-connecting-a-drive)
4. [Adding a backup with the app](#4-adding-a-backup-with-the-app)
5. [Other features](#5-other-features)
6. [Command line](#6-command-line)
7. [Dedup rules](#7-dedup-rules)
8. [Files the tool writes](#8-files-the-tool-writes)
9. [The index](#9-the-index)
10. [The LELLX drive (special case)](#10-the-lellx-drive-special-case)
11. [Troubleshooting](#11-troubleshooting)
12. [Verifying and recovering](#12-verifying-and-recovering)
13. [Development](#13-development)
14. [Uninstall](#14-uninstall)

---

## 1. How it works
- Each drive gets an **index**: a list of every file with its size, and its sha256 once known.
- **First use of a drive**: the index is built automatically by listing the drive (names and sizes only —
  no file contents are read), so it takes minutes, not hours, even on large drives.
- **Analyze**: each file of the new backup is hashed. Only if the drive has a file of the **same size** is that
  drive file hashed too (once — the hash is remembered). Identical content ⇒ **Skip**; otherwise **Copy**.
- **Copy**: copies, reads back, verifies sha256, writes notes, updates the index on the drive and on the computer.
  Each run makes the index more complete, so later runs get faster.

## 2. Install (Linux)
Requirements: Python 3.11+, `python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1` (libadwaita ≥ 1.5), `udisks2`
(`udisksctl`), `util-linux`; optional `ntfs-3g` + `smartmontools` + passwordless `sudo` or `pkexec`
(only for the LELLX special case and the SMART health line).
```bash
git clone https://github.com/LELL0/backup-adder.git ~/backup-tools
cd ~/backup-tools && ./install.sh
```
`install.sh` adds **Backup Adder** to the app menu, links `lellx-add` into `~/.local/bin` and creates `state/`.

## 3. Connecting a drive
1. Plug the drive in (for desktop drives: power the enclosure on first, wait for spin-up, then USB).
2. Open Backup Adder. The **Drive** list shows external / secondary drives (system disks and EFI/recovery
   partitions are hidden). Press ⟳ after plugging something in.
3. Select the drive. It is mounted automatically if needed (`udisksctl`, no password for normal drives).
4. To use any folder (network share, folder on another disk): choose **Other folder…**.

The status line shows mount point, free space, SMART health (if available), the index state, and warnings
such as recent USB/power errors.

## 4. Adding a backup with the app
1. **Source**: **Folder…** (or **File…**) → what you want to add.
2. **Goes into folder (on the drive)**: type a folder or pick an existing one with the 📁 button,
   e.g. `BACKUPS/Phones`. Leave empty for the drive root.
3. **Name** (device / owner) and **Backup date** (`YYYY-MM-DD`, empty = "no date").
   **Will be saved as** shows the final folder, e.g. `/media/me/BIGDISK/BACKUPS/Phones/Samsung A30 - 2027-03-01`.
4. Optional **Rescan the drive first**: only if files were added/removed on the drive outside this app.
   **Compare files by** (default *Identical content*): see [Dedup rules](#7-dedup-rules) for the name modes.
5. **Analyze**. The first time on a drive, its index is built first (progress shown in the status line).
   The table shows:

   | Column | Meaning |
   |---|---|
   | Action | **Copy** (green), **Skip** (orange), **Error** (red) |
   | File in new backup | path inside the source |
   | Size | file size |
   | Reason | why it is copied or skipped |
   | Already on drive at | for skipped files: where the identical copy is |

   Filter with the dropdown (All / Will copy / Skipped / Errors) and the search box.
   **Double-click** a skipped file to open the folder of its existing copy. **Export report…** saves the table
   as TSV. Nothing has been written yet.
6. Optional **Delete originals after verified copy**: after everything is copied and verified, the source files
   are deleted — but only if there were no errors, and each original is kept if its copy on the drive cannot be
   re-confirmed (exists, is a different file, same sha256). Asks for confirmation.
7. **Copy to drive** → confirm. Don't unplug the drive or its power until it finishes. **Cancel** is possible;
   files copied so far are verified and listed in `_MANIFEST.sha256`.
8. **Menu → Safely eject drive** before unplugging.

## 5. Other features
- **Find on drive** tab: search the selected drive's index by file/folder name; double-click to open the folder.
- **History** tab: every add and scan, with counts (`state/history.log`).
- **Menu**: *Open drive in Files*, *Rescan drive index* (sync the index with changes made outside the app),
  *Safely eject drive* (unmount + power off).

## 6. Command line
```bash
lellx-add --list                                             # drives found
# dry run (nothing written):
lellx-add ~/phone-dump --drive LELLX --into BACKUPS/Phones --name "Samsung A30" --date 2027-03-01
# copy for real (optionally delete originals after verification):
lellx-add ~/phone-dump --drive LELLX --into BACKUPS/Phones --name "Samsung A30" --date 2027-03-01 --apply
lellx-add ... --apply --delete-source
lellx-add ~/stuff --folder /mnt/nas/backups --name Stuff --apply      # any folder as target
lellx-add ... --match name          # or name+size: compare by file name instead of content
lellx-add ... --report plan.tsv --quiet                              # TSV report, summary only
lellx-add --drive LELLX --find IMG_0597                              # where is it?
lellx-add --drive LELLX --status | --rescan | --eject
```
`--drive` matches label, id, mount point or device (substring, case-insensitive). With only one drive
connected it can be omitted.

## 7. Dedup rules
**Compare files by** (app option, CLI `--match`):

| Mode | A file is skipped when the drive already has… | Speed |
|---|---|---|
| **Identical content** (default, `content`) | a byte-identical file (same size + sha256), any name | reads files of equal size |
| **Same file name and size** (`name+size`) | a file with the same name and the same size, anywhere | fast, only lists names |
| **Same file name only** (`name`) | a file with the same name, anywhere (the reason shows if the size differs) | fast, only lists names |

Name matching ignores upper/lower case and the folder. **Name modes don't compare content**: a different file that
happens to have the same name (`IMG_0001.jpg`, `notes.txt`…) is skipped too, so check the Skip list. The mode used
is written in `_ADDED_INFO.txt`. With *Delete originals*, an original skipped by a name match is **never deleted**
(only originals whose copy is confirmed byte-identical are). VM/disk images and empty files are always copied in
every mode.

Rules for the default **Identical content** mode:
- **Skip** only if a **byte-identical** file (same size and full sha256) is already on the drive and still
  exists unchanged (size/date as in the index; otherwise it is re-hashed first). Names don't matter: the reason
  says "(under another name)" when the existing copy is named differently.
- Copies inside the drive's **preferred folders** are reported first (`.backup-adder/settings.json`,
  `{"prefer": ["LLO"]}`; set automatically to `LLO` if that folder exists).
- **VM disks and disk images are always copied** (`.vdi .vmdk .vhd .vhdx .vbox .sav .qcow2 .vmx .adi .afi .tib
  .tibx .gho .wim …`): a VM/image set breaks if a piece is missing.
- **Empty files are always copied**; **symbolic links are skipped**; unreadable files are shown as **Error**.
- A file **identical to another file of the same new backup** is skipped, pointing to that copy.
- The destination folder must not exist yet (nothing is ever overwritten). A source on the destination drive
  itself is refused.

## 8. Files the tool writes
In every new backup folder:

| File | Content |
|---|---|
| `_MANIFEST.sha256` | sha256 of every copied file (`sha256sum -c` compatible) |
| `_DUPLICATES_SKIPPED.txt` | each skipped file `=>` location of the existing copy, with the reason |
| `_ADDED_INFO.txt` | date, source path, counts, sizes, errors |

On each drive: hidden folder `.backup-adder/` (`index.db`, `settings.json`, `README.txt`).
On the computer: `~/backup-tools/state/drives/<drive-id>/` (Linux) or `%LOCALAPPDATA%\BackupAdder\drives\`
(Windows), plus `history.log`.

## 9. The index
- SQLite, table `f(path, size, mtime, phash, fhash, err, ino)`; paths relative to the drive root with `/`.
- Stored on the drive (`.backup-adder/index.db`) **and** cached on the computer; the newer one is used.
  So the drive carries its own index to any computer, and a read-only drive still works (cache only).
- Built automatically on first use; updated after every copy; *Rescan* syncs it with outside changes.
- Safe to delete: it is rebuilt (first run slower again).
- Inspect: `sqlite3 ~/backup-tools/state/drives/<id>/index.db "select count(*), sum(size), count(fhash) from f"`.
- Paths in the index are never trusted blindly: absolute paths, `..` or anything resolving outside the drive is
  dropped.

## 10. The LELLX drive (special case)
The 6 TB **LELLX** disk sits in a USB enclosure that reports 4096-byte sectors while the disk was partitioned
with 512-byte sectors, so Linux (and Windows) see no partition by themselves. On Linux the app detects it
(model `ST6000VX001`) and attaches it through a loop device (`losetup --sector-size 512`), mounted
**read-only** at `/mnt/lellx` and switched to read-write only while copying (needs `sudo`/`pkexec`).
Its older index (`/_INDEX/lellx-index.db`, `state/index.db`) is imported automatically.

Manual commands:
```bash
L=$(sudo losetup -f --show -r -P --sector-size 512 /dev/sda)          # read-only
sudo mkdir -p /mnt/lellx && sudo mount -t ntfs-3g -o ro,uid=$(id -u),gid=$(id -g) ${L}p1 /mnt/lellx
sudo umount /mnt/lellx && sudo losetup -d $L && udisksctl power-off -b /dev/sda
```
The kernel `ntfs3` driver can't mount it (`Failed to load $BadClus (-22)`); `ntfs-3g` works.
Folder layout: `BACKUPS/{Laptops,Phones,SD cards & USBs,Collections (mixed, undated),Old drives,Projects}/
<Name> - YYYY-MM-DD`, `LLO/` (organized library), `BACKUPS/RENAMES.txt` (2026-11-12 reorganization),
`_DUPLICATES_REMOVED.txt` in folders cleaned that day.

## 11. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| Drive not in the list | Press ⟳. Desktop drives: power first, wait for spin-up, then USB. Check `lsblk`. |
| `over-current condition` in the kernel log | The enclosure draws power over USB: its power adapter is off/loose/failing. Unplug USB, fix power. |
| `DID_ERROR` / `I/O error` / drive disappears | USB link or power dropped. Reconnect (power first). Redo the copy into a new folder name if it was interrupted. |
| "in use" / cannot switch / unmount | Close Files windows or terminals inside the drive; the nightly `updatedb` may be crawling it (add the mount point to `PRUNEPATHS` in `/etc/updatedb.conf`). |
| "mounted read-only or not writable" | The filesystem is read-only (e.g. NTFS with hibernation/fast-startup flag from Windows, or a full/failing disk). Fix on the drive's side, or choose another target. |
| Files you know are on the drive are marked Copy | They were added outside the app: tick **Rescan the drive first** (or Menu → Rescan). |
| Password window | Only for LELLX mounting / SMART: `pkexec` asks when passwordless `sudo` isn't available. |

## 12. Verifying and recovering
- Verify a backup folder: `cd "<drive>/BACKUPS/Phones/Samsung A30 - 2027-03-01" && sha256sum -c _MANIFEST.sha256`
  (Windows: compare with `certutil -hashfile <file> SHA256`).
- Where is a skipped file? `_DUPLICATES_SKIPPED.txt` in the backup folder, or *Find on drive*.
- Interrupted copy: the folder holds only verified files + notes; delete it and add the backup again.

## 13. Development
```
backup_core.py      core: drives, index, analysis, copy (Linux + Windows)
backup_adder.py     GTK4 app (Linux)
backup_adder_tk.py  Tk app (Windows; also runs on Linux)
lellx-add           CLI
tests/test_core.py  unit tests (use folders as fake drives): python3 -m unittest discover -s tests
.github/workflows/build.yml   tests on Linux + Windows, builds the Windows .exe (artifact; release on v* tags)
```

## 14. Uninstall
```bash
rm ~/.local/share/applications/lellx-backup-adder.desktop ~/.local/bin/lellx-add
rm -r ~/backup-tools      # removes the computer-side cache; drives keep their .backup-adder/ index
```
