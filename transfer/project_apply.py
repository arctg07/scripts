"""project-apply: восстановление проекта из снимка project-copy.

1) Файлы снимка записываются в проект: отсутствующие создаются, существующие перезаписываются
   (права вроде исполняемого gradlew сохраняются).
2) Удаляются неактуальные файлы — те, что есть в проекте, но отсутствуют в снимке. Кандидаты
   отбираются так же, как при копировании (через git или обходом без служебных каталогов).
   Не удаляются: бинарные файлы с пометкой KEEP, настройки IDE (.idea, *.iml, .vscode) и
   локальные настройки Claude Code. Если удалять предстоит подозрительно много файлов (снимок
   другого проекта, не тот target), применение останавливается — продолжить можно с force.
3) Если проекта нет — он создаётся, после записи выполняются git init и стартовый коммит.
"""

import io
import os
import tarfile

from . import TransferError, fsutil, gitutil, projects
from .backup import Backup
from .formats import safe_rel_path


class Snapshot(object):
    def __init__(self, archive):
        try:
            self.tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
            members = self.tar.getmembers()
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise TransferError("снимок повреждён или неполный: %s" % exc)
        self.members = {}
        manifest = None
        for m in members:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name == "manifest.txt":
                manifest = m
            elif name.startswith("files/") and (m.isfile() or m.issym() or m.islnk()):
                self.members[fsutil.nfc(name[6:])] = m
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

    @property
    def project(self):
        return self.meta.get("PROJECT")

    def member(self, rel):
        return self.members[fsutil.nfc(rel)]

    def read(self, rel):
        return self.tar.extractfile(self.member(rel)).read()


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


def _doomed(target, expected_nfc, self_project):
    mode = fsutil.detect_mode(target)
    scope = fsutil.list_project_files(target, mode, self_project)
    return mode, [rel for rel in scope
                  if fsutil.nfc(rel) not in expected_nfc and not fsutil.is_local_ide_file(rel)]


def _ancestors(rel):
    parts = rel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts))]


def apply_snapshot(cfg, archive, target_name, dry_run=False, force=False, commit=False, init_git=True,
                   package_id=None):
    snap = Snapshot(archive)
    target = projects.project_path(cfg, target_name)
    create = not os.path.isdir(target)
    self_project = not create and projects.is_self(cfg, target)
    expected_nfc = set(fsutil.nfc(p) for p in snap.files + snap.keep)

    report = {"kind": "snapshot", "project": snap.project, "target": target_name, "path": target,
              "create": create, "dry_run": dry_run, "new": [], "updated": [], "unchanged": 0, "deleted": [],
              "keep": len(snap.keep), "source": snap.meta.get("SOURCE"), "mode": None, "warnings": [],
              "error": None, "blocked": False, "backup": None, "git": None}

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
        report["mode"], doomed = _doomed(target, expected_nfc, self_project)
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
            # Пересчёт после записи: снимок мог принести новый .gitignore.
            _, doomed_after = _doomed(target, expected_nfc, self_project)
            for rel in doomed_after:
                backup.before_delete(rel)
                if fsutil.remove_file(target, rel):
                    deleted.append(rel)
        report["deleted"] = sorted(set(deleted))
    finally:
        report["backup"] = backup.finish()

    # --- git -----------------------------------------------------------------------------------
    message = "Импорт снимка %s (%s)" % (snap.project or target_name, package_id or "без id")
    if create and init_git and not gitutil.has_own_repo(target):
        gitutil.init_repo(target, snap.meta.get("BRANCH"))
        # Для снимка из git все его файлы были в исходном репозитории — добавляются и те, что под .gitignore.
        forced = snap.files if snap.meta.get("SOURCE") == "git" else None
        sha, warn = gitutil.commit_all(target, message, force_paths=forced)
        report["git"] = {"init": True, "commit": sha, "branch": snap.meta.get("BRANCH")}
        if warn:
            report["warnings"].append(warn)
    elif commit and not create and gitutil.is_work_tree(target):
        touched = report["new"] + report["updated"] + report["deleted"]
        sha, warn = gitutil.commit_paths(target, touched, "Применён снимок %s (%s)" % (
            snap.project or target_name, package_id or "без id"))
        report["git"] = {"init": False, "commit": sha}
        if warn:
            report["warnings"].append(warn)
    return report
