"""Очистка рабочих каталогов: выгрузки (export/), применённые и убранные пакеты inbox, бэкапы.

Каждая запись — то, что удаляется целиком:
  export    — комплект частей одной выгрузки export/<проект>/<вид>-<проект>-<id>-part-NNN.txt;
  applied   — каталог inbox/applied/<время>-<id>/ (файлы применённого пакета);
  discarded — каталог inbox/discarded/<время>-<id>/;
  backups   — бэкап backups/<проект>/<id>/ (после удаления откатить это применение нельзя).
"""

import json
import os
import re
import shutil

from . import TransferError, formats, inbox

AREAS = ("export", "applied", "discarded", "backups")
_PART_RE = re.compile(r"^(?P<prefix>(?P<kind>changes|snapshot)-.+)-part-\d{3}\.txt$")


def _roots(cfg):
    return {"export": cfg.export_dir, "applied": os.path.join(cfg.inbox_dir, inbox.APPLIED),
            "discarded": os.path.join(cfg.inbox_dir, inbox.DISCARDED), "backups": cfg.backups_dir}


def _dirs(path):
    if not os.path.isdir(path):
        return []
    return sorted(n for n in os.listdir(path) if not n.startswith(".") and os.path.isdir(os.path.join(path, n)))


def _tree_size(path):
    total, count = 0, 0
    for dirpath, _, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
                count += 1
            except OSError:
                pass
    return total, count


def _header(path):
    try:
        with open(path, "rb") as fh:
            return formats.parse_header(fh.readline())
    except (OSError, TransferError):
        return None


def _export_items(root):
    items = []
    for project_dir in _dirs(root):
        d = os.path.join(root, project_dir)
        groups = {}
        for name in sorted(os.listdir(d)):
            path = os.path.join(d, name)
            if name.startswith(".") or not os.path.isfile(path):
                continue
            m = _PART_RE.match(name)
            key = m.group("prefix") if m else name
            g = groups.setdefault(key, {"area": "export", "key": project_dir + "/" + key, "project": project_dir,
                                        "kind": m.group("kind") if m else None, "id": None, "files": 0,
                                        "bytes": 0, "mtime": 0})
            st = os.stat(path)
            g["files"] += 1
            g["bytes"] += st.st_size
            g["mtime"] = max(g["mtime"], st.st_mtime)
            if g["id"] is None:
                hdr = _header(path)
                if hdr:
                    g["project"], g["kind"], g["id"] = hdr["project"], hdr["kind"], hdr["id"]
        items.extend(groups.values())
    return items


def _inbox_items(area, root):
    items = []
    for name in _dirs(root):
        path = os.path.join(root, name)
        size, count = _tree_size(path)
        item = {"area": area, "key": name, "project": None, "kind": None, "id": None, "files": count,
                "bytes": size, "mtime": os.path.getmtime(path)}
        for f in sorted(os.listdir(path)):
            hdr = _header(os.path.join(path, f))
            if hdr:
                item.update(project=hdr["project"], kind=hdr["kind"], id=hdr["id"])
                break
        items.append(item)
    return items


def _backup_items(root):
    items = []
    for project_dir in _dirs(root):
        for bid in _dirs(os.path.join(root, project_dir)):
            path = os.path.join(root, project_dir, bid)
            meta = {}
            try:
                with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
                    meta = json.load(fh)
            except (OSError, ValueError):
                pass
            size, count = _tree_size(path)
            items.append({"area": "backups", "key": project_dir + "/" + bid, "project": meta.get("project") or project_dir,
                          "kind": meta.get("kind"), "id": bid, "files": count, "bytes": size,
                          "mtime": os.path.getmtime(path), "created_at": meta.get("created_at"),
                          "restored_at": meta.get("restored_at"), "package": meta.get("package")})
    return items


def list_items(cfg):
    roots = _roots(cfg)
    items = _export_items(roots["export"]) + _inbox_items("applied", roots["applied"]) \
        + _inbox_items("discarded", roots["discarded"]) + _backup_items(roots["backups"])
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return {"dirs": roots, "items": items}


def _check_key(key, depth):
    parts = (key or "").split("/")
    if len(parts) != depth or any(not p or p.startswith(".") or "\\" in p or "\x00" in p for p in parts):
        raise TransferError("некорректный ключ: %r" % key)
    return parts


def delete(cfg, items):
    """items: [{"area", "key"}]. Возвращает {"removed": [...], "bytes": освобождено, "errors": [...]}."""
    roots = _roots(cfg)
    removed, errors, freed = [], [], 0
    for item in items or []:
        area, key = item.get("area"), item.get("key")
        try:
            if area not in AREAS:
                raise TransferError("неизвестная область: %r" % area)
            root = roots[area]
            if area == "export":
                project_dir, prefix = _check_key(key, 2)
                d = os.path.join(root, project_dir)
                names = [n for n in (os.listdir(d) if os.path.isdir(d) else [])
                         if n == prefix or (n.startswith(prefix + "-part-") and _PART_RE.match(n))]
                if not names:
                    raise TransferError("нет файлов")
                for n in names:
                    path = os.path.join(d, n)
                    freed += os.path.getsize(path)
                    os.remove(path)
                if not os.listdir(d):
                    os.rmdir(d)
            else:
                parts = _check_key(key, 2 if area == "backups" else 1)
                path = os.path.join(root, *parts)
                if not os.path.isdir(path) or os.path.islink(path):
                    raise TransferError("каталог не найден")
                freed += _tree_size(path)[0]
                shutil.rmtree(path)
                if area == "backups":
                    parent = os.path.dirname(path)
                    if not os.listdir(parent):
                        os.rmdir(parent)
            removed.append({"area": area, "key": key})
        except (TransferError, OSError) as exc:
            errors.append("%s/%s: %s" % (area, key, exc))
    return {"removed": removed, "bytes": freed, "errors": errors}
