"""Tests for backup_core using plain folders as drives. Run: python -m unittest discover -s tests"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import backup_core as core  # noqa: E402


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else data.encode())


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        core.STATE = os.path.join(self.tmp, "state")
        core.HISTORY = os.path.join(core.STATE, "history.log")
        self.root = os.path.join(self.tmp, "drive")
        write(os.path.join(self.root, "BACKUPS", "Phones", "Old - 2020-01-01", "DCIM", "a.jpg"), "photo-one")
        write(os.path.join(self.root, "LLO", "Books", "book.pdf"), "library-file")
        write(os.path.join(self.root, "BACKUPS", "copy-of-book.pdf"), "library-file")
        write(os.path.join(self.root, "LLO", "s1.txt"), "samesize-1")
        self.src = os.path.join(self.tmp, "src")
        write(os.path.join(self.src, "DCIM", "a_copy.jpg"), "photo-one")
        write(os.path.join(self.src, "book.pdf"), "library-file")
        write(os.path.join(self.src, "s2.txt"), "samesize-2")
        write(os.path.join(self.src, "new.txt"), "brand-new")
        write(os.path.join(self.src, "sub", "new_dup.txt"), "brand-new")
        write(os.path.join(self.src, "empty.txt"), "")
        write(os.path.join(self.src, "vm", "disk.vdi"), "photo-one")
        self.drive = core.folder_drive(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plan(self, name="New", date="2027-03-01", src=None):
        idx = core.Index(self.drive)
        dest = core.make_dest("BACKUPS/Phones", name, date)
        return idx, core.analyze(src or self.src, dest, idx)

    def test_first_run_builds_index_and_decides(self):
        idx, plan = self.plan()
        self.assertFalse(idx.empty)
        got = {i.rel: (i.action, i.existing) for i in plan.items}
        self.assertEqual(got["DCIM/a_copy.jpg"], ("SKIP", "BACKUPS/Phones/Old - 2020-01-01/DCIM/a.jpg"))
        self.assertEqual(got["book.pdf"], ("SKIP", "LLO/Books/book.pdf"))          # LLO preferred
        self.assertEqual(got["s2.txt"][0], "COPY")                                   # same size, other content
        self.assertEqual(got["new.txt"][0], "COPY")
        self.assertEqual(got["sub/new_dup.txt"], ("SKIP", "BACKUPS/Phones/New - 2027-03-01/new.txt"))
        self.assertEqual(got["empty.txt"][0], "COPY")
        self.assertEqual(got["vm/disk.vdi"][0], "COPY")                              # images never deduped

    def test_match_by_name(self):
        write(os.path.join(self.root, "Other", "NEW.TXT"), "different content, other size")
        write(os.path.join(self.src, "S1.txt"), "samesize-X")            # same name+size as LLO/s1.txt, other content
        idx = core.Index(self.drive)
        dest = core.make_dest("BACKUPS/Phones", "ByName", "")
        plan = core.analyze(self.src, dest, idx, match="name")
        got = {i.rel: (i.action, i.existing) for i in plan.items}
        self.assertEqual(got["new.txt"], ("SKIP", "Other/NEW.TXT"))                    # case-insensitive name
        self.assertEqual(got["S1.txt"], ("SKIP", "LLO/s1.txt"))
        self.assertEqual(got["book.pdf"], ("SKIP", "LLO/Books/book.pdf"))
        self.assertEqual(got["DCIM/a_copy.jpg"][0], "COPY")                           # name differs
        self.assertEqual(got["vm/disk.vdi"][0], "COPY")
        plan2 = core.analyze(self.src, core.make_dest("", "BySize", ""), idx, match="name+size")
        got2 = {i.rel: i.action for i in plan2.items}
        self.assertEqual(got2["new.txt"], "COPY")                                      # same name, other size
        self.assertEqual(got2["S1.txt"], "SKIP")
        # a file removed from the drive after indexing is not a match
        os.remove(os.path.join(self.root, "Other", "NEW.TXT"))
        plan3 = core.analyze(self.src, core.make_dest("", "Gone", ""), idx, match="name")
        self.assertEqual({i.rel: i.action for i in plan3.items}["new.txt"], "COPY")

    def test_name_match_never_deletes_original(self):
        write(os.path.join(self.src, "S1.txt"), "samesize-X")
        idx = core.Index(self.drive)
        plan = core.analyze(self.src, core.make_dest("", "Del", ""), idx, match="name+size")
        res = core.execute(plan, idx, delete_source=True)
        self.assertTrue(os.path.exists(os.path.join(self.src, "S1.txt")))              # content differs: kept
        self.assertTrue(os.path.exists(os.path.join(self.src, "book.pdf")))            # name-only match: kept
        self.assertGreaterEqual(res["kept"], 2)
        with open(os.path.join(self.root, "Del - no date", "_ADDED_INFO.txt"), encoding="utf-8") as f:
            self.assertIn("Same file name and size", f.read())

    def test_copy_verifies_writes_notes_and_index(self):
        idx, plan = self.plan()
        res = core.execute(plan, idx)
        self.assertEqual(res["failures"], [])
        dest = os.path.join(self.root, "BACKUPS", "Phones", "New - 2027-03-01")
        for f in ("_MANIFEST.sha256", "_DUPLICATES_SKIPPED.txt", "_ADDED_INFO.txt", "new.txt", "s2.txt",
                  os.path.join("vm", "disk.vdi")):
            self.assertTrue(os.path.exists(os.path.join(dest, f)), f)
        self.assertFalse(os.path.exists(os.path.join(dest, "book.pdf")))
        self.assertTrue(os.path.exists(os.path.join(self.root, core.APP_DIR_NAME, core.INDEX_NAME)))
        with open(os.path.join(dest, "_DUPLICATES_SKIPPED.txt"), encoding="utf-8") as f:
            self.assertIn("book.pdf\t=>\tLLO/Books/book.pdf", f.read())
        # second backup of the same source: everything is a duplicate now (except always-copy files)
        idx2, plan2 = self.plan(date="2027-03-02")
        acts = {i.rel: i.action for i in plan2.items}
        self.assertEqual(acts["new.txt"], "SKIP")
        self.assertEqual(acts["s2.txt"], "SKIP")
        self.assertEqual(acts["vm/disk.vdi"], "COPY")

    def test_existing_destination_refused(self):
        idx, plan = self.plan()
        core.execute(plan, idx)
        with self.assertRaises(ValueError):
            self.plan()

    def test_source_on_drive_refused(self):
        idx = core.Index(self.drive)
        with self.assertRaises(ValueError):
            core.analyze(os.path.join(self.root, "LLO"), "X - no date", idx)
        with self.assertRaises(ValueError):
            core.analyze(os.path.join(self.root, "LLO", "s1.txt"), "Y - no date", idx)

    def test_delete_originals_only_when_safe(self):
        idx, plan = self.plan()
        res = core.execute(plan, idx, delete_source=True)
        self.assertEqual(res["kept"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.src, "new.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.src, "book.pdf")))
        # the drive copies are untouched
        self.assertTrue(os.path.exists(os.path.join(self.root, "LLO", "Books", "book.pdf")))

    def test_delete_keeps_original_if_existing_copy_changed(self):
        idx, plan = self.plan()
        write(os.path.join(self.root, "LLO", "Books", "book.pdf"), "changed!!!!!")   # same size, other bytes
        res = core.execute(plan, idx, delete_source=True)
        self.assertTrue(os.path.exists(os.path.join(self.src, "book.pdf")))
        self.assertGreaterEqual(res["kept"], 1)

    def test_safe_join_rejects_escapes(self):
        self.assertIsNone(core.safe_join(self.root, "../outside.txt"))
        self.assertIsNone(core.safe_join(self.root, "a/../../x"))
        self.assertIsNone(core.safe_join(self.root, "/etc/passwd"))
        self.assertIsNone(core.safe_join(self.root, "C:/Windows/x"))
        self.assertIsNotNone(core.safe_join(self.root, "LLO/s1.txt"))

    def test_bad_index_path_is_dropped_not_followed(self):
        outside = os.path.join(self.tmp, "outside.txt")
        write(outside, "brand-new")
        idx = core.Index(self.drive)
        core.scan(idx)
        idx.add("../outside.txt", 9, os.path.getmtime(outside), None)
        plan = core.analyze(self.src, "BACKUPS/Z - no date", idx)
        self.assertEqual({i.rel: i.action for i in plan.items}["new.txt"], "COPY")

    def test_rescan_picks_up_outside_changes(self):
        idx = core.Index(self.drive)
        core.scan(idx)
        write(os.path.join(self.root, "manual", "brand.txt"), "brand-new")
        os.remove(os.path.join(self.root, "LLO", "s1.txt"))
        res = core.scan(idx)
        self.assertEqual((res["added"], res["removed"]), (1, 1))
        plan = core.analyze(self.src, "BACKUPS/R - no date", idx)
        self.assertEqual({i.rel: i.action for i in plan.items}["new.txt"], "SKIP")

    def test_make_dest(self):
        self.assertEqual(core.make_dest("BACKUPS/Phones", "A30", "2027-01-02"), "BACKUPS/Phones/A30 - 2027-01-02")
        self.assertEqual(core.make_dest("", "A30", ""), "A30 - no date")
        with self.assertRaises(ValueError):
            core.make_dest("x", "A30", "02-01-2027")
        with self.assertRaises(ValueError):
            core.make_dest("x", "  ", "")


if __name__ == "__main__":
    unittest.main()
