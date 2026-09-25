"""Отбор файлов проекта и безопасная запись. Общая часть для copy и apply."""

import os
import stat
import tempfile
import unicodedata

from . import TransferError, gitutil

# Не переносятся и никогда не удаляются при восстановлении.
PROTECTED_EXACT = {
    ".claude/settings.local.json",
    "CLAUDE.local.md",
    # Старые per-project скрипты переноса и их выгрузки.
    "sh/project-copy.sh",
    "sh/project-apply.sh",
}
PROTECTED_PREFIXES = ("sh/export/",)
# Служебные каталоги самой папки scripts.
SELF_PROTECTED_TOP = ("inbox", "export", "backups", "config.local.json")

# Узнаётся один раз при импорте: os.umask глобален для процесса и не потокобезопасен.
_UMASK = os.umask(0o022)
os.umask(_UMASK)

_JUNK_DIRS = ("/.git/", "/.gradle/", "/.idea/", "/.vscode/", "/.settings/", "/node_modules/", "/.kotlin/")
_JUNK_NAMES = (".DS_Store", ".classpath", ".project")
_JUNK_SUFFIXES = (".iml", ".class")


def is_protected(rel, self_project=False):
    if rel in PROTECTED_EXACT or rel.startswith(PROTECTED_PREFIXES):
        return True
    if self_project and rel.split("/", 1)[0] in SELF_PROTECTED_TOP:
        return True
    return False


def is_build_junk(rel):
    """Служебные файлы сборки/IDE — только для проекта без git (там их отсекает .gitignore).

    Каталоги build/out/target отсекаются лишь вне src/ — внутри src/ это обычные пакеты.
    """
    path = "/" + rel
    if any(d in path for d in _JUNK_DIRS):
        return True
    name = path.rsplit("/", 1)[1]
    if name in _JUNK_NAMES or name.endswith(_JUNK_SUFFIXES):
        return True
    prefix = path.split("/src/", 1)[0] + "/"
    return "/build/" in prefix or "/out/" in prefix or "/target/" in prefix


def is_local_ide_file(rel):
    """Настройки IDE переносятся, если есть в снимке, но локально никогда не удаляются."""
    path = "/" + rel
    return "/.idea/" in path or "/.vscode/" in path or path.endswith(".iml")


def detect_mode(root):
    return "git" if gitutil.is_work_tree(root) else "find"


def list_project_files(root, mode, self_project=False):
    """Файлы проекта: пути от корня, отсортированы.

    git  — всё, что попало бы в коммит: отслеживаемые и новые неигнорируемые файлы;
    find — все файлы, кроме служебных каталогов сборки/IDE.
    """
    if mode == "git":
        out = gitutil.git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        candidates = [os.fsdecode(p) for p in out.split(b"\x00") if p]
    else:
        candidates = []
        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            keep_dirs = []
            for d in dirnames:
                if d in (".git", ".gradle", "node_modules"):
                    continue
                if os.path.islink(os.path.join(dirpath, d)):
                    candidates.append(rel_dir + d)  # симлинк на каталог — как файл
                else:
                    keep_dirs.append(d)
            dirnames[:] = keep_dirs
            candidates.extend(rel_dir + f for f in filenames)

    result = set()
    for rel in candidates:
        full = os.path.join(root, rel)
        # git ls-files отдаёт и удалённые с диска файлы индекса, и вложенные репозитории.
        if not (os.path.islink(full) or os.path.isfile(full)):
            continue
        if is_protected(rel, self_project):
            continue
        if mode == "find" and is_build_junk(rel):
            continue
        result.add(rel)
    return sorted(result)


def nfc(path):
    """Пути сравниваются в NFC: macOS может отдавать не-ASCII имена в NFD."""
    return unicodedata.normalize("NFC", path)


def is_binary(path):
    """Бинарный — только при наличии NUL-байта. Пустые и текстовые файлы — не бинарные."""
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 16)
            if not chunk:
                return False
            if b"\x00" in chunk:
                return True


def is_exec_mode(mode):
    return bool(mode & 0o111)


def _inside(root_real, path):
    return path == root_real or path.startswith(root_real.rstrip(os.sep) + os.sep)


def ensure_parent(root, rel):
    """Создаёт каталоги для rel и проверяет, что запись не уйдёт за пределы проекта через симлинк."""
    root_real = os.path.realpath(root)
    parent = os.path.dirname(os.path.join(root, rel))
    cur = root
    rel_dir = os.path.dirname(rel)
    for part in (rel_dir.split("/") if rel_dir else []):
        cur = os.path.join(cur, part)
        if os.path.lexists(cur) and not os.path.isdir(cur):
            raise TransferError("конфликт: %s — файл, а нужен каталог" % os.path.relpath(cur, root))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    if not _inside(root_real, os.path.realpath(parent)):
        raise TransferError("путь ведёт за пределы проекта через симлинк: %s" % rel)
    return os.path.join(root, rel)


def write_file(root, rel, data, mode=None):
    """Атомарная запись файла. mode=None — сохранить права существующего файла."""
    dst = ensure_parent(root, rel)
    if os.path.isdir(dst) and not os.path.islink(dst):
        raise TransferError("конфликт: %s — каталог, а нужен файл" % rel)
    if mode is None:
        if os.path.lexists(dst) and not os.path.islink(dst):
            mode = stat.S_IMODE(os.stat(dst).st_mode)
        else:
            mode = 0o666 & ~_UMASK
    fd, tmp = tempfile.mkstemp(prefix=".transfer-", dir=os.path.dirname(dst))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def write_symlink(root, rel, target):
    dst = ensure_parent(root, rel)
    if os.path.lexists(dst):
        if os.path.isdir(dst) and not os.path.islink(dst):
            raise TransferError("конфликт: %s — каталог, а нужен симлинк" % rel)
        os.remove(dst)
    os.symlink(target, dst)


def remove_file(root, rel):
    """Удаляет файл и опустевшие после этого каталоги (пакеты удалённых классов)."""
    path = os.path.join(root, rel)
    if not os.path.lexists(path):
        return False
    os.remove(path)
    d = os.path.dirname(rel)
    while d:
        try:
            os.rmdir(os.path.join(root, d))
        except OSError:
            break
        d = os.path.dirname(d)
    return True


def read_current(root, rel):
    """('missing'|'file'|'link'|'dir', данные, exec)."""
    path = os.path.join(root, rel)
    if os.path.islink(path):
        return "link", os.readlink(path), False
    if not os.path.lexists(path):
        return "missing", None, False
    if os.path.isdir(path):
        return "dir", None, False
    with open(path, "rb") as fh:
        data = fh.read()
    return "file", data, is_exec_mode(os.stat(path).st_mode)
