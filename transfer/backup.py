"""Бэкап перед применением и откат.

backups/<проект>/<id>/
  meta.json   — что создано, изменено и удалено
  files/...   — прежние версии изменённых и удалённых файлов
"""

import json
import os
import shutil
import time

from . import TransferError, fsutil, gitutil, projects
from .formats import new_package_id, safe_file_part


class Backup(object):
    def __init__(self, cfg, project, target, kind, package_id, project_created=False):
        self.cfg = cfg
        self.project = project
        self.target = target
        self.id = new_package_id()
        self.dir = os.path.join(cfg.backups_dir, safe_file_part(project), self.id)
        self.meta = {"id": self.id, "project": project, "target": target, "kind": kind,
                     "package": package_id, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "project_created": project_created, "created": [], "modified": [], "deleted": [],
                     "restored_at": None}
        self._seen = set()

    def _save(self, rel):
        src = os.path.join(self.target, rel)
        dst = os.path.join(self.dir, "files", rel)
        if not os.path.isdir(os.path.dirname(dst)):
            os.makedirs(os.path.dirname(dst))
        if os.path.islink(src):
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)

    def before_write(self, rel):
        if rel in self._seen:
            return
        self._seen.add(rel)
        if os.path.lexists(os.path.join(self.target, rel)):
            self._save(rel)
            self.meta["modified"].append(rel)
        else:
            self.meta["created"].append(rel)

    def before_delete(self, rel):
        if rel in self._seen:
            return
        self._seen.add(rel)
        self._save(rel)
        self.meta["deleted"].append(rel)

    def finish(self):
        """Сохраняет meta.json; пустой бэкап не создаётся. Возвращает id или None."""
        if not self._seen:
            return None
        if not os.path.isdir(self.dir):
            os.makedirs(self.dir)
        _write_meta(self.dir, self.meta)
        _prune(os.path.dirname(self.dir), self.cfg.keep_backups)
        return self.id


def _write_meta(path, meta):
    with open(os.path.join(path, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)


def _prune(project_dir, keep):
    entries = sorted(e for e in os.listdir(project_dir) if os.path.isfile(os.path.join(project_dir, e, "meta.json")))
    for old in entries[:-keep]:
        shutil.rmtree(os.path.join(project_dir, old), ignore_errors=True)


def list_backups(cfg, project=None):
    result = []
    root = cfg.backups_dir
    if not os.path.isdir(root):
        return result
    for pdir in os.listdir(root):
        if project and pdir != safe_file_part(project):
            continue
        base = os.path.join(root, pdir)
        if not os.path.isdir(base):
            continue
        for bid in os.listdir(base):
            meta_path = os.path.join(base, bid, "meta.json")
            if not os.path.isfile(meta_path):
                continue
            with open(meta_path, encoding="utf-8") as fh:
                meta = json.load(fh)
            meta["counts"] = {k: len(meta.get(k) or []) for k in ("created", "modified", "deleted")}
            for k in ("created", "modified", "deleted"):
                meta[k] = (meta.get(k) or [])[:500]
            meta["dir"] = os.path.join(base, bid)
            result.append(meta)
    result.sort(key=lambda m: m["id"], reverse=True)
    return result


def _find(cfg, backup_id):
    if not backup_id or "/" in backup_id or backup_id.startswith("."):
        raise TransferError("некорректный id бэкапа")
    root = cfg.backups_dir
    for pdir in os.listdir(root) if os.path.isdir(root) else []:
        path = os.path.join(root, pdir, backup_id)
        if os.path.isfile(os.path.join(path, "meta.json")):
            with open(os.path.join(path, "meta.json"), encoding="utf-8") as fh:
                return path, json.load(fh)
    raise TransferError("бэкап не найден: %s" % backup_id)


def restore(cfg, backup_id):
    """Возвращает проект к состоянию до применения: удаляет созданные файлы, возвращает прежние."""
    path, meta = _find(cfg, backup_id)
    if meta.get("project_created"):
        raise TransferError("проект %s был создан этим применением — откат не выполняется; "
                            "если он не нужен, удалите папку вручную" % meta["project"])
    if meta.get("restored_at"):
        raise TransferError("этот бэкап уже откатан (%s)" % meta["restored_at"])
    target = meta["target"]
    if not os.path.isdir(target):
        raise TransferError("проект не найден: %s" % target)
    # Бэкап пишется только для проекта в корне — защищает от подменённого meta.json.
    projects.validate_name(os.path.basename(target))
    if os.path.realpath(os.path.dirname(target)) != os.path.realpath(cfg.projects_root):
        raise TransferError("проект бэкапа вне корня проектов: %s" % target)

    removed, restored = [], []
    for rel in meta.get("created") or []:
        if fsutil.remove_file(target, rel):
            removed.append(rel)
    for rel in (meta.get("modified") or []) + (meta.get("deleted") or []):
        src = os.path.join(path, "files", rel)
        if os.path.islink(src):
            fsutil.write_symlink(target, rel, os.readlink(src))
        elif os.path.isfile(src):
            dst = fsutil.ensure_parent(target, rel)
            if os.path.lexists(dst) and (os.path.islink(dst) or not os.path.isfile(dst)):
                os.remove(dst)
            shutil.copy2(src, dst)
        else:
            continue
        restored.append(rel)
    git_note = _restore_git(target, meta["git"]) if meta.get("git") else None
    meta["restored_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_meta(path, meta)
    return {"id": backup_id, "project": meta["project"], "target": target,
            "removed": removed, "restored": restored, "git": git_note}


def _restore_git(target, info):
    """Возвращает HEAD и индекс на место, если применение переключило проект на новую ветку.

    Сама ветка остаётся (удалить: git branch -D <ветка>).
    """
    branch = info["branch"]
    short = branch.replace("refs/heads/", "", 1)
    code, out, _ = gitutil.run_git(target, ["symbolic-ref", "-q", "HEAD"], check=False)
    current = out.decode("utf-8", "replace").strip() if code == 0 else None
    if current != branch or gitutil.resolve_commit(target, "HEAD") != info["tip"]:
        return "HEAD уже не на ветке %s в состоянии после применения — git не менялся" % short
    if info.get("prev_ref"):
        gitutil.run_git(target, ["symbolic-ref", "HEAD", info["prev_ref"]])
    else:
        gitutil.run_git(target, ["update-ref", "--no-deref", "HEAD", info["prev_head"]])
    if info.get("prev_head"):
        gitutil.run_git(target, ["reset", "-q"])
    else:
        gitutil.run_git(target, ["read-tree", "--empty"])
    prev = (info.get("prev_ref") or "").replace("refs/heads/", "", 1) or (info.get("prev_head") or "")[:10]
    return "HEAD возвращён на %s; ветка %s оставлена (удалить: git branch -D %s)" % (prev, short, short)
