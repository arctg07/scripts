"""inbox — входящие выгрузки.

Файлы можно называть как угодно и класть в любом порядке: части распознаются по заголовкам и
собираются в «пакеты». В одном файле может лежать несколько частей подряд.
"""

import os
import re
import shutil
import time

from . import TransferError, formats, projects

APPLIED = "applied"
DISCARDED = "discarded"


def _source_files(cfg):
    d = cfg.inbox_dir
    if not os.path.isdir(d):
        return []
    names = [n for n in os.listdir(d) if not n.startswith(".") and os.path.isfile(os.path.join(d, n))]
    return sorted(os.path.join(d, n) for n in names)


def _read_items(paths):
    items = []
    for path in paths:
        with open(path, "rb") as fh:
            data = fh.read()
        for item in formats.scan_file(data, os.path.basename(path)):
            item["path"] = path
            items.append(item)
    return items


def group(items):
    """Элементы разбора → пакеты (внутреннее представление, с данными частей)."""
    packages = {}

    def pkg(key, **init):
        if key not in packages:
            p = {"key": key, "kind": None, "project": None, "id": None, "legacy": False, "total": 0,
                 "parts": {}, "legacy_items": [], "paths": set(), "errors": [], "warnings": []}
            p.update(init)
            packages[key] = p
        return packages[key]

    for item in items:
        t = item["type"]
        if t in ("part", "error") and item.get("header"):
            h = item["header"]
            p = pkg("n:%s:%s" % (h["kind"], h["id"]), kind=h["kind"], project=h["project"], id=h["id"],
                    total=h["total"])
            p["paths"].add(item["path"])
            if t == "error":
                p["errors"].append(item["message"])
                continue
            prev = p["parts"].get(h["index"])
            if prev is not None and prev["body"] != item["body"]:
                p["errors"].append("часть %d/%d встречается дважды с разным содержимым" % (h["index"], h["total"]))
            p["parts"][h["index"]] = item
        elif t == "legacy-changes":
            p = pkg("lc:" + item["source"], kind="changes", legacy=True, id=item["source"], total=1)
            p["paths"].add(item["path"])
            p["legacy_items"].append(item)
        elif t == "legacy-snapshot":
            gid = item["group"] or item["source"]
            p = pkg("ls:" + gid, kind="snapshot", legacy=True, id=gid)
            p["paths"].add(item["path"])
            p["legacy_items"].append(item)
            p["total"] = len(p["legacy_items"])
        elif t == "warning":
            continue
        else:
            p = pkg("u:" + item["source"], id=item["source"])
            p["paths"].add(item["path"])
            p["errors"].append(item.get("message") or "не распознано: ни заголовка #@transfer, ни старого формата")
    for item in items:
        if item["type"] == "warning":
            for p in packages.values():
                if item["path"] in p["paths"]:
                    p["warnings"].append(item["message"])
    return packages


def payload(p):
    """(kind, данные): для changes — текст выгрузки, для snapshot — tar.gz."""
    if p["kind"] is None:
        raise TransferError("; ".join(p["errors"]) or "не распознано")
    if p["legacy"]:
        items = sorted(p["legacy_items"], key=lambda i: (i["order"] if "order" in i else 0, i["source"]))
        data = b"".join(i["data"] if i["data"].endswith(b"\n") else i["data"] + b"\n" for i in items)
        if p["kind"] == "snapshot":
            data = formats.decode_base64(data)
        return p["kind"], data
    if p["errors"]:
        raise TransferError("; ".join(p["errors"]))
    return formats.assemble_parts(list(p["parts"].values()))


def summarize(cfg, p, verify=True):
    """Публичное описание пакета для UI/CLI."""
    present = sorted(p["parts"]) if not p["legacy"] else list(range(1, len(p["legacy_items"]) + 1))
    total = p["total"] or len(present)
    missing = [i for i in range(1, total + 1) if i not in present] if not p["legacy"] else []
    info = {"key": p["key"], "kind": p["kind"], "project": p["project"], "id": p["id"], "legacy": p["legacy"],
            "parts_total": total, "parts_present": present, "missing": missing,
            "files": sorted(os.path.basename(x) for x in p["paths"]),
            "errors": list(p["errors"]), "warnings": list(p["warnings"]),
            "mtime": max(os.path.getmtime(x) for x in p["paths"]) if p["paths"] else 0,
            "status": "ready"}
    if p["kind"] is None:
        info["status"] = "unknown"
    elif info["errors"]:
        info["status"] = "error"
    elif missing:
        info["status"] = "incomplete"
        info["errors"].append("не хватает частей: %s из %d" % (", ".join(map(str, missing)), total))
    elif verify:
        try:
            kind, data = payload(p)
            if kind == "snapshot":
                from .project_apply import Snapshot
                snap = Snapshot(data)
                info["project"] = info["project"] or snap.project
                info["snapshot_files"] = len(snap.files)
            else:
                parsed = formats.parse_changes(data)
                info["project"] = info["project"] or parsed["meta"]["project"]
                info["changes_files"] = len(parsed["files"])
                info["changes_deleted"] = len(parsed["deleted"])
                info["commits"] = parsed["meta"]["commits"]
        except TransferError as exc:
            info["status"] = "error"
            info["errors"].append(str(exc))
    if info["project"]:
        local = cfg.local_name(info["project"])
        info["local_project"] = local
        try:
            info["target_exists"] = os.path.isdir(projects.project_path(cfg, local))
        except TransferError:
            info["target_exists"] = False
    return info


def scan(cfg, verify=True):
    packages = group(_read_items(_source_files(cfg)))
    result = [summarize(cfg, p, verify) for p in packages.values()]
    result.sort(key=lambda i: i["mtime"], reverse=True)
    return result


def load(cfg, key):
    packages = group(_read_items(_source_files(cfg)))
    if key not in packages:
        raise TransferError("пакет не найден в inbox: %s" % key)
    return packages[key]


def load_files(paths):
    """Пакеты из явно переданных файлов (CLI)."""
    for path in paths:
        if not os.path.isfile(path):
            raise TransferError("файл не найден: %s" % path)
    return group(_read_items(paths))


def save_text(cfg, data, name=None):
    if not data or not data.strip():
        raise TransferError("пустой текст")
    if not os.path.isdir(cfg.inbox_dir):
        os.makedirs(cfg.inbox_dir)
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", name or "") or "paste"
    if not base.endswith(".txt"):
        base += ".txt"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(cfg.inbox_dir, "%s-%s" % (stamp, base))
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(cfg.inbox_dir, "%s-%d-%s" % (stamp, n, base))
    with open(path, "wb") as fh:
        fh.write(data)
    return os.path.basename(path)


def move_package(cfg, p, sub):
    """Переносит файлы пакета в inbox/<sub>/<время>-<id>/ (applied или discarded)."""
    dest = os.path.join(cfg.inbox_dir, sub, "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"),
                                                       formats.safe_file_part(p["id"] or "package")))
    moved = []
    for path in sorted(p["paths"]):
        if os.path.dirname(os.path.abspath(path)) != os.path.abspath(cfg.inbox_dir) or not os.path.exists(path):
            continue
        if not os.path.isdir(dest):
            os.makedirs(dest)
        shutil.move(path, os.path.join(dest, os.path.basename(path)))
        moved.append(os.path.basename(path))
    return moved
