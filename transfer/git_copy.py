"""git-copy: выгрузка файлов из одного или нескольких коммитов.

Для каждого затронутого файла в выгрузку попадает его ПОЛНОЕ содержимое (не diff) на момент
самого позднего из выбранных коммитов; если там файл удалён — он попадает в DELETED FILES.
Коммиты обрабатываются в порядке истории (topo-order), для merge-коммита учитываются изменения
относительно первого родителя.

Важно: если между выбранными коммитами есть невыбранные, менявшие те же файлы, их правки тоже
окажутся в выгрузке — такие случаи перечисляются в предупреждениях.
"""

import time

from . import TransferError, formats, gitutil, projects

MODE_EXEC = "100755"
MODE_LINK = "120000"
MODE_GITLINK = "160000"
MAX_BETWEEN = 500


def expand_refs(repo, refs):
    """Ссылки (хеш, ветка, тег, HEAD~N, диапазон A..B) → хеши от старых к новым."""
    shas = []
    for ref in refs or ["HEAD"]:
        ref = ref.strip()
        gitutil.check_ref(ref)
        if ".." in ref:
            code, out, _ = gitutil.run_git(repo, ["rev-list", ref, "--"], check=False)
            if code != 0:
                raise TransferError("некорректный диапазон коммитов: %s" % ref)
            shas.extend(out.decode().split())
        else:
            sha = gitutil.resolve_commit(repo, ref)
            if not sha:
                raise TransferError("коммит не найден: %s" % ref)
            shas.append(sha)
    selected = set(shas)
    if not selected:
        raise TransferError("по заданным ссылкам не найдено ни одного коммита: %s" % " ".join(refs))
    # Сортировка по дате не годится: после rebase/cherry-pick у серии коммитов одинаковое время.
    ordered = gitutil.rev_list(repo, "--topo-order", "--reverse", "--stdin",
                               stdin=("\n".join(sorted(selected)) + "\n").encode())
    return [s for s in ordered if s in selected]


def plan_copy(repo, refs):
    """Что попадёт в выгрузку — без чтения содержимого файлов."""
    ordered = expand_refs(repo, refs)
    latest = {}  # путь -> (коммит, статус, режим, blob)
    for sha in ordered:
        for status, path, mode, blob in gitutil.diff_tree_raw(repo, sha):
            latest[path] = (sha, status, mode, blob)

    files, deleted, skipped = [], [], []
    for path in sorted(latest):
        sha, status, mode, blob = latest[path]
        if "\n" in path:
            skipped.append({"path": path, "reason": "перевод строки в имени файла"})
        elif status == "D":
            deleted.append(path)
        elif mode == MODE_LINK:
            skipped.append({"path": path, "reason": "симлинк"})
        elif mode == MODE_GITLINK:
            skipped.append({"path": path, "reason": "подмодуль"})
        else:
            files.append({"path": path, "status": "M" if status == "T" else status, "commit": sha,
                          "blob": blob, "exec": mode == MODE_EXEC})

    info = gitutil.commits_info(repo, ordered)
    commits = []
    for sha in ordered:
        c = info[sha]
        commits.append({"sha": sha, "short": c["short"], "subject": c["subject"], "time": c["time"],
                        "author": c["author"], "merge": c["merge"]})
    return {"commits": commits, "files": files, "deleted": deleted, "skipped": skipped,
            "between": find_unselected(repo, ordered, set(latest))}


def find_unselected(repo, ordered, paths):
    """Невыбранные коммиты между выбранными, которые меняли те же файлы."""
    if len(ordered) < 2 or not paths:
        return {"commits": [], "truncated": False}
    selected = set(ordered)
    oldest = ordered[0]
    stdin = "\n".join(ordered) + "\n"
    if gitutil.parent_count(repo, oldest) > 0:
        stdin += "^%s^@\n" % oldest
    candidates = [s for s in gitutil.rev_list(repo, "--stdin", stdin=stdin.encode()) if s not in selected]
    if len(candidates) > MAX_BETWEEN:
        return {"commits": [], "truncated": True, "count": len(candidates)}
    hits = []
    for sha in candidates:
        touched = sorted(set(gitutil.changed_paths(repo, sha)) & paths)
        if touched:
            hits.append((sha, touched))
    info = gitutil.commits_info(repo, [sha for sha, _ in hits])
    result = []
    for sha, touched in hits:
        c = info[sha]
        result.append({"sha": sha, "short": c["short"], "subject": c["subject"], "paths": touched[:50],
                       "count": len(touched)})
    return {"commits": result, "truncated": False}


def build_payload(repo, project, plan):
    """Нагрузка changes и список бинарных файлов, не попавших в неё."""
    files, skipped = [], list(plan["skipped"])
    meta = ["transfer: changes v1", "PROJECT: " + project, "CREATED: " + time.strftime("%Y-%m-%d %H:%M:%S")]
    for c in plan["commits"]:
        meta.append("COMMIT: %s %s" % (c["short"], c["subject"]))
    with gitutil.BlobReader(repo) as reader:
        for f in plan["files"]:
            content = reader.read(f["blob"])
            if b"\x00" in content:
                skipped.append({"path": f["path"], "reason": "бинарный"})
                continue
            files.append((f["path"], content))
            if f["exec"]:
                meta.append("EXEC: " + f["path"])
            base = content.rstrip(b"\n")
            eol = formats.eol_count(content)
            if eol != formats.default_eol(base):
                meta.append("EOL: %d %s" % (eol, f["path"]))
    for s in skipped:
        meta.append("SKIPPED: %s (%s)" % (s["path"], s["reason"]))
    return formats.build_changes(meta, files, plan["deleted"]), files, skipped


def copy_commits(cfg, project, refs):
    repo = projects.existing_project(cfg, project)
    if not gitutil.is_work_tree(repo):
        raise TransferError("проект %s — не git-репозиторий" % project)
    plan = plan_copy(repo, refs)
    payload, files, skipped = build_payload(repo, project, plan)
    pkg_id = formats.new_package_id()
    parts = formats.make_parts("changes", project, pkg_id, payload, formats.sha256(payload), cfg.max_lines)
    written = formats.write_parts(cfg.export_dir, project, "changes", pkg_id, parts)
    return {"kind": "changes", "project": project, "id": pkg_id, "parts": written,
            "commits": plan["commits"], "files": len(files), "deleted": len(plan["deleted"]),
            "skipped": skipped, "between": plan["between"]}
