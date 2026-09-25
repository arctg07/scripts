"""project-copy: снимок проекта целиком.

Состав снимка:
  - проект в git: всё, что видит git — отслеживаемые и новые неигнорируемые файлы (ровно то, что
    попало бы в коммит; build/, .gradle/, .idea/ и локальные секреты отсекает .gitignore);
  - проект без git: все файлы, кроме служебных каталогов сборки/IDE. Здесь .gitignore не работает —
    проверьте, не уедут ли локальные секреты (.env, application-local.yml).

Файлы кладутся в tar.gz как есть — с правами (gradlew остаётся исполняемым), симлинками,
кодировкой и переводами строк. Бинарные файлы (есть NUL-байт) по умолчанию НЕ переносятся:
в манифесте они помечаются KEEP, и при применении не удаляются.
"""

import io
import os
import tarfile
import time

from . import TransferError, formats, fsutil, gitutil, projects


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


def preview(cfg, project, with_binaries=False):
    root, mode, files, keep, total, warnings = _collect(cfg, project, with_binaries)
    return {"project": project, "mode": mode, "files": len(files), "bytes": total, "keep": keep,
            "warnings": warnings, "file_list": files[:2000]}


def _reset_owner(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def build_archive(root, project, mode, files, keep):
    manifest = ["# project snapshot manifest", "FORMAT\t2", "SOURCE\t%s" % mode, "PROJECT\t%s" % project,
                "CREATED\t%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if mode == "git":
        branch = gitutil.current_branch(root)
        head = gitutil.resolve_commit(root, "HEAD")
        if branch and not branch.startswith("("):
            manifest.append("BRANCH\t%s" % branch)
        if head:
            manifest.append("HEAD\t%s" % head)
    manifest += ["FILE\t%s" % rel for rel in files]
    manifest += ["KEEP\t%s" % rel for rel in keep]
    manifest_bytes = ("\n".join(manifest) + "\n").encode("utf-8", "surrogateescape")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.txt")
        info.size = len(manifest_bytes)
        info.mtime = int(time.time())
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(manifest_bytes))
        for rel in files:
            tar.add(os.path.join(root, rel), arcname="files/" + rel, recursive=False, filter=_reset_owner)
    return buf.getvalue()


def copy_project(cfg, project, with_binaries=False):
    root, mode, files, keep, total, warnings = _collect(cfg, project, with_binaries)
    archive = build_archive(root, project, mode, files, keep)
    pkg_id = formats.new_package_id()
    payload = formats.encode_base64(archive)
    parts = formats.make_parts("snapshot", project, pkg_id, payload, formats.sha256(archive), cfg.max_lines)
    written = formats.write_parts(cfg.export_dir, project, "snapshot", pkg_id, parts)
    return {"kind": "snapshot", "project": project, "id": pkg_id, "parts": written, "mode": mode,
            "files": len(files), "bytes": total, "archive_bytes": len(archive), "keep": keep,
            "warnings": warnings}
