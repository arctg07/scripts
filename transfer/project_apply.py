"""project-apply: восстановление проекта из снимка project-copy.

1) Файлы снимка записываются в проект: отсутствующие создаются, существующие перезаписываются
   (права вроде исполняемого gradlew сохраняются).
2) Удаляются неактуальные файлы — те, что есть в проекте, но отсутствуют в снимке. Кандидаты
   отбираются так же, как при копировании (через git или обходом без служебных каталогов).
   Не удаляются: бинарные файлы с пометкой KEEP, настройки IDE (.idea, *.iml, .vscode) и
   локальные настройки Claude Code. Если удалять предстоит подозрительно много файлов (снимок
   другого проекта, не тот target), применение останавливается — продолжить можно с force.
3) Если проекта нет — он создаётся, после записи выполняются git init и коммиты: история из снимка
   (см. history.py) или один стартовый коммит.
4) Для существующего проекта в git можно отвести ветку <имя>-ДД-ММ-ГГ от текущего HEAD: в неё
   записываются коммиты истории и незакоммиченные правки источника, HEAD переключается на неё.
   Рабочий каталог при этом уже содержит снимок и не меняется.
"""

import io
import json
import os
import tarfile
import time

from . import TransferError, fsutil, gitutil, history, projects
from .backup import Backup
from .formats import safe_rel_path


class Snapshot(object):
    def __init__(self, archive):
        try:
            self.tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
            members = self.tar.getmembers()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise TransferError("снимок повреждён или неполный: %s" % exc)
        self.members, self.extra = {}, {}
        manifest = None
        for m in members:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name == "manifest.txt":
                manifest = m
            elif name.startswith("files/") and (m.isfile() or m.issym() or m.islnk()):
                self.members[fsutil.nfc(name[6:])] = m
            elif (name == history.MANIFEST or name.startswith("history/")) and m.isfile():
                self.extra[name] = m
        if manifest is None:
            raise TransferError("в снимке нет manifest.txt — формат 1 (part_*.txt) не поддерживается, "
                                "используйте старый project-apply.sh")
        self.meta, self.files, self.keep = {}, [], []
        text = self.tar.extractfile(manifest).read().decode("utf-8", "surrogateescape")
        for line in text.splitlines():
            tag, sep, value = line.partition("\t")
            if not sep or tag.startswith("#"):
                continue
            if tag == "FILE":
                self.files.append(safe_rel_path(value))
            elif tag == "KEEP":
                self.keep.append(safe_rel_path(value))
            else:
                self.meta[tag] = value
        if self.meta.get("FORMAT") != "2":
            raise TransferError("неподдерживаемый формат снимка: %s" % self.meta.get("FORMAT"))
        if not self.files:
            raise TransferError("в манифесте снимка нет ни одного файла")
        for rel in self.files:
            if fsutil.nfc(rel) not in self.members:
                raise TransferError("в снимке нет файла из манифеста: %s" % rel)
        self.history = self._read_history()

    def _read_history(self):
        if history.MANIFEST not in self.extra:
            return None
        try:
            data = json.loads(self.read_extra(history.MANIFEST).decode("utf-8"))
            if data.get("version") != history.VERSION:
                return None
            names = [c["patch"] for c in data["commits"] if "patch" in c] + [data["worktree"]]
            data["untracked"] = [safe_rel_path(p) for p in data.get("untracked") or []]
            for c in data["commits"]:
                if "paths" in c:
                    c["paths"] = [safe_rel_path(p) for p in c["paths"]]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise TransferError("история в снимке повреждена: %s" % exc)
        for name in names:
            if name not in self.extra:
                raise TransferError("в снимке нет файла истории: %s" % name)
        return data
    @property
    def project(self):
        return self.meta.get("PROJECT")

    def member(self, rel):
        return self.members[fsutil.nfc(rel)]

    def read(self, rel):
        return self.tar.extractfile(self.member(rel)).read()

    def read_extra(self, name):
        return self.tar.extractfile(self.extra[name]).read()


def _state(target, snap, rel):
    m = snap.member(rel)
    kind, data, is_exec = fsutil.read_current(target, rel)
    if kind == "missing":
        return "NEW"
    if m.issym():
        return None if kind == "link" and data == m.linkname else "UPDATED"
    if kind != "file":
        return "UPDATED"
    if snap.read(rel) == data and fsutil.is_exec_mode(m.mode) == is_exec:
        return None
    return "UPDATED"


def _doomed(target, expected_nfc, self_project, tracked_only=False):
    """Файлы проекта, которых нет в снимке. tracked_only — только отслеживаемые git (режим ветки: как при
    git checkout, неотслеживаемые файлы не трогаются и остаются в рабочем каталоге)."""
    mode = fsutil.detect_mode(target)
    scope = fsutil.list_project_files(target, mode, self_project)
    if tracked_only:
        tracked = set(os.fsdecode(p) for p in gitutil.git(target, "ls-files", "-z", "--cached").split(b"\x00") if p)
        scope = [rel for rel in scope if rel in tracked]
    return mode, [rel for rel in scope
                  if fsutil.nfc(rel) not in expected_nfc and not fsutil.is_local_ide_file(rel)]


def _ancestors(rel):
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def branch_for(target, name):
    """Имя новой ветки: <name>-ДД-ММ-ГГ, при совпадении с существующей — с суффиксом -2, -3…"""
    base = "%s-%s" % (name.strip(), time.strftime("%d-%m-%y"))
    gitutil.check_branch_name(target, base)
    candidate, n = base, 2
    while gitutil.branch_exists(target, candidate):
        candidate = "%s-%d" % (base, n)
        n += 1
    return candidate


def apply_snapshot(cfg, archive, target_name, dry_run=False, force=False, commit=False, init_git=True,
                   package_id=None, branch=None):
    """branch — имя ветки (без даты) для существующего проекта в git; None — без новой ветки."""
    snap = Snapshot(archive)
    target = projects.project_path(cfg, target_name)
    create = not os.path.isdir(target)
    self_project = not create and projects.is_self(cfg, target)
    expected_nfc = set(fsutil.nfc(p) for p in snap.files + snap.keep)
    branch = (branch or "").strip() or None

    report = {"kind": "snapshot", "project": snap.project, "target": target_name, "path": target,
              "create": create, "dry_run": dry_run, "new": [], "updated": [], "unchanged": 0, "deleted": [],
              "keep": len(snap.keep), "source": snap.meta.get("SOURCE"), "mode": None, "warnings": [],
              "error": None, "blocked": False, "backup": None, "git": None, "branch": None,
              "history": history.summary(snap.history)}

    if branch and not create:
        if gitutil.has_own_repo(target) and gitutil.is_work_tree(target):
            report["branch"] = branch_for(target, branch)
        else:
            report["warnings"].append("проект не в собственном git-репозитории — ветка не будет создана")
    tracked_only = bool(report["branch"])

    if create:
        report["new"] = list(snap.files)
        doomed = []
    else:
        for rel in snap.files:
            state = _state(target, snap, rel)
            if state == "NEW":
                report["new"].append(rel)
            elif state == "UPDATED":
                report["updated"].append(rel)
            else:
                report["unchanged"] += 1
        report["mode"], doomed = _doomed(target, expected_nfc, self_project, tracked_only)
        report["deleted"] = doomed
        if gitutil.is_work_tree(target):
            dirty = gitutil.status_count(target)
            if dirty:
                report["warnings"].append("в проекте есть незакоммиченные изменения (%d) — перед применением "
                                          "делается бэкап затронутых файлов" % dirty)

    if snap.project and snap.project != target_name:
        report["warnings"].append("снимок сделан с проекта %s, применяется к %s" % (snap.project, target_name))

    limit = max(20, len(expected_nfc) // 5)
    if len(doomed) > limit and not force:
        report["blocked"] = True
        report["error"] = ("к удалению %d файлов при %d в снимке — подозрительно много (снимок другого "
                           "проекта или не тот target?). Проверьте список и повторите с force."
                           % (len(doomed), len(expected_nfc)))

    wrapper_props = "gradle/wrapper/gradle-wrapper.properties"
    wrapper_jar = "gradle/wrapper/gradle-wrapper.jar"
    if wrapper_props in snap.files and wrapper_jar not in snap.files \
            and not os.path.exists(os.path.join(target, wrapper_jar)):
        report["warnings"].append("нет gradle/wrapper/gradle-wrapper.jar (бинарный, не перенесён) — выполните "
                                  "'gradle wrapper' или снимите снимок с бинарными файлами")

    if dry_run or report["blocked"]:
        return report

    # --- Запись ---------------------------------------------------------------------------------
    if create:
        os.makedirs(target)
    # Для нового проекта бэкап — только запись в истории (откат не предусмотрен).
    backup = Backup(cfg, target_name, target, "snapshot", package_id, project_created=create)
    try:
        # Файл, на месте которого в снимке каталог (и наоборот), удаляется до записи.
        expected_dirs = set(d for rel in snap.files for d in _ancestors(rel))
        expected_set = set(snap.files)
        early = [rel for rel in doomed if rel in expected_dirs or any(a in expected_set for a in _ancestors(rel))]
        for rel in early:
            backup.before_delete(rel)
            fsutil.remove_file(target, rel)

        for rel in report["new"] + report["updated"]:
            m = snap.member(rel)
            backup.before_write(rel)
            if m.issym():
                fsutil.write_symlink(target, rel, m.linkname)
            else:
                fsutil.write_file(target, rel, snap.read(rel), mode=m.mode & 0o777)

        deleted = list(early)
        if not create:
            # Пересчёт после записи: снимок мог принести новый .gitignore. Удаляются только файлы из
            # предпросмотра — те, что прятал прежний .gitignore (локальные заметки и т.п.), остаются.
            planned = set(doomed)
            _, doomed_after = _doomed(target, expected_nfc, self_project, tracked_only)
            for rel in doomed_after:
                if rel not in planned:
                    continue
                backup.before_delete(rel)
                if fsutil.remove_file(target, rel):
                    deleted.append(rel)
        report["deleted"] = sorted(set(deleted))

        try:
            _git(snap, target, target_name, report, backup, package_id, create, init_git, commit)
        except gitutil.GitError as exc:
            report["warnings"].append("git: %s" % exc)
    finally:
        report["backup"] = backup.finish()
    return report


def _git(snap, target, target_name, report, backup, package_id, create, init_git, commit):
    label = snap.project or target_name
    if create and init_git and not gitutil.has_own_repo(target):
        branch = snap.meta.get("BRANCH")
        gitutil.init_repo(target, branch)
        report["git"] = {"init": True, "commit": None, "branch": branch, "commits": []}
        if not snap.history:
            # Для снимка из git все его файлы были в исходном репозитории — добавляются и те, что под .gitignore.
            forced = snap.files if snap.meta.get("SOURCE") == "git" else None
            sha, warn = gitutil.commit_all(target, "Импорт снимка %s (%s)" % (label, package_id or "без id"),
                                           force_paths=forced)
            report["git"]["commit"] = sha
            if warn:
                report["warnings"].append(warn)
            return
        result = history.commit_snapshot(target, snap, None, label, package_id)
        report["warnings"] += result["warnings"]
        if result["tip"]:
            gitutil.run_git(target, ["update-ref", "HEAD", result["tip"]])
            gitutil.run_git(target, ["reset", "-q"])
        report["git"].update(commit=result["tip"], commits=result["commits"])
    elif report["branch"]:
        head = gitutil.resolve_commit(target, "HEAD")
        result = history.commit_snapshot(target, snap, head, label, package_id)
        report["warnings"] += result["warnings"]
        report["git"] = {"init": False, "commit": None, "branch": None, "commits": result["commits"],
                         "reused": result["reused"]}
        if result["tip"] == head:
            report["warnings"].append("коммитить нечего: проект уже совпадает со снимком — ветка %s не создана"
                                      % report["branch"])
            return
        name = report["branch"]
        code, out, _ = gitutil.run_git(target, ["symbolic-ref", "-q", "HEAD"], check=False)
        prev_ref = out.decode("utf-8", "replace").strip() if code == 0 else None
        gitutil.run_git(target, ["update-ref", "refs/heads/" + name, result["tip"], gitutil.ZERO_SHA])
        gitutil.run_git(target, ["symbolic-ref", "HEAD", "refs/heads/" + name])
        # Индекс — по новой вершине; рабочий каталог уже содержит снимок и не трогается.
        gitutil.run_git(target, ["reset", "-q"])
        report["git"].update(commit=result["tip"], branch=name)
        backup.meta["git"] = {"branch": "refs/heads/" + name, "tip": result["tip"], "prev_ref": prev_ref,
                              "prev_head": head}
    elif commit and not create and gitutil.is_work_tree(target):
        touched = report["new"] + report["updated"] + report["deleted"]
        sha, warn = gitutil.commit_paths(target, touched, "Применён снимок %s (%s)" % (label, package_id or "без id"))
        report["git"] = {"init": False, "commit": sha}
        if warn:
            report["warnings"].append(warn)
