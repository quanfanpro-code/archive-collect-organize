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
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path, PureWindowsPath

REPORT_DIR = "_整理报告"
REPORT_MARKER = ".archive-collect-organize.json"


class IntegrityError(ValueError):
    """写出结果与压缩包原内容不一致，必须停止后续写入。"""


def owned_report_roots(root):
    """只认带本工具标记的根级目录，不按目录名称排除用户资料。"""
    owned = []
    for path in Path(root).resolve().iterdir():
        if path.is_symlink() or not path.is_dir():
            continue
        if path.name != REPORT_DIR and not path.name.startswith(REPORT_DIR + "-"):
            continue
        try:
            marker = json.loads((path / REPORT_MARKER).read_text(encoding="utf-8"))
            if marker == {"owner": "archive-collect-organize", "schema": 1}:
                owned.append(path.resolve())
        except (OSError, ValueError):
            continue
    return owned


def report_root(root):
    root = Path(root).resolve()
    owned = owned_report_roots(root)
    if owned:
        return sorted(owned)[0]
    candidate = Path(unique_path(str(root / REPORT_DIR)))
    candidate.mkdir(exist_ok=False)
    with (candidate / REPORT_MARKER).open("x", encoding="utf-8") as f:
        json.dump({"owner": "archive-collect-organize", "schema": 1}, f)
    return candidate


def user_files(root):
    """枚举全部用户文件，只跳过已核实归属本工具的内部报告根目录。"""
    internal = set(owned_report_roots(root))
    def fail(exc):
        raise exc
    for folder, dirs, files in os.walk(root, onerror=fail):
        dirs[:] = [d for d in dirs if (Path(folder) / d).resolve() not in internal]
        for name in files:
            yield Path(folder) / name


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
    """未标UTF-8的文件名先尝试UTF-8字节，再尝试GBK字节。"""
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


def safe_target(root, rel):
    """拒绝越界路径与Windows非法名称；不改写合法的中文文件名。"""
    rel = str(rel).replace("\\", "/")
    parts = rel.split("/")
    if PureWindowsPath(rel).drive or any(
        p in ("", ".", "..") or p.endswith((" ", "."))
        or any(c in p for c in '<>:"|?*') or os.path.isreserved(p)
        for p in parts
    ):
        raise ValueError("非法相对路径: " + rel)
    base = Path(root).resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("路径超出资料目录: " + rel)
    return target


def build_index(root):
    """完整相对目标路径 -> [(大小, 绝对路径)]；不跨目录比较文件名。"""
    idx = {}
    for path in user_files(root):
        p = str(path)
        key = os.path.relpath(p, root).replace("\\", "/").casefold()
        idx.setdefault(key, []).append((path.stat().st_size, p))
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
    archive_rel = zip_name.replace("\\", "/")
    spec = cfg["archives"].get(archive_rel, cfg["archives"].get(archive_rel.split("/")[-1]))
    parts = rel.split("/")
    if spec:
        parts = strip_prefix(parts, spec.get("strip", []))
        base = spec.get("target", "")
    else:
        base = parts[0] if len(parts) > 1 else ""
        parts = parts[1:] if len(parts) > 1 else parts
    if not parts:
        return None
    routed_by_rule = False
    for rule in cfg["rules"]:
        if any(rule["match_dir"] == p for p in parts[:-1]):
            base = rule["target"]
            routed_by_rule = True
            break
    result = "/".join([base] + parts) if base else "/".join(parts)
    if not spec and not routed_by_rule:
        parent, _, filename = archive_rel.rpartition("/")
        stem = os.path.splitext(filename)[0]
        if result.split("/")[0].casefold() != stem.casefold():
            result = stem + "/" + result
        if parent:
            result = parent + "/" + result
    return result


def sync(root, cfg, apply_):
    """只负责解压补缺，不能据此宣称业务归类已完成。预演也保留待写路径索引。"""
    root = os.path.abspath(root)
    idx = build_index(root)
    digests = {}
    report = {"root": root, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "zips": {},
              "copied": [], "conflicts": [], "failed": [], "stopped": False,
              "stage": "仅解压补缺，业务归类另行执行"}

    zips = sorted(str(p) for p in user_files(root) if p.suffix.lower() == ".zip")

    for zpath in zips:
        zip_name = os.path.basename(zpath)
        zip_id = os.path.relpath(zpath, root).replace("\\", "/")
        if zip_name in cfg.get("exclude", []) or zip_id in cfg.get("exclude", []):
            continue
        res = {"copied": 0, "skipped": 0, "conflicts": 0, "failed": 0}
        try:
            z = zipfile.ZipFile(zpath)
        except Exception as e:
            res["failed"] += 1
            report["failed"].append({"zip": zip_id, "reason": "解压失败: %s" % e})
            report["zips"][zip_id] = res
            continue
        with z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                rel = member_name(info).replace("\\", "/")
                segments = rel.split("/")
                if segments[0] == "__MACOSX" or segments[-1] == ".DS_Store" or segments[-1].startswith("._"):
                    res["skipped"] += 1
                    continue
                dst = None
                try:
                    safe_target(root, rel)
                    rel_target = route(zip_id, rel, cfg)
                    if not rel_target:
                        continue
                    dst = str(safe_target(root, rel_target))
                    if any(Path(dst).is_relative_to(p) for p in owned_report_roots(root)):
                        raise ValueError("解压目标进入内部报告区，请指定另一来源目录: " + rel_target)
                    h = hashlib.sha256()
                    with z.open(info) as src:
                        for block in iter(lambda: src.read(1 << 20), b""):
                            h.update(block)
                    digest = h.hexdigest()
                    key = os.path.relpath(dst, root).replace("\\", "/").casefold()
                    hits = idx.get(key, [])
                    existing = hits[0] if hits else None
                    counter = 1
                    # 同一路径才比较；内容不同则增加来源子目录，保持文件原名。
                    while hits:
                        size, exist = hits[0]
                        if exist not in digests:
                            digests[exist] = sha256(exist)
                        if size == info.file_size and digests[exist] == digest:
                            break
                        tag = os.path.splitext(zip_name)[0] + ("-%d" % counter if counter > 1 else "")
                        rel_dst = os.path.join(os.path.dirname(rel_target), "_同路径并存", tag, os.path.basename(rel_target))
                        dst = str(safe_target(root, rel_dst))
                        key = os.path.relpath(dst, root).replace("\\", "/").casefold()
                        hits = idx.get(key, [])
                        counter += 1
                    if hits:
                        res["skipped"] += 1
                        continue
                    zip_time = datetime(*info.date_time).strftime("%Y-%m-%d %H:%M:%S")
                    if existing:
                        report["conflicts"].append({"dir": os.path.dirname(rel_target) or ".",
                            "existing": existing[1], "added": os.path.relpath(dst, root),
                            "size_existing": existing[0], "size_added": info.file_size, "zip_time": zip_time})
                        res["conflicts"] += 1
                    if apply_:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        with z.open(info) as src, open(dst, "xb") as output:
                            shutil.copyfileobj(src, output)
                        moment = datetime(*info.date_time).timestamp()
                        os.utime(dst, (moment, moment))
                        if sha256(dst) != digest:
                            raise IntegrityError("解压回读核验不一致: " + rel_target)
                    report["copied"].append({"path": os.path.relpath(dst, root), "size": info.file_size,
                        "zip": zip_id, "source": rel, "zip_time": zip_time,
                        "sha256": digest, "written": bool(apply_)})
                    idx[key] = [(info.file_size, dst)]
                    digests[dst] = digest
                    res["copied"] += 1
                except Exception as ex:
                    res["failed"] += 1
                    report["failed"].append({"zip": zip_id, "file": rel, "target": dst, "reason": str(ex)})
                    if isinstance(ex, (IntegrityError, OSError, zipfile.BadZipFile, RuntimeError)):
                        report["stopped"] = True
                        break
        report["zips"][zip_id] = res
        if report["stopped"]:
            break
    write_report(root, report)
    return report


def write_report(root, report):
    outdir = str(report_root(root))
    out = unique_path(os.path.join(outdir, "archive_sync.json"))
    report["report_path"] = out
    with open(out, "x", encoding="utf-8") as f:
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
                dst_dir = str(safe_target(root, target))
                dst = os.path.join(dst_dir, f)
                if os.path.exists(dst):
                    raise FileExistsError("归位目标已存在，请增加来源子目录，不改文件名: " + dst)
                moved.append({"from": f, "to": os.path.relpath(dst, root), "written": bool(apply_)})
                break
    if apply_:
        for item in moved:
            dst = str(safe_target(root, item["to"]))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(os.path.join(root, item["from"]), dst)
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
    if a.scatter and not report.get("stopped", False) and not report["failed"]:
        report["scattered"] = scatter(root, cfg, a.apply)
        write_report(root, report)
    out = report["report_path"]
    print("压缩包 %d 个，待复制 %d，已存在跳过 %d，冲突 %d，失败 %d"
          % (len(report["zips"]), len(report["copied"]),
             sum(z["skipped"] for z in report["zips"].values()),
             len(report["conflicts"]), len(report["failed"])))
    print("报告:", out)
    if not a.apply:
        print("（预演仅写报告，未改动资料文件；加 --apply 执行。解压后仍需业务归类。）")
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
