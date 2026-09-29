#!/usr/bin/env python3
"""Первый перенос scripts на машину, где новых scripts ещё нет.

Распаковывает снимок проекта (выгрузку «Весь проект» / project-copy.sh) в текущий каталог.
Запускается из каталога scripts:

  python3 bootstrap.py                          # берёт export/project.txt
  python3 bootstrap.py part-1.txt part-2.txt    # части в отдельных файлах, в любом порядке
  python3 bootstrap.py --no-git                 # без git init и стартового коммита

Части можно сложить и в один файл подряд. Скрипт проверяет, что все части на месте и контрольная
сумма сошлась, и записывает файлы снимка с правами: существующие перезаписываются, лишние не удаляются.
Если каталог ещё не git-репозиторий — делает git init на ветке из снимка и стартовый коммит.
Нужен только Python 3.6+, сторонних пакетов нет.
"""

import argparse
import base64
import binascii
import hashlib
import io
import os
import re
import subprocess
import sys
import tarfile

try:
    from urllib.parse import unquote
except ImportError:  # pragma: no cover
    from urllib import unquote

HEADER = "#@transfer "


def fail(message):
    sys.stderr.write("ошибка: %s\n" % message)
    sys.exit(1)


def warn(message):
    sys.stderr.write("внимание: %s\n" % message)


# --- Сборка частей ----------------------------------------------------------------------------

def parse_header(line):
    fields = dict(t.split("=", 1) for t in line[len(HEADER):].split() if "=" in t)
    try:
        index, total = fields["part"].split("/")
        return {"kind": fields["kind"], "project": unquote(fields.get("project", "")), "id": unquote(fields["id"]),
                "index": int(index), "total": int(total), "lines": int(fields["lines"]),
                "sha256": fields["sha256"].lower()}
    except (KeyError, ValueError):
        fail("повреждённый заголовок части: %s" % line[:200])


def read_parts(paths):
    """Части из файлов: список (заголовок, строки тела). Пустые строки и \\r отбрасываются."""
    parts, headless = [], []
    for path in paths:
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", "replace").lstrip("﻿")
        except (IOError, OSError) as exc:
            fail("не удалось прочитать %s: %s" % (path, exc))
        current = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(HEADER):
                current = (parse_header(line), [])
                parts.append(current)
            elif line:
                (current[1] if current else headless).append(line)
    return parts, headless


def assemble(parts, headless):
    """Склеивает части и проверяет целостность. Возвращает (заголовок, tar.gz)."""
    if not parts:
        if not headless:
            fail("во входных файлах пусто")
        warn("нет строки #@transfer — части не проверить, контрольная сумма не сверяется")
        return {"id": "без id", "project": None}, decode_base64(headless)
    if headless:
        warn("%d строк до первого заголовка #@transfer — пропущены" % len(headless))
    ids = sorted(set(h["id"] for h, _ in parts))
    if len(ids) > 1:
        fail("перемешаны части разных выгрузок: %s" % ", ".join(ids))
    hdr = parts[0][0]
    if hdr["kind"] != "snapshot":
        fail("это выгрузка kind=%s, а нужен снимок проекта (kind=snapshot, «Весь проект»)" % hdr["kind"])
    bodies = {}
    for h, body in parts:
        if len(body) < h["lines"]:
            fail("часть %d/%d обрезана: ожидалось %d строк, найдено %d — скопируйте её заново"
                 % (h["index"], h["total"], h["lines"], len(body)))
        if len(body) > h["lines"]:
            warn("после части %d/%d лишние строки (%d) — пропущены" % (h["index"], h["total"], len(body) - h["lines"]))
        bodies[h["index"]] = body[:h["lines"]]
    missing = [str(i) for i in range(1, hdr["total"] + 1) if i not in bodies]
    if missing:
        fail("не хватает частей: %s из %d" % (", ".join(missing), hdr["total"]))
    archive = decode_base64(line for i in sorted(bodies) for line in bodies[i])
    if hashlib.sha256(archive).hexdigest() != hdr["sha256"]:
        fail("контрольная сумма не совпала — текст повреждён при переносе (часть изменена или вставлена не та)")
    return hdr, archive


def decode_base64(lines):
    try:
        return base64.b64decode(re.sub(r"[^A-Za-z0-9+/=]", "", "".join(lines)))
    except (binascii.Error, ValueError) as exc:
        fail("некорректный base64: %s" % exc)


# --- Снимок ---------------------------------------------------------------------------------

def open_snapshot(archive):
    """Возвращает (tar, meta, [(путь, member)], keep) по manifest.txt снимка."""
    try:
        tar = tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz")
        members = tar.getmembers()
    except (tarfile.TarError, EOFError, OSError) as exc:
        fail("снимок повреждён или неполный: %s" % exc)
    by_name, manifest = {}, None
    for m in members:
        name = m.name[2:] if m.name.startswith("./") else m.name
        if name == "manifest.txt":
            manifest = m
        elif name.startswith("files/") and (m.isfile() or m.issym() or m.islnk()):
            by_name[name[6:]] = m
    if manifest is None:
        fail("в снимке нет manifest.txt — это не снимок формата 2")
    meta, files, keep = {}, [], []
    for line in tar.extractfile(manifest).read().decode("utf-8", "surrogateescape").splitlines():
        tag, sep, value = line.partition("\t")
        if not sep or tag.startswith("#"):
            continue
        if tag == "FILE":
            files.append(value)
        elif tag == "KEEP":
            keep.append(value)
        else:
            meta[tag] = value
    if meta.get("FORMAT") != "2":
        fail("неподдерживаемый формат снимка: %s" % meta.get("FORMAT"))
    for rel in files:
        check_path(rel)
        if rel not in by_name:
            fail("в снимке нет файла из манифеста: %s" % rel)
    return tar, meta, [(rel, by_name[rel]) for rel in files], keep


def check_path(rel):
    parts = rel.split("/")
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel or re.match(r"^[A-Za-z]:", rel) \
            or any(p in ("", ".", "..", ".git") for p in parts):
        fail("недопустимый путь в снимке: %r" % rel)


def write_member(tar, member, target, rel):
    """Записывает файл или симлинк. Возвращает True, если файл был, и False, если создан."""
    dest = os.path.join(target, *rel.split("/"))
    parent = os.path.dirname(dest)
    if os.path.lexists(parent) and not os.path.isdir(parent):
        fail("конфликт: %s — файл, а нужен каталог" % os.path.relpath(parent, target))
    if not os.path.isdir(parent):
        os.makedirs(parent)
    existed = os.path.lexists(dest)
    if os.path.isdir(dest) and not os.path.islink(dest):
        fail("конфликт: %s — каталог, а нужен файл" % rel)
    if member.issym():
        if existed:
            os.remove(dest)
        os.symlink(member.linkname, dest)
        return existed
    data = tar.extractfile(member).read()
    if os.path.islink(dest):
        os.remove(dest)
    with open(dest, "wb") as fh:
        fh.write(data)
    os.chmod(dest, member.mode & 0o777)
    return existed


# --- git -------------------------------------------------------------------------------------

def git(target, *args):
    proc = subprocess.Popen(["git", "--literal-pathspecs"] + list(args), cwd=target,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = proc.communicate()
    return proc.returncode, (out or err).decode("utf-8", "replace").strip()


def init_git(target, branch, files, message):
    try:
        code, out = git(target, "init", "-q")
    except OSError:
        warn("git не найден — репозиторий не создан")
        return
    if code != 0:
        warn("git init не удался: %s" % out)
        return
    if branch and not branch.startswith("("):
        git(target, "symbolic-ref", "HEAD", "refs/heads/" + branch)
    # Все файлы снимка были в исходном репозитории — добавляются и те, что под .gitignore.
    for i in range(0, len(files), 200):
        code, out = git(target, "add", "-f", "--", *files[i:i + 200])
        if code != 0:
            warn("git add не удался: %s" % out)
            return
    if not git(target, "config", "user.name")[1] or not git(target, "config", "user.email")[1]:
        warn("в git не заданы user.name/user.email — стартовый коммит не создан. Задайте их "
             "(git config --global user.name ...; git config --global user.email ...) и выполните: "
             "git commit -m \"%s\"" % message)
        return
    code, out = git(target, "commit", "-q", "-m", message)
    if code != 0:
        warn("коммит не создан: %s" % out)
        return
    print("git: репозиторий создан (ветка %s), стартовый коммит %s"
          % (git(target, "symbolic-ref", "--short", "HEAD")[1], git(target, "rev-parse", "--short", "HEAD")[1]))


# --- main ------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Распаковка снимка scripts в текущий каталог.")
    parser.add_argument("files", nargs="*", help="файлы с частями выгрузки (по умолчанию export/project.txt)")
    parser.add_argument("--no-git", action="store_true", help="не делать git init и стартовый коммит")
    parser.add_argument("-y", "--yes", action="store_true", help="не спрашивать подтверждения")
    args = parser.parse_args()

    target = os.getcwd()
    paths = args.files or [os.path.join("export", "project.txt")]
    for path in paths:
        if not os.path.isfile(path):
            fail("нет файла %s — запускайте из каталога scripts или передайте путь к файлу" % path)

    hdr, archive = assemble(*read_parts(paths))
    tar, meta, files, keep = open_snapshot(archive)
    project = meta.get("PROJECT") or hdr["project"] or "?"
    print("Снимок %s (%s, ветка %s): %d файлов → %s"
          % (project, hdr["id"], meta.get("BRANCH", "—"), len(files), target))

    if os.path.basename(target) != project and not args.yes:
        answer = input("Текущий каталог называется %s, а снимок сделан с проекта %s. Распаковать сюда? [y/N] "
                       % (os.path.basename(target), project))
        if answer.strip().lower() not in ("y", "yes", "д", "да"):
            fail("отменено")

    created = updated = 0
    for rel, member in files:
        if write_member(tar, member, target, rel):
            updated += 1
        else:
            created += 1
    print("Создано файлов: %d, перезаписано: %d" % (created, updated))
    if keep:
        warn("бинарные файлы в снимок не попали, перенесите их вручную: %s" % ", ".join(keep))

    if args.no_git:
        pass
    elif os.path.exists(os.path.join(target, ".git")):
        print("git: каталог уже в git — коммит не создан, проверьте git status")
    else:
        init_git(target, meta.get("BRANCH"), [rel for rel, _ in files],
                 "Импорт снимка %s (%s)" % (project, hdr["id"]))
    print("Готово. Веб-интерфейс: ./ui.sh")


if __name__ == "__main__":
    main()
