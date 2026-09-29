#!/usr/bin/env python3
"""Перенос изменений и снимков проектов между машинами.

  scripts.py projects                                   список проектов в корне
  scripts.py git-copy <проект> [ref ...]                выгрузка коммитов (без ref — HEAD)
  scripts.py project-copy <проект> [--with-binaries] [--history N]   снимок проекта целиком
  scripts.py inbox                                      что лежит во входящих
  scripts.py git-apply [файлы] [--project X] [--dry-run] [--commit]
  scripts.py project-apply [файлы] [--project X] [--dry-run] [--force] [--commit] [--no-git] [--branch ИМЯ]
  scripts.py backups [проект] / restore <id>            бэкапы перед применением и откат
  scripts.py ui [--port N] [--no-browser]               веб-интерфейс
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from transfer import TransferError, backup, inbox, projects, service  # noqa: E402
from transfer.config import DEFAULTS, load_config  # noqa: E402


def human_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%d %s" % (n, unit)) if unit == "Б" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def cmd_projects(cfg, args):
    print("Корень: %s" % cfg.projects_root)
    for p in projects.list_projects(cfg):
        print("  %s%s" % (p["name"], "" if p["git"] else "   (без git)"))


def print_parts(result):
    for part in result["parts"]:
        print("  %s  (%d строк, %s)" % (part["path"], part["lines"], human_size(part["bytes"])))


def cmd_git_copy(cfg, args):
    result = service.copy(cfg, args.project, "commits", refs=args.refs or ["HEAD"])
    print("Коммитов: %d" % len(result["commits"]))
    for c in result["commits"]:
        print("  %s %s" % (c["short"], c["subject"]))
    print("Файлов выгружено: %d, удалённых: %d" % (result["files"], result["deleted"]))
    for s in result["skipped"]:
        print("НЕ ПЕРЕНЕСЕНО: %s (%s)" % (s["path"], s["reason"]), file=sys.stderr)
    between = result["between"]
    if between.get("truncated"):
        print("WARN: между выбранными коммитами %d других — проверка пересечений пропущена" % between["count"],
              file=sys.stderr)
    for c in between.get("commits", []):
        print("WARN: невыбранный коммит %s %s тоже менял: %s%s" % (
            c["short"], c["subject"], ", ".join(c["paths"][:5]), " …" if c["count"] > 5 else ""), file=sys.stderr)
    print("Результат (%d частей):" % len(result["parts"]))
    print_parts(result)


def cmd_project_copy(cfg, args):
    result = service.copy(cfg, args.project, "project", with_binaries=args.with_binaries, history=args.history)
    for w in result["warnings"]:
        print("WARN: " + w, file=sys.stderr)
    if result["history"]:
        print("История (%d коммитов):" % len(result["history"]))
        for c in result["history"]:
            print("  %s %s" % (c["sha"], c["subject"]))
    if result["keep"]:
        print("Бинарные файлы НЕ перенесены (%d, для переноса — --with-binaries):" % len(result["keep"]))
        for rel in result["keep"]:
            print("  " + rel)
    print("Режим отбора: %s. Файлов: %d (%s), архив %s" % (
        result["mode"], result["files"], human_size(result["bytes"]), human_size(result["archive_bytes"])))
    print("Результат (%d частей, максимум %d строк в каждой):" % (len(result["parts"]), cfg.max_lines))
    print_parts(result)


def describe(p):
    status = {"ready": "готов", "incomplete": "не хватает частей", "error": "ошибка",
              "unknown": "не распознан"}[p["status"]]
    kind = {"changes": "изменения", "snapshot": "снимок", None: "?"}[p["kind"]]
    parts = "%d/%d" % (len(p["parts_present"]), p["parts_total"])
    line = "[%s] %-9s %-30s %s  частей %s%s" % (
        status, kind, p.get("project") or "(проект не указан)", p["id"], parts, "  (старый формат)" if p["legacy"] else "")
    for e in p["errors"]:
        line += "\n      ! " + e
    return line


def cmd_inbox(cfg, args):
    packages = inbox.scan(cfg)
    print("Inbox: %s" % cfg.inbox_dir)
    if not packages:
        print("  пусто")
    for p in packages:
        print("  " + describe(p))
        print("      ключ: %s" % p["key"])


def pick_package(cfg, args, kind):
    if args.files:
        packages = list(inbox.load_files(args.files).values())
        summaries = [inbox.summarize(cfg, p, verify=False) for p in packages]
        if len(packages) != 1:
            raise TransferError("в переданных файлах %d выгрузок — передайте файлы одной выгрузки:\n%s" % (
                len(packages), "\n".join("  " + describe(s) for s in summaries)))
        return packages[0], False
    if args.key:
        return inbox.load(cfg, args.key), True
    candidates = [p for p in inbox.scan(cfg) if p["kind"] == kind and p["status"] == "ready"]
    if len(candidates) != 1:
        others = inbox.scan(cfg)
        raise TransferError("в inbox %s подходящих выгрузок (%s) — укажите файлы или --key:\n%s" % (
            "нет" if not candidates else "несколько", kind, "\n".join(
                "  %s\n      ключ: %s" % (describe(p), p["key"]) for p in others) or "  inbox пуст"))
    return inbox.load(cfg, candidates[0]["key"]), True


def print_report(report):
    for label, key in (("NEW", "new"), ("UPDATED", "updated"), ("DELETE" if report["dry_run"] else "DELETED",
                                                                "deleted"), ("ABSENT", "absent")):
        for rel in report.get(key) or []:
            print("%-8s %s" % (label + ":", rel))
    for w in report["warnings"]:
        print("WARN: " + w, file=sys.stderr)
    print("-" * 64)
    if report["dry_run"]:
        print("DRY RUN — ничего не изменено.")
    if report.get("create"):
        print("Проект %s будет создан: %s" % (report["target"], report["path"]) if report["dry_run"]
              else "Создан проект %s: %s" % (report["target"], report["path"]))
    print("Создано: %d, изменено: %d, без изменений: %d, удалено: %d%s (проект: %s)" % (
        len(report["new"]), len(report["updated"]), report["unchanged"], len(report["deleted"]),
        (", отсутствовало: %d" % len(report["absent"])) if report.get("absent") else "", report["path"]))
    if report.get("backup"):
        print("Бэкап: %s (откат: ./scripts.py restore %s)" % (report["backup"], report["backup"]))
    if report["dry_run"]:
        if report.get("branch"):
            print("Будет создана ветка %s" % report["branch"])
        if report.get("history"):
            print("История в снимке: %d коммитов" % len(report["history"]))
    git = report.get("git")
    for c in (git or {}).get("commits") or []:
        print("COMMIT:  %s %s" % (c["short"], c["subject"]))
    if git and git.get("commit"):
        print("git: %s%s%s" % ("init, " if git.get("init") else "",
                               ("ветка %s, " % git["branch"]) if git.get("branch") else "",
                               "коммит " + git["commit"][:10]))
    if report.get("error"):
        print("ОШИБКА: " + report["error"], file=sys.stderr)
        return 1
    return 0


def cmd_apply(kind):
    def run(cfg, args):
        package, from_inbox = pick_package(cfg, args, kind)
        pkg_kind = package["kind"]
        if pkg_kind != kind:
            raise TransferError("это выгрузка вида %s, а не %s — используйте %s" % (
                pkg_kind, kind, "project-apply" if pkg_kind == "snapshot" else "git-apply"))
        report = service.apply_package(
            cfg, package, target=args.project, dry_run=args.dry_run, force=getattr(args, "force", False),
            commit=args.commit, init_git=not getattr(args, "no_git", False), archive_after=from_inbox,
            branch=getattr(args, "branch", None))
        return print_report(report)
    return run


def cmd_backups(cfg, args):
    items = backup.list_backups(cfg, args.project)
    if not items:
        print("Бэкапов нет")
    for b in items:
        c = b["counts"]
        print("%s  %-30s %-8s создано %d, изменено %d, удалено %d%s" % (
            b["id"], b["project"], b["kind"], c["created"], c["modified"], c["deleted"],
            "  [откатан %s]" % b["restored_at"] if b.get("restored_at") else ""))


def cmd_restore(cfg, args):
    result = backup.restore(cfg, args.id)
    for rel in result["removed"]:
        print("REMOVED:  " + rel)
    for rel in result["restored"]:
        print("RESTORED: " + rel)
    print("Откат выполнен: удалено %d, восстановлено %d (%s)" % (
        len(result["removed"]), len(result["restored"]), result["target"]))
    if result.get("git"):
        print("git: " + result["git"])


def cmd_ui(cfg, args):
    from transfer.web.server import serve
    serve(cfg, port=args.port or cfg.port, open_browser=not args.no_browser)


def build_parser():
    parser = argparse.ArgumentParser(prog="scripts.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    p = sub.add_parser("projects", help="список проектов")
    p.set_defaults(func=cmd_projects)

    p = sub.add_parser("git-copy", help="выгрузка коммитов")
    p.add_argument("project")
    p.add_argument("refs", nargs="*", help="хеш, ветка, тег, HEAD~N, диапазон A..B (по умолчанию HEAD)")
    p.set_defaults(func=cmd_git_copy)

    p = sub.add_parser("project-copy", help="снимок проекта")
    p.add_argument("project")
    p.add_argument("--with-binaries", action="store_true", help="переносить и бинарные файлы")
    p.add_argument("--history", type=int, metavar="N",
                   help="сколько последних коммитов добавить в снимок (0 — без истории; по умолчанию %d)"
                        % DEFAULTS["history_commits"])
    p.set_defaults(func=cmd_project_copy)

    p = sub.add_parser("inbox", help="входящие выгрузки")
    p.set_defaults(func=cmd_inbox)

    for name, kind in (("git-apply", "changes"), ("project-apply", "snapshot")):
        p = sub.add_parser(name, help="применить %s" % ("изменения коммитов" if kind == "changes" else "снимок"))
        p.add_argument("files", nargs="*", help="файлы выгрузки (по умолчанию — из inbox)")
        p.add_argument("--project", help="локальный проект (по умолчанию — из выгрузки)")
        p.add_argument("--key", help="ключ пакета из inbox (см. scripts.py inbox)")
        p.add_argument("-n", "--dry-run", action="store_true", help="показать изменения, ничего не меняя")
        p.add_argument("--commit", action="store_true", help="закоммитить применённые изменения")
        if kind == "snapshot":
            p.add_argument("--force", action="store_true", help="удалять, даже если неактуальных файлов много")
            p.add_argument("--no-git", action="store_true", help="для нового проекта не делать git init")
            p.add_argument("--branch", metavar="ИМЯ",
                           help="отвести от HEAD ветку ИМЯ-ДД-ММ-ГГ и записать в неё историю коммитов из снимка")
        p.set_defaults(func=cmd_apply(kind))

    p = sub.add_parser("backups", help="список бэкапов")
    p.add_argument("project", nargs="?")
    p.set_defaults(func=cmd_backups)

    p = sub.add_parser("restore", help="откатить применение по бэкапу")
    p.add_argument("id")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("ui", help="веб-интерфейс")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(func=cmd_ui)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config()
        return args.func(cfg, args) or 0
    except TransferError as exc:
        print("Ошибка: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
