# -*- coding: utf-8 -*-
"""archive_sync — 压缩包与目录比对补缺（只增不改）

用法：
    python archive_sync.py --root <目标目录> [--config mapping.json] [--apply] [--scatter]

不加 --apply 只出报告，不复制任何文件。压缩包原件一律保留。
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unicodedata
import zipfile
from datetime import datetime

REPORT_DIR = "_整理报告"
SKIP_DIRS = {REPORT_DIR, "_旧版本"}


def norm(name):
    """文件名归一化：全半角、大小写、首尾空格（Windows 不区分大小写）。"""
    return unicodedata.normalize("NFKC", name).casefold().strip()


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def member_name(info):
    """未标 UTF-8 的包按 cp437 还原字节：先试 UTF-8（Mac 打包），失败再试 GBK（Windows 打包）。"""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    try:
        return info.filename.encode("cp437").decode("gbk")
    except Exception:
        return info.filename


def extract_zip(zpath, outdir):
    """按修正名解压，返回 [{'rel','size','zip_time'}]。zipfile 的 extractall 不保留时间，故自行记录。"""
    entries = []
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            rel = member_name(info).replace("\\", "/")
            if not rel or rel.startswith("../") or rel.endswith(":") or rel.startswith("/"):
                continue
            seg = rel.split("/")
            if seg[0] == "__MACOSX" or seg[-1] == ".DS_Store" or seg[-1].startswith("._"):
                continue
            dst = os.path.join(outdir, *rel.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
            entries.append({
                "rel": rel,
                "size": info.file_size,
                "zip_time": "%04d-%02d-%02d %02d:%02d" % info.date_time[:5],
            })
    return entries


def build_index(root):
    """归一化文件名 -> [(大小, 绝对路径)]，用于跨目录去重与撞名判定。"""
    idx = {}
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in files:
            p = os.path.join(dirpath, f)
            try:
                idx.setdefault(norm(f), []).append((os.path.getsize(p), p))
            except OSError:
                continue
    return idx


def strip_prefix(parts, prefixes):
    for p in prefixes:
        if parts and parts[0] == p:
            parts = parts[1:]
        else:
            break
    return parts


def unique_path(target):
    """目标已存在时生成不冲突的路径，绝不覆盖。"""
    if not os.path.exists(target):
        return target
    d, name = os.path.split(target)
    stem, ext = os.path.splitext(name)
    i = 2
    while os.path.exists(os.path.join(d, "%s-%d%s" % (stem, i, ext))):
        i += 1
    return os.path.join(d, "%s-%d%s" % (stem, i, ext))


def load_config(path):
    if not path or not os.path.exists(path):
        return {"archives": {}, "rules": [], "scatter": {}, "fallback": "_待归类", "exclude": []}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("archives", {})
    cfg.setdefault("rules", [])
    cfg.setdefault("scatter", {})
    cfg.setdefault("fallback", "_待归类")
    cfg.setdefault("exclude", [])
    return cfg


def route(zip_name, rel, cfg):
    """决定压缩包内某个相对路径落到目标目录的哪个位置。"""
    spec = cfg["archives"].get(zip_name)
    parts = rel.split("/")
    if spec:
        parts = strip_prefix(parts, spec.get("strip", []))
        base = spec.get("target", "")
    else:
        base = parts[0] if len(parts) > 1 else ""
        parts = parts[1:] if len(parts) > 1 else parts
    if not parts:
        return None
    for rule in cfg["rules"]:
        if any(rule["match_dir"] == p for p in parts[:-1]):
            base = rule["target"]
            break
    return "/".join([base] + parts) if base else "/".join(parts)


def sync(root, cfg, apply_):
    idx = build_index(root)
    report = {"root": root, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "zips": {},
              "copied": [], "conflicts": [], "failed": []}

    zips = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in files:
            if f.lower().endswith(".zip"):
                zips.append(os.path.join(dirpath, f))
    zips.sort()

    for zpath in zips:
        zip_name = os.path.basename(zpath)
        if zip_name in cfg["exclude"]:
            continue
        res = {"copied": 0, "skipped": 0, "conflicts": 0, "failed": 0}
        stage = tempfile.mkdtemp(prefix="archive_sync_")
        try:
            entries = extract_zip(zpath, stage)
        except Exception as e:
            res["failed"] += 1
            report["failed"].append({"zip": zip_name, "reason": "解压失败: %s" % e})
            report["zips"][zip_name] = res
            shutil.rmtree(stage, ignore_errors=True)
            continue

        for e in entries:
            src = os.path.join(stage, *e["rel"].split("/"))
            rel_target = route(zip_name, e["rel"], cfg)
            if not rel_target:
                continue
            dst = os.path.join(root, *rel_target.split("/"))
            name = os.path.basename(rel_target)
            hits = idx.get(norm(name), [])
            same = False
            if hits:
                digest = sha256(src)
                for size, exist in hits:
                    if size == e["size"] and sha256(exist) == digest:
                        same = True
                        break
            if same:
                res["skipped"] += 1
                continue
            if hits:
                # L2：同名但内容不同，保留双方
                stem, ext = os.path.splitext(dst)
                dst = unique_path("%s_补-%s%s" % (stem, os.path.splitext(zip_name)[0], ext))
                report["conflicts"].append({
                    "dir": os.path.dirname(rel_target) or ".",
                    "existing": hits[0][1],
                    "added": os.path.relpath(dst, root),
                    "size_existing": hits[0][0],
                    "size_added": e["size"],
                    "zip_time": e["zip_time"],
                })
                res["conflicts"] += 1
            else:
                dst = unique_path(dst)

            try:
                if apply_:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.copy2(src, dst)
                report["copied"].append({
                    "path": os.path.relpath(dst, root),
                    "size": e["size"],
                    "zip": zip_name,
                    "zip_time": e["zip_time"],
                    "written": bool(apply_),
                })
                if apply_:
                    idx.setdefault(norm(name), []).append((e["size"], dst))
                res["copied"] += 1
            except Exception as ex:
                res["failed"] += 1
                report["failed"].append({"file": rel_target, "reason": str(ex)})
        report["zips"][zip_name] = res
        shutil.rmtree(stage, ignore_errors=True)
    write_report(root, report)
    return report


def write_report(root, report):
    outdir = os.path.join(root, REPORT_DIR)
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, "archive_sync.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    return out


def scatter(root, cfg, apply_):
    """根目录散文件按关键字归位。只处理根目录直属文件。"""
    moved = []
    for f in sorted(os.listdir(root)):
        p = os.path.join(root, f)
        if not os.path.isfile(p) or f.lower().endswith(".zip"):
            continue
        for kw, target in cfg["scatter"].items():
            if kw in f:
                dst_dir = os.path.join(root, *target.split("/"))
                dst = unique_path(os.path.join(dst_dir, f))
                if apply_:
                    os.makedirs(dst_dir, exist_ok=True)
                    shutil.move(p, dst)
                moved.append({"from": f, "to": os.path.relpath(dst, root), "written": bool(apply_)})
                break
    return moved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--config")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--scatter", action="store_true")
    a = ap.parse_args()
    root = os.path.abspath(a.root)
    cfg = load_config(a.config)
    report = sync(root, cfg, a.apply)
    if a.scatter:
        report["scattered"] = scatter(root, cfg, a.apply)
        write_report(root, report)
    out = os.path.join(root, REPORT_DIR, "archive_sync.json")
    print("压缩包 %d 个，待复制 %d，已存在跳过 %d，冲突 %d，失败 %d"
          % (len(report["zips"]), len(report["copied"]),
             sum(z["skipped"] for z in report["zips"].values()),
             len(report["conflicts"]), len(report["failed"])))
    print("报告:", out)
    if not a.apply:
        print("（预演模式，未写入任何文件；加 --apply 执行）")


if __name__ == "__main__":
    sys.exit(main())
