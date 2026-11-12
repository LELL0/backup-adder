# Backup Adder for Windows

Same tool as on Linux: add a backup to an external drive (or any folder / network share) without copying
files that are already on it. Each file is listed as **Copy** or **Skip** with the reason, and skipped files
show where the identical copy already is.

## Get it
**Option A: ready-made program (no Python needed)**
1. On GitHub open the repository → **Releases** (or **Actions → latest "test-and-build" run → Artifacts**).
2. Download `BackupAdder-windows.zip` and extract it anywhere (e.g. `C:\Tools\BackupAdder`).
3. Run **`BackupAdder.exe`**. Windows SmartScreen may warn because the program is not signed:
   *More info → Run anyway*.

The zip also contains `backup-adder-cli.exe` (command line).

**Option B: run from source**
1. Install Python 3.11+ from python.org (tick *Add python.exe to PATH*; Tk is included).
2. Download the repository (Code → Download ZIP) and extract it.
3. Double-click `backup_adder_tk.py` (or run `py backup_adder_tk.py`).

## Use it
1. Connect the backup drive. It appears in the **Backup drive** list (the Windows system drive is never listed).
   To use a network share or any folder, choose **Other folder…**.
2. The status line shows free space and the index state. **The first time a drive is used, its index is built
   automatically** when you press Analyze: the whole drive is listed (names and sizes only, no file reading),
   which can take a few minutes on a big drive. Later runs reuse it and are fast.
3. **Source**: Folder… / File… → what you want to add.
4. **Goes into folder**: pick an existing folder of the drive from the list (or type a new one),
   then **Name** and **Date (YYYY-MM-DD)**. "Will be saved as" shows the final folder, e.g.
   `E:\BACKUPS\Phones\Samsung A30 - 2027-03-01`.
5. **Analyze** → review the table (filter: All / Will copy / Skipped / Errors; search box).
   Double-click a skipped file to open the folder of its existing copy. **Nothing is written yet.**
6. **Copy to drive** → every copied file is read back and checked (sha256).
   Optional: *Delete originals after verified copy* (asks for confirmation, only runs if there were no errors,
   and keeps any original whose copy on the drive can't be re-confirmed).
7. **Safely eject** before unplugging.

Other tabs: **Find on drive** (search the index by name), **History**.
Buttons: **Rescan index** (if files were added or removed on the drive outside the app), **Open**.

## Where things are stored
- On each drive: hidden folder `.backup-adder\` with `index.db`, `settings.json`, `README.txt`.
- On the computer: `%LOCALAPPDATA%\BackupAdder\` (index cache per drive, `history.log`).
- In each new backup folder: `_MANIFEST.sha256`, `_DUPLICATES_SKIPPED.txt`, `_ADDED_INFO.txt`.

`settings.json` → `"prefer": ["LLO"]` makes copies inside those folders the ones reported as "existing copy".

## Command line
```bat
backup-adder-cli.exe --list
backup-adder-cli.exe D:\PhoneDump --drive LELLX --into "BACKUPS\Phones" --name "Samsung A30" --date 2027-03-01
backup-adder-cli.exe D:\PhoneDump --drive LELLX --into "BACKUPS\Phones" --name "Samsung A30" --date 2027-03-01 --apply
backup-adder-cli.exe --drive LELLX --find IMG_0597
```

## Notes
- Paths longer than 260 characters are supported.
- The LELLX 6 TB disk in its current USB enclosure reports 4096-byte sectors while it was partitioned with
  512-byte sectors; **Windows will not see its partition in that enclosure either** (Disk Management shows it
  as unknown/unallocated — do NOT initialize or format it). Use it on Linux, or put the disk in an enclosure /
  SATA dock that reports 512-byte sectors, where Windows sees it as a normal NTFS drive.
