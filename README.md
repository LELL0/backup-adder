# Backup Adder

Add new backups to the **LELLX 6 TB backup drive** without copying files that are already on it.
Every file of the new backup is listed as **Copy** or **Skip**, with the reason, and for skipped files the
location of the identical copy that is already on the drive. Nothing is written until you confirm.

- GUI: **Backup Adder** (GTK4 / libadwaita) in the GNOME app menu
- CLI: `lellx-add`

---

## Contents
1. [Requirements](#1-requirements)
2. [Install](#2-install)
3. [Connecting the drive](#3-connecting-the-drive)
4. [Adding a backup with the app](#4-adding-a-backup-with-the-app)
5. [Other features](#5-other-features)
6. [Command line](#6-command-line)
7. [Dedup rules](#7-dedup-rules)
8. [Files the tool writes](#8-files-the-tool-writes)
9. [The index](#9-the-index)
10. [Manual mount / unmount](#10-manual-mount--unmount)
11. [Troubleshooting](#11-troubleshooting)
12. [Verifying and recovering](#12-verifying-and-recovering)
13. [Drive layout](#13-drive-layout)
14. [Uninstall](#14-uninstall)

---

## 1. Requirements
- Linux with GNOME (tested on Debian 13, GNOME 48)
- Python 3.11+ with PyGObject, GTK 4 and libadwaita ≥ 1.5
  (`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`)
- `ntfs-3g`, `util-linux` (`losetup`, `lsblk`, `mount`), `psmisc` (`fuser`), `udisks2` (`udisksctl`)
- optional: `smartmontools` (`smartctl`) for the health line
- root access through `sudo` (passwordless) or `pkexec` (a password window appears)

## 2. Install
```bash
git clone https://github.com/LELL0/backup-adder.git ~/backup-tools
cd ~/backup-tools && ./install.sh
```
`install.sh` creates the app-menu entry (`~/.local/share/applications/lellx-backup-adder.desktop`),
links `lellx-add` into `~/.local/bin`, and creates `state/`.

**The index is not in this repository** (it lists every file on the drive). After cloning, either:
- copy an existing index to `state/index.db`, or
- connect the drive: the app uses the copy stored on the drive at `/_INDEX/lellx-index.db`, or
- run **Menu → Rescan drive index** to build one from scratch (slow, files are hashed lazily when needed).

## 3. Connecting the drive
1. Plug the enclosure's **power adapter** in and switch it on. Wait until the disk spins.
2. Only then plug in the USB cable (directly into the laptop, no hub).
3. Open Backup Adder → press **Connect**. The drive is mounted **read-only** at `/mnt/lellx`.

> The enclosure reports 4096-byte sectors but the disk was partitioned with 512-byte sectors, so Linux sees
> no partition by itself. The app attaches it through a loop device with `--sector-size 512`
> (see [§10](#10-manual-mount--unmount)). It is only switched to read-write while copying, then back.

The drive also appears in the Files sidebar as **6TB Backup (LELLX)** once mounted.

## 4. Adding a backup with the app
1. **Source**: press **Folder…** (or **File…** for a single file) and choose what to add.
2. **Goes into**: Laptops, Phones, SD cards & USBs, Collections (mixed, undated), Old drives, Projects, or LLO.
3. **Name**: device or owner, e.g. `Samsung A30`, `Dell Laptop`.
4. **Backup date**: `YYYY-MM-DD` (today by default; empty = "no date").
   The row **Will be saved as** shows the final folder, e.g. `BACKUPS/Phones/Samsung A30 - 2027-03-01`.
5. Press **Analyze**. Every file is hashed (sha256) and compared with the drive. The table shows:

   | Column | Meaning |
   |---|---|
   | Action | **Copy** (green), **Skip** (orange), **Error** (red) |
   | File in new backup | path inside the source |
   | Size | file size |
   | Reason | why it is copied or skipped |
   | Already on drive at | for skipped files: where the identical copy is |

   - Use the dropdown (All / Will copy / Skipped / Errors) and the filter box to narrow the list.
   - **Double-click** a skipped file to open the folder of its existing copy.
   - **Export report…** saves the whole table as a TSV file.
   - Nothing has been written yet.
6. Optional: tick **Delete originals after verified copy** (removes the source files from this computer once
   every file is verified on the drive; only runs if there were no errors; asks for confirmation).
7. Press **Copy to drive** and confirm. Each file is copied, flushed, **read back and checked by sha256**.
   Do not unplug the drive or its power until it finishes.
8. When done: **Menu → Safely eject drive** before unplugging.

Cancel is available during Analyze and Copy. Files copied before cancelling are verified and listed in
`_MANIFEST.sha256`.

## 5. Other features
- **Drive status line**: connected / mounted read-only or read-write, free space, SMART health,
  bad-sector counts, and a warning if USB/power errors occurred in the last hour.
- **Find on drive** tab: search any file or folder name in the index (works with the drive unplugged).
  Double-click a result to open its folder (drive must be connected).
- **History** tab: every add and rescan with counts (`state/history.log`).
- **Menu**
  - *Open drive in Files*
  - *Rescan drive index (slow)*: walks the whole drive (≈20–40 min) and syncs the index with changes made
    outside the app (new files are added, deleted files removed, changed files re-hashed when needed).
  - *Safely eject drive*: unmounts, detaches the loop device, powers the disk off.

## 6. Command line
```bash
# dry run: show the plan (nothing is written)
lellx-add ~/Downloads/phone-dump --category phones --name "Samsung A30" --date 2027-03-01

# copy for real (optionally delete originals after verification)
lellx-add ~/Downloads/phone-dump --category phones --name "Samsung A30" --date 2027-03-01 --apply
lellx-add ... --apply --delete-source

# save the plan as TSV
lellx-add ... --report plan.tsv

# where is a file on the drive?
lellx-add --find IMG_0597

# drive status / safe eject
lellx-add --status
lellx-add --eject
```
Categories: `laptops`, `phones`, `sd cards & usbs`, `collections`, `old drives`, `projects`, `llo`.

## 7. Dedup rules
- A file is **skipped** only if a **byte-identical** file (same size and full sha256) is already on the drive,
  and that file still exists with the size/date recorded in the index (otherwise it is re-hashed first).
  Matches inside `LLO/` are preferred when several identical copies exist.
- **VM disks and disk images are always copied** (`.vdi .vmdk .vhd .vhdx .vbox .sav .qcow2 .vmx .adi .afi .tib
  .tibx .gho .wim …`): a VM or image set breaks if one piece is missing.
- **Empty files are always copied** (keeps the folder structure readable).
- **Symbolic links are skipped** (NTFS cannot store them).
- A file **identical to another file in the same new backup** is skipped, pointing to that copy.
- Existing folders are never overwritten: the destination folder must not exist yet.

## 8. Files the tool writes
In every new backup folder:

| File | Content |
|---|---|
| `_MANIFEST.sha256` | sha256 of every copied file (`sha256sum -c` compatible) |
| `_DUPLICATES_SKIPPED.txt` | each skipped file `=>` location of the existing copy, with the reason |
| `_ADDED_INFO.txt` | date, source path, counts, sizes, errors |

On the drive root: `/_INDEX/lellx-index.db` (+ `README.txt`).
On the laptop: `state/index.db`, `state/index.work.db` (working copy), `state/history.log`.

## 9. The index
SQLite database, table `f(path, size, mtime, phash, fhash, err, ino)`, paths relative to the drive root.
- Stored on the laptop (`state/index.db`) **and** on the drive (`/_INDEX/lellx-index.db`);
  the newer one is used, both are written after every copy.
- `fhash` = full sha256. Files whose size is unique on the drive may have no hash yet; they are hashed the
  first time a new file of the same size appears.
- If the index is lost: connect the drive (it has its own copy), or run *Rescan drive index*.
- Inspect it: `sqlite3 state/index.db "select count(*), sum(size) from f"`.

## 10. Manual mount / unmount
Find the disk (`lsblk -o NAME,SIZE,MODEL` → model `ST6000VX001-2BD186`, e.g. `/dev/sda`), then:
```bash
# read-only (safe)
L=$(sudo losetup -f --show -r -P --sector-size 512 /dev/sda)
sudo mkdir -p /mnt/lellx && sudo mount -t ntfs-3g -o ro,uid=$(id -u),gid=$(id -g) ${L}p1 /mnt/lellx

# read-write: same, without -r on losetup and with rw,big_writes on mount

# unmount
sudo umount /mnt/lellx && sudo losetup -d $L
udisksctl power-off -b /dev/sda      # before unplugging
```
The kernel `ntfs3` driver cannot mount this volume (`Failed to load $BadClus (-22)`); use `ntfs-3g`.
If the drive is ever connected through an enclosure that reports 512-byte sectors, the partition
(`/dev/sdX1`, label `LELLX`) appears directly and the app mounts it without a loop device.

## 11. Troubleshooting
| Symptom | Cause / fix |
|---|---|
| "Not connected" | Power the enclosure first, wait for spin-up, then plug USB. Check `lsblk`. |
| Kernel log shows `over-current condition` | The enclosure tries to draw power over USB: its power adapter is off/loose/failing. Unplug USB, fix power, retry. |
| Kernel log shows `DID_ERROR`, `I/O error, dev sda`, drive disappears | USB link or power dropped. Reconnect (power first). Nothing on the drive is damaged while it is read-only; a copy can simply be redone into a new folder name. |
| "The drive is in use and cannot be switched" | A Files window or terminal is inside `/mnt/lellx`, or the nightly `locate` indexer (`updatedb`) is crawling it. Close them, or wait. To stop `updatedb` from crawling the drive, add `/mnt/lellx` to `PRUNEPATHS` in `/etc/updatedb.conf`. |
| A password window appears | Passwordless sudo is not available, so `pkexec` asks for your password to mount. |
| "A folder with this name already exists" | Choose another name or date; existing folders are never overwritten. |
| Analysis says files are new but you know they are on the drive | The index is out of date (files were added outside the app): run *Rescan drive index*. |
| "No index found" | Connect the drive (uses `/_INDEX/lellx-index.db`) or run *Rescan drive index*. |
| Drive not shown in Files | The loop mount is not auto-listed; use the **6TB Backup (LELLX)** bookmark or open `/mnt/lellx`. |

## 12. Verifying and recovering
- Verify a backup folder at any time:
  `cd "/mnt/lellx/BACKUPS/Phones/Samsung A30 - 2027-03-01" && sha256sum -c _MANIFEST.sha256`
- Find where a skipped file lives: open `_DUPLICATES_SKIPPED.txt` in the backup folder, or use *Find on drive*.
- If a copy was interrupted: the folder contains only verified files plus notes; delete that folder and add
  the backup again (or add it under a new name).

## 13. Drive layout
```
BACKUPS/
  Laptops/                        <Device or owner> - YYYY-MM-DD
  Phones/                         <Device> - YYYY-MM-DD
  SD cards & USBs/                <Card/stick> - YYYY-MM-DD
  Collections (mixed, undated)/
  Old drives/                     whole-drive backups
  Projects/
  RENAMES.txt                     old -> new folder names (2026-11-12 reorganization)
LLO/                              organized library (always wins over backups, never deduped file by file)
Downloads/  wtsp/  found.000/
_INDEX/                           Backup Adder index
```
Each folder cleaned on 2026-11-12 has `_DUPLICATES_REMOVED.txt` listing removed duplicates `=>` the kept copy.

## 14. Uninstall
```bash
rm ~/.local/share/applications/lellx-backup-adder.desktop ~/.local/bin/lellx-add
rm -r ~/backup-tools        # also deletes the laptop index (the drive keeps /_INDEX)
```
