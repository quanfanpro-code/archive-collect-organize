# -*- coding: utf-8 -*-
"""依据已核对的业务归位清单移动资料；不猜分类、不改文件名、不删除文件。"""
import argparse
import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from archive_sync import REPORT_DIR, safe_target, sha256


def organize(root, moves, apply_=False):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("资料目录不存在")
    report_dir = root / REPORT_DIR / ("业务归类_" + datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    report_dir.mkdir(parents=True, exist_ok=False)
    report = {"mode": "执行" if apply_ else "预演", "complete": False,
              "items": [], "failed": [], "unplanned": [], "empty_directories": []}
    source_keys, target_keys = set(), set()
    for item in moves:
        try:
            src, dst = safe_target(root, item["source"]), safe_target(root, item["target"])
            if REPORT_DIR in src.relative_to(root).parts or REPORT_DIR in dst.relative_to(root).parts:
                raise ValueError("内部报告目录不得作为业务资料归位")
            if not src.is_file() or src.name != dst.name:
                raise ValueError("源文件不存在，或目标改变了原文件名")
            src_key, dst_key = str(src).casefold(), str(dst).casefold()
            if src_key in source_keys or dst_key in target_keys:
                raise ValueError("源文件重复，或多个文件落到同一目标路径")
            if dst.exists() and src != dst:
                raise ValueError("目标已存在；请在清单中增加来源子目录，不能覆盖或改名")
            source_keys.add(src_key)
            target_keys.add(dst_key)
            report["items"].append({"source": str(src), "target": str(dst),
                "size": src.stat().st_size, "sha256": sha256(src), "status": "待归位"})
        except (KeyError, OSError, ValueError) as exc:
            report["failed"].append({"item": item, "reason": str(exc)})
    actual = {str(p.resolve()).casefold() for p in root.rglob("*")
              if p.is_file() and REPORT_DIR not in p.relative_to(root).parts}
    report["unplanned"] = sorted(actual - source_keys)
    if not moves:
        report["failed"].append({"reason": "归位清单为空"})
    if apply_ and not report["failed"] and not report["unplanned"]:
        # 先完成全清单检查，防止发现漏项或撞名时已经移动了前面的文件。
        with (report_dir / "移动记录.jsonl").open("x", encoding="utf-8") as journal:
            for item in report["items"]:
                try:
                    src, dst = Path(item["source"]), Path(item["target"])
                    if sha256(src) != item["sha256"]:
                        raise ValueError("源文件在预检后发生变化")
                    if src != dst:
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(src, dst)
                        item["status"] = "已移动，待核验"
                        journal.write(json.dumps(item, ensure_ascii=False) + "\n")
                        journal.flush()
                    if sha256(dst) != item["sha256"] or dst.stat().st_size != item["size"]:
                        raise ValueError("归位后回读核验不一致")
                    item["status"] = "核验通过"
                except (OSError, ValueError) as exc:
                    report["failed"].append({"item": item.copy(), "reason": str(exc)})
                    break
        if not report["failed"]:
            old_tops = {Path(i["source"]).relative_to(root).parts[0] for i in report["items"]
                        if i["source"] != i["target"]}
            for name in sorted(old_tops):
                src = safe_target(root, name)
                if src.is_dir() and not any(p.is_file() or p.is_symlink() for p in src.rglob("*")):
                    dst = report_dir / "归位后空目录" / name
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    if not src.resolve().is_relative_to(root) or not dst.resolve().is_relative_to(report_dir):
                        raise ValueError("空目录归位路径越界")
                    os.rename(src, dst)
                    report["empty_directories"].append({"source": str(src), "target": str(dst)})
            final = {str(p.resolve()).casefold() for p in root.rglob("*")
                     if p.is_file() and REPORT_DIR not in p.relative_to(root).parts}
            if final != target_keys:
                report["failed"].append({"reason": "归位后的文件集合与清单不一致"})
        report["complete"] = not report["failed"]
    report["categories"] = dict(Counter(Path(i["target"]).relative_to(root).parts[0]
                                         for i in report["items"]))
    report["report_dir"] = str(report_dir)
    with (report_dir / "结果.json").open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with (report_dir / "归位明细.csv").open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["文件名", "原位置", "归位后位置", "字节数", "内容指纹", "核验结果"])
        for i in report["items"]:
            writer.writerow([Path(i["source"]).name, i["source"], i["target"],
                             i["size"], i["sha256"], i["status"]])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    with open(args.plan, encoding="utf-8-sig") as f:
        plan = json.load(f)
    report = organize(args.root, plan["moves"], args.apply)
    print("清单文件 %d 个，漏列 %d 个，失败 %d 个，%s" % (
        len(report["items"]), len(report["unplanned"]), len(report["failed"]),
        "业务归类完成且核验通过" if report["complete"] else "业务归类尚未完成"))
    print("报告:", report["report_dir"])
    return 1 if report["failed"] or report["unplanned"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
