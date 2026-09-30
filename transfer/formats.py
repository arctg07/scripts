"""Форматы выгрузок.

Каждый выходной файл — часть «пакета» и начинается со строки-заголовка:

  #@transfer v1 kind=changes project=teg-reactive id=2026-09-25_14-00-00-a1b2 part=1/3 lines=6999 sha256=...

За ней ровно `lines` строк тела. Тела частей по порядку, склеенные, дают полезную нагрузку:
  - kind=changes  — текст в формате changed_classes.txt (блоки FILE + секция DELETED FILES);
  - kind=snapshot — base64 от tar.gz со снимком проекта.
sha256 считается от нагрузки (changes) или от tar.gz (snapshot) и проверяется при сборке.

Старые выгрузки без заголовков тоже читаются (legacy): changed_classes.txt и
project-copy-<время>-part-NNN.txt со снимком формата 2.
"""

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import time

try:
    from urllib.parse import quote, unquote
except ImportError:  # pragma: no cover
    from urllib import quote, unquote

from . import TransferError

HEADER_PREFIX = b"#@transfer "
SEP = b"=" * 64
SEP_RE = re.compile(rb"^=+$")
KINDS = ("changes", "snapshot")


# --- Идентификаторы и заголовки ---------------------------------------------------------------

def new_package_id():
    return time.strftime("%Y-%m-%d_%H-%M-%S") + "-" + secrets.token_hex(2)


def safe_file_part(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "project"


def make_header(kind, project, pkg_id, index, total, lines, sha):
    return ("#@transfer v1 kind=%s project=%s id=%s part=%d/%d lines=%d sha256=%s\n" % (
        kind, quote(project, safe=""), quote(pkg_id, safe=""), index, total, lines, sha)).encode()


def parse_header(line):
    """Разбирает строку заголовка; None — это не заголовок."""
    line = line.rstrip(b"\r\n \t")
    if not line.startswith(HEADER_PREFIX):
        return None
    fields = {}
    for token in line[len(HEADER_PREFIX):].decode("ascii", "replace").split():
        key, sep, value = token.partition("=")
        if sep:
            fields[key] = unquote(value)
    try:
        index, total = fields["part"].split("/")
        hdr = {
            "version": line.split()[1].decode("ascii", "replace"),
            "kind": fields["kind"],
            "project": fields.get("project") or None,
            "id": fields["id"],
            "index": int(index),
            "total": int(total),
            "lines": int(fields["lines"]),
            "sha256": fields["sha256"].lower(),
        }
    except (KeyError, ValueError):
        raise TransferError("повреждённый заголовок части: %s" % line[:200].decode("utf-8", "replace"))
    if hdr["kind"] not in KINDS or not (1 <= hdr["index"] <= hdr["total"]) or hdr["lines"] < 0:
        raise TransferError("некорректный заголовок части: %s" % line[:200].decode("utf-8", "replace"))
    return hdr


def split_lines(data):
    """Строки без '\\n'. Завершающий перевод строки не порождает пустой строки."""
    lines = data.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return lines


def make_parts(kind, project, pkg_id, payload, sha, max_lines):
    """Режет нагрузку (bytes, заканчивается на \\n) на части с заголовками. Возвращает список bytes."""
    body_max = max(1, max_lines - 1)
    lines = [line + b"\n" for line in split_lines(payload)]
    chunks = [lines[i:i + body_max] for i in range(0, len(lines), body_max)] or [[]]
    total = len(chunks)
    return [make_header(kind, project, pkg_id, i + 1, total, len(chunk), sha) + b"".join(chunk)
            for i, chunk in enumerate(chunks)]


def write_parts(export_dir, project, kind, pkg_id, parts):
    """Пишет части в export/<проект>/, удалив прежние комплекты того же вида."""
    out_dir = os.path.join(export_dir, safe_file_part(project))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir)
    prefix = "%s-%s-" % (kind, safe_file_part(project))
    for name in os.listdir(out_dir):
        if name.startswith(prefix) and name.endswith(".txt"):
            os.remove(os.path.join(out_dir, name))
    files = []
    for i, data in enumerate(parts, 1):
        name = "%s%s-part-%03d.txt" % (prefix, pkg_id, i)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        files.append({"name": name, "path": path, "project_dir": safe_file_part(project),
                      "lines": data.count(b"\n"), "bytes": len(data)})
    return files


def sha256(data):
    return hashlib.sha256(data).hexdigest()


# --- Разбор входных файлов --------------------------------------------------------------------

_LEGACY_SNAPSHOT_NAME = re.compile(r"project-copy-(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})-part-(\d+)\.txt$")
_BASE64_LINE = re.compile(rb"^[A-Za-z0-9+/=]+$")


def scan_file(data, source):
    """Разбирает содержимое одного входного файла.

    Возвращает список элементов:
      {"type": "part", "header": {...}, "body": [строки], "source": ...}
      {"type": "legacy-changes" | "legacy-snapshot" | "unknown", "data": bytes, "source": ...}
    а также {"type": "error", "message": ..., "source": ...} для испорченных частей.
    """
    lines = split_lines(data)
    if not any(line.startswith(HEADER_PREFIX) for line in lines):
        return [_detect_legacy(data, lines, source)]

    items = []
    i = 0
    junk = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith(HEADER_PREFIX):
            if line.strip():
                junk += 1
            i += 1
            continue
        try:
            hdr = parse_header(line)
        except TransferError as exc:
            items.append({"type": "error", "message": str(exc), "source": source})
            i += 1
            continue
        crlf = line.endswith(b"\r")
        body = lines[i + 1:i + 1 + hdr["lines"]]
        got = len(body)
        for k, b in enumerate(body):
            if b.startswith(HEADER_PREFIX) and _starts_part(lines, i + 1 + k, hdr):
                got = k
                break
        if crlf:
            # Транспорт превратил \n в \r\n — возвращаем как было.
            body = [b[:-1] if b.endswith(b"\r") else b for b in body]
        if got < hdr["lines"]:
            items.append({"type": "error", "source": source, "header": hdr,
                          "message": "часть %d/%d обрезана: ожидалось %d строк, найдено %d" % (
                              hdr["index"], hdr["total"], hdr["lines"], got)})
            i += 1 + got
            continue
        items.append({"type": "part", "header": hdr, "body": body, "source": source})
        i += 1 + hdr["lines"]
    if junk:
        items.append({"type": "warning", "source": source,
                      "message": "%s: %d посторонних строк вне частей — пропущены" % (source, junk)})
    return items


_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _starts_part(lines, k, hdr):
    """Строка k внутри тела части hdr — заголовок следующей части (текст обрезан, за ним вставлена другая)?

    В содержимом файлов тоже бывают такие строки (пример заголовка в README), поэтому заголовком
    считается только корректная строка той же выгрузки или строка, за которой есть всё её тело.
    """
    try:
        other = parse_header(lines[k])
    except TransferError:
        return False
    if other is None or not _SHA_RE.match(other["sha256"]):
        return False
    return other["id"] == hdr["id"] or len(lines) - k - 1 >= other["lines"]


def _detect_legacy(data, lines, source):
    item = {"data": data, "source": source}
    meaningful = [l.rstrip(b"\r") for l in lines if l.strip()]
    if not meaningful:
        item["type"] = "unknown"
        return item
    for k in range(len(lines) - 2):
        if SEP_RE.match(lines[k].rstrip(b"\r")) and SEP_RE.match(lines[k + 2].rstrip(b"\r")) and (
                lines[k + 1].startswith(b"FILE: ") or lines[k + 1].rstrip(b"\r") == b"DELETED FILES"):
            item["type"] = "legacy-changes"
            return item
    if all(_BASE64_LINE.match(l) for l in meaningful):
        item["type"] = "legacy-snapshot"
        m = _LEGACY_SNAPSHOT_NAME.search(source)
        item["group"] = m.group(1) if m else None
        item["order"] = int(m.group(2)) if m else 0
        return item
    item["type"] = "unknown"
    return item


def assemble_parts(parts):
    """Склеивает части одного пакета (в любом порядке) и проверяет целостность.

    Возвращает (kind, payload) — для snapshot payload это уже tar.gz.
    """
    if not parts:
        raise TransferError("нет частей")
    hdr = parts[0]["header"]
    by_index = {}
    for part in parts:
        h = part["header"]
        if (h["kind"], h["id"], h["total"], h["sha256"]) != (hdr["kind"], hdr["id"], hdr["total"], hdr["sha256"]):
            raise TransferError("части разных выгрузок перемешаны (id %s)" % hdr["id"])
        prev = by_index.get(h["index"])
        if prev is not None and prev["body"] != part["body"]:
            raise TransferError("часть %d/%d встречается дважды с разным содержимым" % (h["index"], h["total"]))
        by_index[h["index"]] = part
    missing = [i for i in range(1, hdr["total"] + 1) if i not in by_index]
    if missing:
        raise TransferError("не хватает частей: %s из %d" % (", ".join(map(str, missing)), hdr["total"]))
    payload = b"".join(line + b"\n" for i in range(1, hdr["total"] + 1) for line in by_index[i]["body"])
    if hdr["kind"] == "snapshot":
        payload = decode_base64(payload)
    if sha256(payload) != hdr["sha256"]:
        raise TransferError("контрольная сумма не совпала — текст повреждён при переносе "
                            "(часть обрезана, изменена или вставлена не та)")
    return hdr["kind"], payload


def decode_base64(text):
    compact = re.sub(rb"[^A-Za-z0-9+/=]", b"", text)
    try:
        return base64.b64decode(compact)
    except (binascii.Error, ValueError) as exc:
        raise TransferError("некорректный base64: %s" % exc)


def encode_base64(data):
    return base64.encodebytes(data)


# --- Формат changes (changed_classes.txt) -----------------------------------------------------

def build_changes(meta_lines, files, deleted, versions=()):
    """files — список (path, content_bytes). Возвращает нагрузку в формате git-copy.sh.

    Метаданные — строки '# ...' до первого блока; старый git-apply.sh их пропускает.
    versions — промежуточные версии файлов [(sha коммита, path, content)] для покоммитного применения:
    блоки VERSION идут до блоков FILE, старый git-apply.sh их не замечает.
    """
    out = []
    for line in meta_lines:
        out.append(("# " + line + "\n").encode("utf-8"))
    for sha, path, content in versions:
        out.append(SEP + b"\n")
        out.append(b"VERSION: " + sha.encode() + b" " + os.fsencode(path) + b"\n")
        out.append(SEP + b"\n")
        out.append(content)
        out.append(b"\n\n")
    for path, content in files:
        out.append(SEP + b"\n")
        out.append(b"FILE: " + os.fsencode(path) + b"\n")
        out.append(SEP + b"\n")
        out.append(content)
        out.append(b"\n\n")
    out.append(SEP + b"\n")
    out.append(b"DELETED FILES\n")
    out.append(SEP + b"\n")
    for path in deleted:
        out.append(os.fsencode(path) + b"\n")
    return b"".join(out)


def eol_count(content):
    return len(content) - len(content.rstrip(b"\n"))


def default_eol(base):
    """Сколько \\n получит файл без метаданных EOL (поведение старого git-apply.sh)."""
    return 1 if base else 0


def parse_changes(payload):
    """Разбор нагрузки changes. Возвращает dict с meta, files [(path, content)], deleted [path],
    versions {(sha, path): content}.

    Содержимое восстанавливается как в git-apply.sh: хвостовые пустые строки блока срезаются,
    затем добавляется один \\n. Метаданные EOL/EXEC уточняют окончание файла и исполняемость.
    meta["history"] — изменения каждого коммита (COMMIT-DATA) для покоммитного применения.
    """
    lines = split_lines(payload)
    meta = {"project": None, "commits": [], "exec": set(), "eol": {}, "skipped": [], "created": None,
            "history": []}
    files, deleted, raw_versions = [], [], []
    mode = "none"
    cur_path, cur_version, content = None, None, []

    def flush():
        if cur_path is None:
            return
        end = len(content)
        while end > 0 and content[end - 1] == b"":
            end -= 1
        if cur_version:
            raw_versions.append((cur_version, cur_path, b"\n".join(content[:end])))
        else:
            files.append((cur_path, b"\n".join(content[:end])))

    n = len(lines)
    i = 0
    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else b""
        nxt2 = lines[i + 2] if i + 2 < n else b""
        if SEP_RE.match(line) and SEP_RE.match(nxt2):
            if nxt.startswith(b"FILE: "):
                flush()
                cur_path, cur_version, content = os.fsdecode(nxt[6:]), None, []
                mode = "file"
                i += 3
                continue
            if nxt.startswith(b"VERSION: ") and b" " in nxt[9:]:
                flush()
                sha, _, path = nxt[9:].partition(b" ")
                cur_path, cur_version, content = os.fsdecode(path), sha.decode("ascii", "replace"), []
                mode = "file"
                i += 3
                continue
            if nxt == b"DELETED FILES":
                flush()
                cur_path, cur_version, content = None, None, []
                mode = "deleted"
                i += 3
                continue
        if mode == "file":
            content.append(line)
        elif mode == "deleted":
            if line:
                deleted.append(os.fsdecode(line))
        elif line.startswith(b"# "):
            _parse_meta(line[2:].decode("utf-8", "replace"), meta)
        i += 1
    flush()

    result = []
    for path, base in files:
        eol = meta["eol"].get(path)
        if eol is None:
            eol = default_eol(base)
        result.append((path, base + b"\n" * eol))
    eols = {}
    for c in meta["history"]:
        for path, kind, eol, _ in c["changes"]:
            if kind == "version":
                eols[(c["sha"], path)] = eol
    versions = {}
    for sha, path, base in raw_versions:
        eol = eols.get((sha, path))
        versions[(sha, path)] = base + b"\n" * (default_eol(base) if eol is None else eol)
    return {"meta": meta, "files": result, "deleted": deleted, "versions": versions}


def _parse_meta(text, meta):
    key, _, value = text.partition(": ")
    if key == "PROJECT":
        meta["project"] = value
    elif key == "CREATED":
        meta["created"] = value
    elif key == "COMMIT":
        sha, _, subject = value.partition(" ")
        meta["commits"].append({"sha": sha, "subject": subject})
    elif key == "EXEC":
        meta["exec"].add(value)
    elif key == "EOL":
        count, _, path = value.partition(" ")
        if count.isdigit() and path:
            meta["eol"][path] = int(count)
    elif key == "SKIPPED":
        meta["skipped"].append(value)
    elif key == "COMMIT-DATA":
        entry = _parse_commit_data(value)
        if entry:
            meta["history"].append(entry)


def commit_data(sha, author, message, changes):
    """Строка метаданных COMMIT-DATA. changes — [(path, "file" | "delete" | "version", eol, exec)]:
    file — итоговая версия из блока FILE, version — промежуточная из блока VERSION."""
    return "COMMIT-DATA: " + json.dumps({"sha": sha, "author": list(author), "message": message,
                                          "changes": [list(c) for c in changes]},
                                         ensure_ascii=False, separators=(",", ":"))


def _parse_commit_data(value):
    try:
        data = json.loads(value)
        changes = [(str(path), kind, int(eol), bool(is_exec)) for path, kind, eol, is_exec in data["changes"]
                   if kind in ("file", "delete", "version")]
        author = [str(x) for x in data.get("author") or []]
        return {"sha": str(data["sha"]), "author": author if len(author) == 3 else None,
                "message": str(data.get("message") or ""), "changes": changes}
    except (ValueError, KeyError, TypeError):
        return None


# --- Безопасность путей ----------------------------------------------------------------------

def safe_rel_path(path):
    """Относительный путь внутри проекта; запрещены абсолютные пути, '..' и запись в .git."""
    if not path or "\x00" in path or "\n" in path or "\\" in path:
        raise TransferError("недопустимый путь: %r" % path)
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise TransferError("абсолютный путь запрещён: %s" % path)
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise TransferError("недопустимый путь: %s" % path)
    if any(p == ".git" for p in parts):
        raise TransferError("запись в .git запрещена: %s" % path)
    return path
