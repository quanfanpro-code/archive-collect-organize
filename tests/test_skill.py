# -*- coding: utf-8 -*-
"""archive-collect-organize 自检：覆盖补缺、撞名、时间裁决三类行为。"""
import csv
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import archive_sync  # noqa: E402
import resolve_conflicts  # noqa: E402


def make_zip(path, items):
    """items: [(压缩包内名称, bytes, datetime)]"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, dt in items:
            zi = zipfile.ZipInfo(name, date_time=dt.timetuple()[:6])
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)


def write(path, data, mtime):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))


class Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="aco_test_")
        self.cfg = {"archives": {}, "rules": [], "scatter": {}, "fallback": "_待归类"}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_sync(self, apply_=True):
        return archive_sync.sync(self.root, self.cfg, apply_)

    def report_path(self):
        return os.path.join(self.root, "_整理报告", "archive_sync.json")


class TestSync(Base):
    def test_same_content_skipped(self):
        write(os.path.join(self.root, "a.txt"), b"same", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"same", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["copied"]), 0)
        self.assertEqual(len(r["conflicts"]), 0)
        self.assertEqual([n for n in os.listdir(self.root) if n.startswith("a")], ["a.txt"])

    def test_conflict_creates_supplement(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"newer", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["conflicts"]), 1)
        names = os.listdir(self.root)
        self.assertIn("a_补-p.txt", names)
        with open(os.path.join(self.root, "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        with open(os.path.join(self.root, "a_补-p.txt"), "rb") as f:
            self.assertEqual(f.read(), b"newer")

    def test_case_difference_not_overwritten(self):
        write(os.path.join(self.root, "report.pdf"), b"AAAA", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("Report.PDF", b"BBBB", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["conflicts"]), 1)
        with open(os.path.join(self.root, "report.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"AAAA")

    def test_dry_run_writes_nothing(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"newer", datetime(2026, 1, 1))])
        self.run_sync(apply_=False)
        self.assertNotIn("a_补-p.txt", os.listdir(self.root))


class TestResolve(Base):
    def _pair(self, exist_data, exist_time, zip_data, zip_time):
        write(os.path.join(self.root, "a.txt"), exist_data, exist_time)
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", zip_data, zip_time)])
        self.run_sync()
        return resolve_conflicts.resolve(self.root, self.report_path(), True)

    def test_zip_newer_replaces_and_archives(self):
        d = self._pair(b"old", datetime(2020, 1, 1), b"new", datetime(2026, 10, 1))
        self.assertEqual(d[0]["保留"], "压缩包版本")
        with open(os.path.join(self.root, "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"new")
        old = os.path.join(self.root, "_旧版本", "a.txt")
        self.assertTrue(os.path.exists(old))
        with open(old, "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_existing_newer_archives_supplement(self):
        d = self._pair(b"current", datetime(2026, 10, 19), b"older", datetime(2020, 1, 1))
        self.assertEqual(d[0]["保留"], "现有文件")
        with open(os.path.join(self.root, "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"current")
        self.assertTrue(os.path.exists(os.path.join(self.root, "_旧版本", "a_补-p.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "a_补-p.txt")))

    def test_equal_time_no_move(self):
        d = self._pair(b"x", datetime(2026, 1, 1, 12, 0), b"y", datetime(2026, 1, 1, 12, 0))
        self.assertEqual(d[0]["保留"], "未定")
        self.assertFalse(os.path.exists(os.path.join(self.root, "_旧版本")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "a_补-p.txt")))

    def test_csv_written_with_bom(self):
        self._pair(b"old", datetime(2020, 1, 1), b"new", datetime(2026, 10, 1))
        path = os.path.join(self.root, "_整理报告", "decisions.csv")
        with open(path, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf")
        with open(path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["保留"], "压缩包版本")


class TestEvidence(Base):
    def test_zip_entry_time_used_when_report_absent(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2020, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"new", datetime(2026, 10, 1))])
        self.run_sync()
        os.remove(self.report_path())
        d = resolve_conflicts.resolve(self.root, self.report_path(), True)
        self.assertEqual(d[0]["保留"], "压缩包版本")

    def test_legacy_supplement_name_recognised(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2020, 1, 1))
        write(os.path.join(self.root, "a_补2.txt"), b"new", datetime(2026, 10, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"new", datetime(2026, 10, 1))])
        d = resolve_conflicts.resolve(self.root, "", True)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["保留"], "压缩包版本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
