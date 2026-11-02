# -*- coding: utf-8 -*-
"""resolve_conflicts — 兼容旧版补文件的核查工具，默认保留双方。

用法：
    python resolve_conflicts.py --root <目标目录> [--report <报告>] [--by-time] [--apply]

只有用户另行授权按时间处理时才传入 --by-time。跨目录文件始终不作版本裁决。
"""
import argparse
import csv
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from archive_sync import unique_path

OLD_DIR = "_旧版本"
REPORT_DIR = "_整理报告"
SKIP_DIRS = {REPORT_DIR, OLD_DIR}
PAIR_RE = re.compile(r"^(?P<base>.+?)_补-(?P<src>[^\\/]+?)(?:-(?P<n>\d+))?(?P<ext>\.[^.]+)$")
LEGACY_RE = re.compile(r"^(?P<base>.+?)_补(?P<n>\d*)(?P<ext>\.[^.]+)$")


def parse_time(text):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except (ValueError, TypeError):
            continue
    return None


def zip_entry_times(root):
    """全树压缩包内条目时间：归一化文件名 -> (datetime, 包名)。"""
    table = {}
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in files:
            if not f.lower().endswith(".zip"):
                continue
            try:
                with zipfile.ZipFile(os.path.join(dirpath, f)) as z:
                    for info in z.infolist():
                        if info.is_dir():
                            continue
                        name = info.filename
                        if not info.flag_bits & 0x800:
                            try:
                                name = name.encode("cp437").decode("gbk")
                            except Exception:
                                pass
                        name = name.replace("\\", "/").split("/")[-1]
                        key = name.casefold().strip()
                        if key not in table:
                            table[key] = (datetime(*info.date_time[:6]), f)
                        else:
                            table[key] = None
            except Exception:
                continue
    return table


def pairs_from_report(report_path, root):
    with open(report_path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for c in data.get("conflicts", []):
        existing = c["existing"]
        added = c["added"]
        out.append({
            "dir": c["dir"],
            "existing": existing if os.path.isabs(existing) else os.path.join(root, existing),
            "added": added if os.path.isabs(added) else os.path.join(root, added),
            "zip_time": c.get("zip_time"),
        })
    return out


def pairs_by_scan(root, zip_times):
    """没有报告时，按 _补- 命名反查配对，时间从压缩包条目里取。"""
    out = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in files:
            m = PAIR_RE.match(f) or LEGACY_RE.match(f)
            if not m:
                continue
            added = os.path.join(dirpath, f)
            base = m.group("base") + m.group("ext")
            existing = os.path.join(dirpath, base)
            if not os.path.exists(existing):
                continue
            zt = zip_times.get(base.casefold().strip())
            out.append({
                "dir": os.path.relpath(dirpath, root),
                "existing": existing,
                "added": added,
                "zip_time": zt[0].strftime("%Y-%m-%d %H:%M") if zt else None,
            })
    return out


def _unique(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists("%s-%d%s" % (stem, i, ext)):
        i += 1
    return "%s-%d%s" % (stem, i, ext)


def move_to_old(path):
    """把文件移入同级 _旧版本 目录，返回新路径。"""
    old_dir = os.path.join(os.path.dirname(path), OLD_DIR)
    os.makedirs(old_dir, exist_ok=True)
    dst = _unique(os.path.join(old_dir, os.path.basename(path)))
    os.rename(path, dst)
    return dst


def resolve(root, report_path, apply_, by_time=False):
    zip_times = zip_entry_times(root)
    if report_path and os.path.exists(report_path):
        pairs = pairs_from_report(report_path, root)
    else:
        pairs = pairs_by_scan(root, zip_times)

    decisions = []
    for p in pairs:
        existing = p["existing"]
        added = p["added"]
        for path in (existing, added):
            if not Path(path).resolve().is_relative_to(Path(root).resolve()):
                raise ValueError("报告路径超出资料目录: " + path)
        if not (os.path.isfile(existing) and os.path.isfile(added)):
            continue
        t_exist = datetime.fromtimestamp(os.path.getmtime(existing))
        t_add = parse_time(p["zip_time"]) if p["zip_time"] else None
        if t_add is None:
            name = os.path.basename(existing).casefold().strip()
            evidence = zip_times.get(name)
            t_add = evidence[0] if evidence else None

        winner, action = None, "时间缺失，未处理"
        if Path(existing).resolve().parent != Path(added).resolve().parent:
            action = "不同目录的文件，保留双方，不判定新旧"
        elif not by_time:
            action = "未授权按时间判定，保留双方"
        elif t_add is not None:
            delta = (t_add - t_exist).total_seconds()
            if delta > 60:
                winner, action = "压缩包版本", "现有文件移入 _旧版本，补文件改回原名"
            elif delta < -60:
                winner, action = "现有文件", "补文件移入 _旧版本"
            else:
                action = "时间相同（60 秒内），未处理"

        if apply_ and winner:
            if winner == "压缩包版本":
                move_to_old(existing)
                os.rename(added, existing)
            else:
                move_to_old(added)

        decisions.append({
            "目录": p["dir"],
            "现有文件": os.path.basename(existing),
            "压缩包版本": os.path.basename(added),
            "现有修改时间": t_exist.strftime("%Y-%m-%d %H:%M"),
            "包内修改时间": t_add.strftime("%Y-%m-%d %H:%M") if t_add else "未知",
            "保留": winner or "未定",
            "处理": action if apply_ or not winner else action + "（预演）",
        })
    write_decisions(root, decisions)
    return decisions


FIELDS = ["目录", "现有文件", "压缩包版本", "现有修改时间", "包内修改时间", "保留", "处理"]


def write_decisions(root, decisions):
    outdir = os.path.join(root, REPORT_DIR)
    os.makedirs(outdir, exist_ok=True)
    csv_path = unique_path(os.path.join(outdir, "decisions.csv"))
    with open(csv_path, "x", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(decisions)
    with open(unique_path(os.path.join(outdir, "decisions.json")), "x", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=1)
    return csv_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--report")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--by-time", action="store_true", help="仅在用户明确授权后按时间处理同目录旧版补文件")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    reports = list(Path(root, REPORT_DIR).glob("archive_sync*.json"))
    latest = str(max(reports, key=lambda p: p.stat().st_mtime_ns)) if reports else ""
    report = a.report or latest
    decisions = resolve(root, report, a.apply, by_time=a.by_time)
    auto = sum(1 for d in decisions if d["保留"] != "未定")
    print("冲突 %d 对，可判定 %d 对，未判定 %d 对" % (len(decisions), auto, len(decisions) - auto))
    csv_reports = list(Path(root, REPORT_DIR).glob("decisions*.csv"))
    print("清单:", max(csv_reports, key=lambda p: p.stat().st_mtime_ns))
    if not a.apply:
        print("（预演模式，未移动任何文件；加 --apply 执行）")


if __name__ == "__main__":
    sys.exit(main())
