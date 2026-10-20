# -*- coding: utf-8 -*-
"""resolve_conflicts — 撞名冲突按修改时间取新，旧版移入同级 _旧版本

用法：
    python resolve_conflicts.py --root <目标目录> [--report <archive_sync.json>] [--apply]

比较两侧：压缩包内修改时间（ZipInfo.date_time，入包时的真实时间）与现有文件修改时间。
差值超过 60 秒才判定；相同则不动。不加 --apply 只生成 decisions.csv，不移动任何文件。
"""
import argparse
import csv
import json
import os
import re
import sys
import zipfile
from datetime import datetime

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
    os.replace(path, dst)
    return dst


def resolve(root, report_path, apply_):
    zip_times = zip_entry_times(root)
    if report_path and os.path.exists(report_path):
        pairs = pairs_from_report(report_path, root)
    else:
        pairs = pairs_by_scan(root, zip_times)

    decisions = []
    for p in pairs:
        existing = p["existing"]
        added = p["added"]
        if not (os.path.isfile(existing) and os.path.isfile(added)):
            continue
        t_exist = datetime.fromtimestamp(os.path.getmtime(existing))
        t_add = parse_time(p["zip_time"]) if p["zip_time"] else None
        if t_add is None:
            name = os.path.basename(existing).casefold().strip()
            t_add = zip_times[name][0] if name in zip_times else None

        winner, action = None, "时间缺失，未处理"
        if t_add is not None:
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
                os.replace(added, existing)
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
    csv_path = os.path.join(outdir, "decisions.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(decisions)
    with open(os.path.join(outdir, "decisions.json"), "w", encoding="utf-8") as f:
        json.dump(decisions, f, ensure_ascii=False, indent=1)
    return csv_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--report")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    report = a.report or os.path.join(root, REPORT_DIR, "archive_sync.json")
    decisions = resolve(root, report, a.apply)
    auto = sum(1 for d in decisions if d["保留"] != "未定")
    print("冲突 %d 对，可判定 %d 对，未判定 %d 对" % (len(decisions), auto, len(decisions) - auto))
    print("清单:", os.path.join(root, REPORT_DIR, "decisions.csv"))
    if not a.apply:
        print("（预演模式，未移动任何文件；加 --apply 执行）")


if __name__ == "__main__":
    sys.exit(main())
