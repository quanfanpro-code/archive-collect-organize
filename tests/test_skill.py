# -*- coding: utf-8 -*-
"""资料整理回归验证；样本保留，不自动删除测试文件。"""
import csv
import json
import os
import importlib
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import archive_sync  # noqa: E402
import resolve_conflicts  # noqa: E402
business = importlib.import_module("业务归类")


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
        parent = os.environ.get("ACO_TEST_ROOT")
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.root = tempfile.mkdtemp(prefix="aco_test_", dir=parent)
        self.cfg = {"archives": {}, "rules": [], "scatter": {}, "fallback": "_待归类"}

    def tearDown(self):
        pass

    def run_sync(self, apply_=True):
        return archive_sync.sync(self.root, self.cfg, apply_)

    def report_path(self):
        return os.path.join(self.root, "_整理报告", "archive_sync.json")


class TestSync(Base):
    def test_unmarked_chinese_names_decode_utf8_and_gbk(self):
        for encoding in ("utf-8", "gbk"):
            info = zipfile.ZipInfo("资料/记录.txt".encode(encoding).decode("cp437"))
            info.flag_bits = 0
            self.assertEqual(archive_sync.member_name(info), "资料/记录.txt")

    def test_exclude_configuration_and_system_metadata(self):
        make_zip(os.path.join(self.root, "p.zip"),
                 [(name, b"data", datetime(2026, 1, 1)) for name in
                  ("__MACOSX/metadata", ".DS_Store", "._说明.txt", "说明.txt")])
        make_zip(os.path.join(self.root, "保留.zip"), [("不能展开.txt", b"keep", datetime(2026, 1, 1))])
        self.cfg["exclude"] = ["保留.zip"]
        result = self.run_sync()
        self.assertEqual(len(result["copied"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "不能展开.txt")))
        self.assertEqual(len(result["zips"]), 1)

    def test_same_named_packages_preserve_source_context(self):
        for source in ("事项甲", "事项乙"):
            os.makedirs(os.path.join(self.root, source))
            make_zip(os.path.join(self.root, source, "附件.zip"),
                     [("资料/说明.txt", b"same", datetime(2026, 1, 1))])
        result = self.run_sync()
        self.assertEqual(len(result["zips"]), 2)
        self.assertEqual(len(result["copied"]), 2)
        for source in ("事项甲", "事项乙"):
            self.assertTrue(os.path.isfile(os.path.join(self.root, source, "资料", "说明.txt")))

    def test_user_old_version_directory_is_not_excluded(self):
        os.makedirs(os.path.join(self.root, "_旧版本"))
        make_zip(os.path.join(self.root, "_旧版本", "附件.zip"),
                 [("资料/a.txt", b"same", datetime(2026, 1, 1))])
        self.assertEqual(len(self.run_sync()["copied"]), 1)

    def test_integrity_failure_stops_further_extraction(self):
        make_zip(os.path.join(self.root, "p.zip"),
                 [("a.txt", b"one", datetime(2026, 1, 1)),
                  ("b.txt", b"two", datetime(2026, 1, 1))])
        with patch.object(archive_sync, "sha256", return_value="wrong"):
            result = self.run_sync()
        self.assertTrue(result["failed"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "b.txt")))

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
        self.assertIn("_同路径并存", names)
        with open(os.path.join(self.root, "a.txt"), "rb") as f:
            self.assertEqual(f.read(), b"old")
        with open(os.path.join(self.root, "_同路径并存", "p", "a.txt"), "rb") as f:
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
        self.assertNotIn("_同路径并存", os.listdir(self.root))

    def test_different_directories_keep_identical_content(self):
        write(os.path.join(self.root, "G04", "目录.dwg"), b"same", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("H01/目录.dwg", b"same", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["copied"]), 1)
        self.assertEqual(len(r["conflicts"]), 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "H01", "目录.dwg")))

    def test_different_directories_keep_different_content(self):
        write(os.path.join(self.root, "G04", "目录.dwg"), b"G04", datetime(2024, 1, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("H01/目录.dwg", b"H01", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["conflicts"]), 0)
        with open(os.path.join(self.root, "H01", "目录.dwg"), "rb") as f:
            self.assertEqual(f.read(), b"H01")

    def test_dry_run_detects_collisions_between_packages(self):
        for name, data in (("p.zip", b"one"), ("q.zip", b"two")):
            make_zip(os.path.join(self.root, name), [("a.txt", data, datetime(2026, 1, 1))])
        preview = self.run_sync(False)
        applied = self.run_sync(True)
        self.assertEqual(len(preview["copied"]), 2)
        self.assertEqual(len(preview["conflicts"]), 1)
        self.assertEqual([r["path"] for r in preview["copied"]], [r["path"] for r in applied["copied"]])

    def test_nested_parent_path_rejected(self):
        make_zip(os.path.join(self.root, "p.zip"), [("nested/../a.txt", b"bad", datetime(2026, 1, 1))])
        r = self.run_sync()
        self.assertEqual(len(r["failed"]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.root, "a.txt")))

    def test_reports_not_overwritten(self):
        first = self.run_sync(False)
        with open(first["report_path"], "rb") as f:
            original = f.read()
        second = self.run_sync(False)
        self.assertNotEqual(first["report_path"], second["report_path"])
        with open(first["report_path"], "rb") as f:
            self.assertEqual(f.read(), original)

    def test_scatter_collision_never_renames_or_overwrites(self):
        write(os.path.join(self.root, "合同.pdf"), b"one", datetime(2024, 1, 1))
        write(os.path.join(self.root, "合同", "合同.pdf"), b"two", datetime(2024, 1, 1))
        self.cfg["scatter"] = {"合同": "合同"}
        with self.assertRaises(FileExistsError):
            archive_sync.scatter(self.root, self.cfg, True)
        with open(os.path.join(self.root, "合同.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"one")
        with open(os.path.join(self.root, "合同", "合同.pdf"), "rb") as f:
            self.assertEqual(f.read(), b"two")


class TestResolve(Base):
    def _pair(self, exist_data, exist_time, zip_data, zip_time):
        write(os.path.join(self.root, "a.txt"), exist_data, exist_time)
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", zip_data, zip_time)])
        write(os.path.join(self.root, "a_补-p.txt"), zip_data, zip_time)
        os.makedirs(os.path.dirname(self.report_path()), exist_ok=True)
        with open(self.report_path(), "w", encoding="utf-8") as f:
            json.dump({"conflicts": [{"dir": ".", "existing": "a.txt", "added": "a_补-p.txt",
                                      "zip_time": zip_time.strftime("%Y-%m-%d %H:%M:%S")}]}, f)
        return resolve_conflicts.resolve(self.root, self.report_path(), True, by_time=True)

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
        write(os.path.join(self.root, "a_补-p.txt"), b"new", datetime(2026, 10, 1))
        d = resolve_conflicts.resolve(self.root, "", True, by_time=True)
        self.assertEqual(d[0]["保留"], "压缩包版本")

    def test_legacy_supplement_name_recognised(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2020, 1, 1))
        write(os.path.join(self.root, "a_补2.txt"), b"new", datetime(2026, 10, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"new", datetime(2026, 10, 1))])
        d = resolve_conflicts.resolve(self.root, "", True, by_time=True)
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["保留"], "压缩包版本")

    def test_default_never_selects_by_time(self):
        write(os.path.join(self.root, "a.txt"), b"old", datetime(2020, 1, 1))
        write(os.path.join(self.root, "a_补2.txt"), b"new", datetime(2026, 10, 1))
        make_zip(os.path.join(self.root, "p.zip"), [("a.txt", b"new", datetime(2026, 10, 1))])
        d = resolve_conflicts.resolve(self.root, "", True)
        self.assertEqual(d[0]["保留"], "未定")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "a_补2.txt")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "_旧版本")))

    def test_cross_directory_never_selects_by_time(self):
        write(os.path.join(self.root, "G04", "a.txt"), b"old", datetime(2020, 1, 1))
        write(os.path.join(self.root, "H01", "a.txt"), b"new", datetime(2026, 1, 1))
        path = os.path.join(self.root, "pair.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"conflicts": [{"dir": "H01", "existing": "G04/a.txt", "added": "H01/a.txt",
                                      "zip_time": "2026-01-01 00:00:00"}]}, f)
        d = resolve_conflicts.resolve(self.root, path, True, by_time=True)
        self.assertEqual(d[0]["保留"], "未定")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "G04", "a.txt")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "H01", "a.txt")))


class TestBusiness(Base):
    def test_user_report_directory_is_not_silently_ignored(self):
        write(os.path.join(self.root, "_整理报告", "用户资料.txt"), b"user", datetime(2026, 1, 1))
        write(os.path.join(self.root, "便笺.txt"), b"memo", datetime(2026, 1, 1))
        result = business.organize(self.root, [{"source": "便笺.txt", "target": "记录/便笺.txt"}], True)
        self.assertFalse(result["complete"])
        self.assertEqual(len(result["unplanned"]), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "便笺.txt")))

    def test_nested_user_report_name_can_be_planned(self):
        write(os.path.join(self.root, "用户数据", "_整理报告", "资料.txt"), b"user", datetime(2026, 1, 1))
        result = business.organize(self.root, [{"source": "用户数据/_整理报告/资料.txt",
                                               "target": "资料/资料.txt"}], True)
        self.assertTrue(result["complete"])

    def test_finalization_failure_still_records_moved_files(self):
        moves = self.fixture()
        rename = os.rename
        def fail_on_empty_directory(src, dst):
            if Path(src).is_dir():
                raise PermissionError("模拟空目录归位失败")
            return rename(src, dst)
        with patch.object(business.os, "rename", side_effect=fail_on_empty_directory):
            result = business.organize(self.root, moves, True)
        self.assertFalse(result["complete"])
        self.assertTrue(result["failed"])
        self.assertTrue(Path(result["report_dir"], "结果.json").is_file())
        for move in moves:
            self.assertTrue(os.path.isfile(os.path.join(self.root, move["target"])))

    def fixture(self):
        write(os.path.join(self.root, "包一", "建筑", "G04", "目录.dwg"), b"same", datetime(2024, 1, 1))
        write(os.path.join(self.root, "包二", "建筑", "H01", "目录.dwg"), b"same", datetime(2026, 1, 1))
        write(os.path.join(self.root, "合同.pdf"), b"contract", datetime(2026, 1, 1))
        return [{"source": "包一/建筑/G04/目录.dwg", "target": "02 设计/建筑/G04/目录.dwg"},
                {"source": "包二/建筑/H01/目录.dwg", "target": "02 设计/建筑/H01/目录.dwg"},
                {"source": "合同.pdf", "target": "03 合同/合同.pdf"}]

    def test_packages_merge_by_business_keep_paths_and_names(self):
        moves = self.fixture()
        preview = business.organize(self.root, moves, False)
        self.assertFalse(preview["complete"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "02 设计")))
        report = business.organize(self.root, moves, True)
        self.assertTrue(report["complete"])
        self.assertEqual(report["categories"], {"02 设计": 2, "03 合同": 1})
        for move in moves:
            self.assertTrue(os.path.isfile(os.path.join(self.root, move["target"])))
            self.assertFalse(os.path.exists(os.path.join(self.root, move["source"])))
        self.assertFalse(os.path.exists(os.path.join(self.root, "包一")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "包二")))
        self.assertEqual(len(report["empty_directories"]), 2)

    def test_missing_files_stop_before_any_move(self):
        moves = self.fixture()
        report = business.organize(self.root, moves[:1], True)
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["unplanned"]), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.root, moves[0]["source"])))

    def test_target_collision_stops_before_any_move(self):
        moves = self.fixture()
        moves[1]["target"] = moves[0]["target"]
        report = business.organize(self.root, moves, True)
        self.assertFalse(report["complete"])
        self.assertTrue(report["failed"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, moves[0]["source"])))

    def test_renaming_or_outside_target_rejected(self):
        moves = self.fixture()
        moves[0]["target"] = "02 设计/图纸新名字.dwg"
        moves[1]["target"] = "../outside/目录.dwg"
        report = business.organize(self.root, moves, True)
        self.assertFalse(report["complete"])
        self.assertEqual(len(report["failed"]), 2)

    def test_already_organized_files_can_be_kept(self):
        moves = self.fixture()
        self.assertTrue(business.organize(self.root, moves, True)["complete"])
        kept = [{"source": m["target"], "target": m["target"]} for m in moves]
        self.assertTrue(business.organize(self.root, kept, True)["complete"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
