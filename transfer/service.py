"""Операции верхнего уровня — общие для CLI и веб-UI."""

import threading

from . import TransferError, git_apply, git_copy, gitutil, inbox, project_apply, project_copy, projects

# Операции, меняющие файлы, выполняются по одной.
LOCK = threading.Lock()


def copy(cfg, project, mode, refs=None, ref=None, with_binaries=False):
    """mode: last — последний коммит ветки ref; commits — выбранные refs; project — снимок целиком."""
    with LOCK:
        if mode == "project":
            return project_copy.copy_project(cfg, project, with_binaries)
        return git_copy.copy_commits(cfg, project, _refs(mode, refs, ref))


def copy_preview(cfg, project, mode, refs=None, ref=None, with_binaries=False):
    if mode == "project":
        return project_copy.preview(cfg, project, with_binaries)
    repo = projects.existing_project(cfg, project)
    if not gitutil.is_work_tree(repo):
        raise TransferError("проект %s — не git-репозиторий" % project)
    plan = git_copy.plan_copy(repo, _refs(mode, refs, ref))
    for f in plan["files"]:
        f.pop("blob", None)
    return plan


def _refs(mode, refs, ref):
    if mode == "last":
        return [ref or "HEAD"]
    if mode == "commits":
        if not refs:
            raise TransferError("не выбрано ни одного коммита")
        return list(refs)
    raise TransferError("неизвестный режим копирования: %s" % mode)


def apply_package(cfg, package, target=None, dry_run=False, force=False, commit=False, init_git=True,
                  archive_after=True):
    """Применяет пакет inbox/файлов. target — имя локального проекта (по умолчанию из выгрузки)."""
    kind, data = inbox.payload(package)
    source_project = package.get("project")
    if not target:
        if kind == "snapshot" and not source_project:
            source_project = project_apply.Snapshot(data).project
        if kind == "changes" and not source_project:
            from .formats import parse_changes
            source_project = parse_changes(data)["meta"]["project"]
        if not source_project:
            raise TransferError("в выгрузке нет имени проекта (старый формат) — укажите проект явно")
        target = cfg.local_name(source_project)
    projects.validate_name(target)

    with LOCK:
        if kind == "snapshot":
            report = project_apply.apply_snapshot(cfg, data, target, dry_run=dry_run, force=force, commit=commit,
                                                  init_git=init_git, package_id=package.get("id"))
        else:
            report = git_apply.apply_changes(cfg, data, target, dry_run=dry_run, commit=commit,
                                             package_id=package.get("id"))
        report["package"] = package.get("key")
        if not dry_run and not report.get("error") and archive_after and package.get("key"):
            report["archived"] = inbox.move_package(cfg, package, inbox.APPLIED)
    return report
