"""project-copy: снимок проекта целиком.

Состав снимка:
  - проект в git: всё, что видит git — отслеживаемые и новые неигнорируемые файлы (ровно то, что
    попало бы в коммит; build/, .gradle/, .idea/ и локальные секреты отсекает .gitignore);
  - проект без git: все файлы, кроме служебных каталогов сборки/IDE. Здесь .gitignore не работает —
    проверьте, не уедут ли локальные секреты (.env, application-local.yml).

Файлы кладутся в tar.gz как есть — с правами (gradlew остаётся исполняемым), симлинками,
кодировкой и переводами строк. Бинарные файлы (есть NUL-байт) по умолчанию НЕ переносятся:
в манифесте они помечаются KEEP, и при применении не удаляются.

Для проекта в git в снимок добавляется история последних коммитов (см. history.py).
"""

import io
import json
import os
import tarfile
import time

from . import TransferError, formats, fsutil, gitutil, history, projects


def _collect(cfg, project, with_binaries):
    root = projects.existing_project(cfg, project)
    mode = fsutil.detect_mode(root)
    self_project = projects.is_self(cfg, root)
    files, keep, total = [], [], 0
    for rel in fsutil.list_project_files(root, mode, self_project):
        full = os.path.join(root, rel)
        if not os.path.islink(full):
            if not with_binaries and fsutil.is_binary(full):
                keep.append(rel)
                continue
            total += os.path.getsize(full)
        files.append(rel)
    warnings = []
    if mode == "find":
        warnings.append("проект не в git — проверьте, что в снимок не попали локальные секреты (.env и т.п.)")
    if not files:
        raise TransferError("не найдено ни одного файла для снимка (режим: %s)" % mode)
    return root, mode, files, keep, total, warnings


def _history_limit(cfg, limit):
    return cfg.history_commits if limit is None else max(0, min(int(limit), 1000))


def preview(cfg, project, with_binaries=False, history_limit=None):
    root, mode, files, keep, total, warnings = _collect(cfg, project, with_binaries)
    commits = []
    limit = _history_limit(cfg, history_limit)
    if mode == "git" and limit and gitutil.has_own_repo(root) and gitutil.resolve_commit(root, "HEAD"):
        shas = gitutil.first_parent_history(root, limit)
        info = gitutil.commits_info(root, shas)
        commits = [info[sha] for sha in reversed(shas) if sha in info]
    return {"project": project, "mode": mode, "files": len(files), "bytes": total, "keep": keep,
            "warnings": warnings, "file_list": files[:2000], "history": commits}


def _reset_owner(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _add_bytes(tar, name, data, mtime):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def build_archive(root, project, mode, files, keep, history_data=None, history_members=None):
    manifest = ["# project snapshot manifest", "FORMAT\t2", "SOURCE\t%s" % mode, "PROJECT\t%s" % project,
                "CREATED\t%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if mode == "git":
        branch = gitutil.current_branch(root)
        head = gitutil.resolve_commit(root, "HEAD")
        if branch and not branch.startswith("("):
            manifest.append("BRANCH\t%s" % branch)
        if head:
            manifest.append("HEAD\t%s" % head)
    if history_data:
        manifest.append("HISTORY\t%d" % len(history_data["commits"]))
    manifest += ["FILE\t%s" % rel for rel in files]
    manifest += ["KEEP\t%s" % rel for rel in keep]
    manifest_bytes = ("\n".join(manifest) + "\n").encode("utf-8", "surrogateescape")

    buf = io.BytesIO()
    now = int(time.time())
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        _add_bytes(tar, "manifest.txt", manifest_bytes, now)
        for rel in files:
            tar.add(os.path.join(root, rel), arcname="files/" + rel, recursive=False, filter=_reset_owner)
        if history_data:
            _add_bytes(tar, history.MANIFEST, json.dumps(history_data, ensure_ascii=False, indent=1).encode("utf-8"),
                       now)
            for name in sorted(history_members or {}):
                _add_bytes(tar, name, history_members[name], now)
    return buf.getvalue()


def copy_project(cfg, project, with_binaries=False, history_limit=None):
    root, mode, files, keep, total, warnings = _collect(cfg, project, with_binaries)
    history_data, history_members, history_warnings = history.collect(
        root, files, _history_limit(cfg, history_limit), with_binaries, projects.is_self(cfg, root))
    warnings += history_warnings
    archive = build_archive(root, project, mode, files, keep, history_data, history_members)
    pkg_id = formats.new_package_id()
    payload = formats.encode_base64(archive)
    parts = formats.make_parts("snapshot", project, pkg_id, payload, formats.sha256(archive), cfg.max_lines)
    written = formats.write_parts(cfg.export_dir, project, "snapshot", pkg_id, parts)
    return {"kind": "snapshot", "project": project, "id": pkg_id, "parts": written, "mode": mode,
            "files": len(files), "bytes": total, "archive_bytes": len(archive), "keep": keep,
            "warnings": warnings, "history": history.summary(history_data)}
