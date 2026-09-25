"""Проекты — каталоги в корне (там же, где лежит папка scripts)."""

import os

from . import TransferError, gitutil


def validate_name(name):
    if not name or not isinstance(name, str):
        raise TransferError("не указан проект")
    if name in (".", "..") or name.startswith(".") or any(c in name for c in "/\\\x00\n"):
        raise TransferError("некорректное имя проекта: %r" % name)
    return name


def project_path(cfg, name):
    return os.path.join(cfg.projects_root, validate_name(name))


def existing_project(cfg, name):
    path = project_path(cfg, name)
    if not os.path.isdir(path):
        raise TransferError("проект не найден: %s (%s)" % (name, path))
    return path


def is_self(cfg, path):
    """Это сама папка scripts (у неё свои служебные каталоги)."""
    return os.path.realpath(path) == os.path.realpath(cfg.scripts_dir)


def list_projects(cfg):
    root = cfg.projects_root
    if not os.path.isdir(root):
        raise TransferError("корень проектов не найден: %s" % root)
    result = []
    for name in sorted(os.listdir(root), key=lambda s: s.lower()):
        if name.startswith(".") or name in cfg.hidden_projects:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        result.append({"name": name, "git": gitutil.has_own_repo(path), "self": is_self(cfg, path)})
    return result


def project_info(cfg, name):
    path = project_path(cfg, name)
    info = {"name": name, "path": path, "exists": os.path.isdir(path), "git": False,
            "branch": None, "dirty": None, "head": None, "self": is_self(cfg, path)}
    if not info["exists"] or not gitutil.is_work_tree(path):
        return info
    info["git"] = True
    info["branch"] = gitutil.current_branch(path)
    info["dirty"] = gitutil.status_count(path)
    head = gitutil.resolve_commit(path, "HEAD")
    if head:
        commits = gitutil.log_commits(path, head, limit=1)
        info["head"] = commits[0] if commits else None
    return info
