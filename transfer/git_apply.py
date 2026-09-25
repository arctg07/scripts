"""git-apply: применение выгрузки git-copy (changed_classes.txt) к проекту.

Каждый блок FILE создаёт или перезаписывает файл, пути из секции DELETED FILES удаляются.
"""

import os

from . import TransferError, formats, fsutil, gitutil, projects
from .backup import Backup


def commit_message(meta, package_id):
    commits = meta.get("commits") or []
    if len(commits) == 1:
        title = commits[0]["subject"] or "Перенос коммита %s" % commits[0]["sha"]
    elif commits:
        title = "Перенос %d коммитов" % len(commits)
    else:
        title = "Перенос изменений"
    body = ["%s %s" % (c["sha"], c["subject"]) for c in commits] if len(commits) > 1 else []
    source = meta.get("project")
    if source or package_id:
        body.append("")
        body.append("Transfer: %s %s" % (source or "", package_id or ""))
    return "\n".join([title, ""] + body).strip() + "\n"


def apply_changes(cfg, payload, target_name, dry_run=False, commit=False, package_id=None):
    parsed = formats.parse_changes(payload)
    meta = parsed["meta"]
    target = projects.project_path(cfg, target_name)
    if not os.path.isdir(target):
        raise TransferError("проект не найден: %s — изменения коммитов применяются только к существующему "
                            "проекту (для нового проекта нужен снимок целиком)" % target)
    if not parsed["files"] and not parsed["deleted"]:
        raise TransferError("в выгрузке нет ни файлов, ни удалений — это точно выгрузка git-copy?")

    report = {"kind": "changes", "project": meta["project"], "target": target_name, "path": target,
              "create": False, "dry_run": dry_run, "new": [], "updated": [], "unchanged": 0, "deleted": [],
              "absent": [], "commits": meta["commits"], "skipped": meta["skipped"], "warnings": [],
              "error": None, "blocked": False, "backup": None, "git": None}
    if meta["project"] and meta["project"] != target_name:
        report["warnings"].append("выгрузка сделана с проекта %s, применяется к %s" % (meta["project"], target_name))
    for s in meta["skipped"]:
        report["warnings"].append("не перенесено: %s" % s)
    if gitutil.is_work_tree(target):
        dirty = gitutil.status_count(target)
        if dirty:
            report["warnings"].append("в проекте есть незакоммиченные изменения (%d)" % dirty)

    writes = []
    for path, content in parsed["files"]:
        formats.safe_rel_path(path)
        want_exec = path in meta["exec"]
        kind, data, is_exec = fsutil.read_current(target, path)
        if kind == "missing":
            report["new"].append(path)
        elif kind == "file" and data == content and (is_exec or not want_exec):
            report["unchanged"] += 1
            continue
        else:
            report["updated"].append(path)
        writes.append((path, content, want_exec, kind))
    deletes = []
    for path in parsed["deleted"]:
        formats.safe_rel_path(path)
        if os.path.lexists(os.path.join(target, path)):
            report["deleted"].append(path)
            deletes.append(path)
        else:
            report["absent"].append(path)

    if dry_run:
        return report

    backup = Backup(cfg, target_name, target, "changes", package_id)
    try:
        for path, content, want_exec, kind in writes:
            backup.before_write(path)
            mode = None
            if kind == "link":
                os.remove(os.path.join(target, path))
            if want_exec:
                cur = os.stat(os.path.join(target, path)).st_mode & 0o777 if kind == "file" else 0o666 & ~fsutil._UMASK
                mode = cur | ((cur & 0o444) >> 2)
            fsutil.write_file(target, path, content, mode=mode)
        for path in deletes:
            backup.before_delete(path)
            fsutil.remove_file(target, path)
    finally:
        report["backup"] = backup.finish()

    if commit and gitutil.is_work_tree(target):
        touched = report["new"] + report["updated"] + report["deleted"]
        sha, warn = gitutil.commit_paths(target, touched, commit_message(meta, package_id))
        report["git"] = {"init": False, "commit": sha}
        if warn:
            report["warnings"].append(warn)
    return report
