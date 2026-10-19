"""История коммитов в снимке проекта.

Копирование: к файлам снимка добавляются патчи последних N коммитов HEAD (по первому родителю) и патч
незакоммиченных правок (рабочий каталог относительно HEAD). Патчи — обычный `git diff --binary`,
метаданные (автор, коммиттер, даты, сообщение) — в history.json.

Применение: дерево снимка собирается в отдельном индексе, после чего патчи откатываются в обратном
порядке — так получаются деревья каждого коммита, не зависящие от состояния целевого проекта. Затем
поверх текущего HEAD создаются коммиты: «состояние до истории» (если проект отличается от источника
перед первым коммитом), сами коммиты с исходными авторами, датами и сообщениями (хеши — новые) и
незакоммиченные правки источника. Коммиты, которые в проекте уже есть (дерево HEAD совпало с деревом
одного из коммитов), пропускаются.

Пути, которых нет в снимке (бинарные без --with-binaries, локальные настройки IDE, защищённые файлы),
в патчи не попадают: в воссозданной истории они не меняются.
"""

import os

from . import fsutil, gitutil, progress

VERSION = 1
MANIFEST = "history.json"


# --- Копирование -----------------------------------------------------------------------------------

def collect(root, files, limit, with_binaries, self_project):
    """(данные для history.json или None, {имя в архиве: bytes}, предупреждения)."""
    if limit <= 0 or not gitutil.is_work_tree(root):
        return None, {}, []
    if not gitutil.has_own_repo(root):
        return None, {}, ["история не добавлена: проект лежит внутри другого git-репозитория"]
    head = gitutil.resolve_commit(root, "HEAD")
    if not head:
        return None, {}, []

    empty = gitutil.empty_tree(root)
    chain = gitutil.first_parent_log(root, limit)
    progress.log("История: %d коммитов (запрошено %d)" % (len(chain), limit))
    stats = []
    for i, c in enumerate(chain):
        progress.step("Изменённые файлы коммитов истории", i, len(chain))
        stats.append(gitutil.diff_numstat(root, c["parent"] or empty, c["sha"]))
    with progress.timed("Незакоммиченные изменения (git diff HEAD)"):
        worktree_stat = gitutil.diff_numstat(root, "HEAD")
    progress.log("незакоммиченных изменений: %d файлов" % len(worktree_stat))

    # Путь исключается целиком (из всех патчей), если его содержимое в снимке не совпадёт с источником.
    files_set = set(files)
    exclude = set()
    for stat in stats + [worktree_stat]:
        for path, binary in stat:
            exists = os.path.lexists(os.path.join(root, path))
            if (binary and not with_binaries) or (exists and path not in files_set) \
                    or fsutil.is_local_ide_file(path) or fsutil.is_protected(path, self_project):
                exclude.add(path)

    if exclude:
        progress.log("из патчей истории исключено путей: %d (бинарные, IDE, вне снимка)" % len(exclude))

    members, commits = {}, []
    for i, (c, stat) in enumerate(zip(chain, stats), 1):
        progress.step("Патчи коммитов истории", i - 1, len(chain))
        sha, parent = c["sha"], c["parent"]
        entry = {"sha": sha, "parent": parent, "author": c["author"], "committer": c["committer"],
                 "message": c["message"]}
        subject = c["message"].split("\n", 1)[0][:80]
        if parent is None:
            # Корневой коммит только создаёт файлы: вместо патча со всем проектом — список путей.
            entry["paths"] = sorted(p for p, _ in stat if p not in exclude)
            progress.log("  %d/%d %s %s — корневой, %d путей" % (i, len(chain), sha[:10], subject, len(entry["paths"])))
        else:
            entry["patch"] = "history/%04d.patch" % i
            members[entry["patch"]] = gitutil.diff_patch(root, parent, sha, exclude)
            progress.log("  %d/%d %s %s — %d файлов, патч %s" % (
                i, len(chain), sha[:10], subject, len(stat), progress.fmt_size(len(members[entry["patch"]]))))
        commits.append(entry)
    progress.step("Патч незакоммиченных изменений", 0, 0)
    members["history/worktree.patch"] = gitutil.diff_patch(root, "HEAD", None, exclude)
    progress.log("история: %d патчей, %s" % (len(members), progress.fmt_size(sum(len(v) for v in members.values()))))

    tracked = set(os.fsdecode(p) for p in gitutil.git(root, "ls-files", "-z", "--cached").split(b"\x00") if p)
    data = {"version": VERSION, "head": head, "commits": commits, "worktree": "history/worktree.patch",
            "untracked": sorted(p for p in files if p not in tracked), "excluded": sorted(exclude)}
    return data, members, []


def summary(data):
    """Коммиты истории для UI и CLI: новые сверху."""
    result = []
    for c in reversed((data or {}).get("commits") or []):
        date = (c["author"][2] or "").split()
        result.append({"sha": c["sha"][:10], "full": c["sha"], "subject": c["message"].split("\n", 1)[0],
                       "author": c["author"][0], "time": int(date[0]) if date and date[0].isdigit() else 0})
    return result


# --- Применение ------------------------------------------------------------------------------------

def _history_trees(idx, snap, warnings):
    """Деревья после каждого коммита истории: trees[i] — после i-го (trees[0] — до первого).

    Возвращает (trees, first): деревья известны для индексов first..n. None — истории нет или её не
    удалось отделить от незакоммиченных правок.
    """
    data = snap.history
    commits = (data or {}).get("commits") or []
    if not commits:
        return None, 0
    idx.remove(data.get("untracked") or [])
    if not idx.apply(snap.read_extra(data["worktree"]), reverse=True):
        warnings.append("незакоммиченные правки снимка не удалось отделить от истории — история коммитов "
                        "не воссоздана, снимок записан одним коммитом")
        return None, 0
    n = len(commits)
    trees = [None] * (n + 1)
    trees[n] = idx.write_tree()
    first = 0
    for i in range(n, 0, -1):
        c = commits[i - 1]
        if "paths" in c:
            idx.remove(c["paths"])
            ok = True
        else:
            ok = idx.apply(snap.read_extra(c["patch"]), reverse=True)
        if not ok:
            first = i
            warnings.append("коммиты до %s включительно не удалось воссоздать — их изменения вошли в первый "
                            "коммит ветки" % c["sha"][:10])
            break
        trees[i - 1] = idx.write_tree()
    return trees, first


def commit_snapshot(repo, snap, parent, label, package_id, message=None):
    """Коммиты снимка поверх parent (None — первый коммит репозитория).

    message — сообщение коммита, если история не воссоздана и снимок записывается одним коммитом.

    Рабочий каталог уже содержит файлы снимка. Ни рабочий каталог, ни индекс, ни ссылки не меняются —
    возвращается вершина новой цепочки: {"tip", "commits", "reused", "warnings"}; tip == parent, если
    коммитить нечего.
    """
    ident = gitutil.local_identity(repo)
    if not ident:
        raise gitutil.GitError("в git не заданы user.name/user.email — коммиты не созданы "
                               "(git config --global user.name ...; git config --global user.email ...)")
    result = {"tip": parent, "commits": [], "reused": 0, "warnings": []}
    with gitutil.TempIndex(repo) as idx:
        idx.read_tree(parent)
        paths = set(snap.files)
        if parent:
            # Отслеживаемые файлы, которых нет в снимке: оставшиеся на диске (KEEP, настройки IDE) остаются
            # в дереве, удалённые применением — убираются.
            paths.update(gitutil.ls_tree_paths(repo, parent))
        idx.update_from_disk(sorted(paths))
        snap_tree = idx.write_tree()
        trees, first = _history_trees(idx, snap, result["warnings"])

    empty = gitutil.empty_tree(repo)
    parent_tree = gitutil.tree_of(repo, parent) if parent else empty
    tip = parent

    def add(tree, message, author, committer=None, source=None):
        sha = gitutil.commit_tree(repo, tree, [tip] if tip else [], message, author, committer)
        result["commits"].append({"sha": sha, "short": sha[:10], "subject": message.split("\n", 1)[0],
                                  "source": source})
        return sha

    last_tree = parent_tree
    commits = snap.history["commits"] if trees else []
    if trees:
        n = len(commits)
        present = [i for i in range(first, n + 1) if trees[i] == parent_tree]
        if present:
            start = max(present)
            result["reused"] = start
        else:
            start = first
            if parent or trees[first] != empty:
                before = commits[first]["parent"] or commits[first]["sha"]
                tip = add(trees[first], "Импорт %s: состояние на %s\n\nСнимок %s. Следующие коммиты воссозданы "
                                        "из истории %s.\n" % (label, before[:10], package_id or "без id", label),
                          ident)
        for i in range(start + 1, n + 1):
            c = commits[i - 1]
            tip = add(trees[i], c["message"], c["author"], c["committer"], source=c["sha"])
        last_tree = trees[n]

    if snap_tree != last_tree:
        if trees:
            message = "Импорт %s: незакоммиченные изменения (%s)\n" % (label, package_id or "без id")
        else:
            message = (message or "Применён снимок %s (%s)" % (label, package_id or "без id")) + "\n"
            listed = summary(snap.history)
            if listed:
                message += "\nПоследние коммиты источника:\n" + "".join(
                    "  %s %s\n" % (c["sha"], c["subject"]) for c in listed)
        tip = add(snap_tree, message, ident)
    result["tip"] = tip
    return result
