"""Веб-интерфейс: http.server на 127.0.0.1 + JSON API над движком.

Каждый запрос к /api/ должен нести заголовок X-Transfer-Token (токен генерируется при запуске и
встраивается в страницу), а Host — быть локальным: так сторонний сайт в браузере не сможет
ни прочитать данные, ни запустить применение (в том числе через DNS rebinding).
"""

import json
import os
import re
import secrets
import socket
import sys
import threading
import traceback
import webbrowser

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
    from urllib.parse import parse_qs, unquote, urlparse
except ImportError:  # pragma: no cover
    raise

from .. import TransferError, VERSION, backup, formats, gitutil, inbox, projects, service

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_BODY = 64 * 1024 * 1024
_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost|\[::1\])(?::(\d+))?$")


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class ApiError(Exception):
    def __init__(self, status, message):
        Exception.__init__(self, message)
        self.status = status


def _bool(value):
    return value in (True, "1", "true", "yes", "on", 1)


class App(object):
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = secrets.token_urlsafe(24)
        self.routes = []
        route = self.route
        route("GET", r"/api/state", self.state)
        route("GET", r"/api/projects", self.list_projects)
        route("GET", r"/api/projects/(?P<p>[^/]+)", self.project)
        route("GET", r"/api/projects/(?P<p>[^/]+)/branches", self.branches)
        route("GET", r"/api/projects/(?P<p>[^/]+)/commits", self.commits)
        route("GET", r"/api/projects/(?P<p>[^/]+)/commits/(?P<sha>[0-9a-fA-F]{4,40})/files", self.commit_files)
        route("GET", r"/api/projects/(?P<p>[^/]+)/exports", self.exports)
        route("POST", r"/api/copy/preview", self.copy_preview)
        route("POST", r"/api/copy", self.copy)
        route("GET", r"/api/export/(?P<p>[^/]+)/(?P<name>[^/]+)", self.export_file)
        route("GET", r"/api/inbox", self.inbox_list)
        route("POST", r"/api/inbox", self.inbox_add)
        route("POST", r"/api/inbox/discard", self.inbox_discard)
        route("POST", r"/api/apply", self.apply)
        route("GET", r"/api/backups", self.backups)
        route("POST", r"/api/backups/restore", self.restore)

    def route(self, method, pattern, handler):
        self.routes.append((method, re.compile("^" + pattern + "$"), handler))

    def dispatch(self, method, path, query, body):
        for m, regex, handler in self.routes:
            match = regex.match(path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return handler(query=query, body=body, **params)
        raise ApiError(404, "нет такого метода API: %s %s" % (method, path))

    # --- Проекты ---------------------------------------------------------------------------------

    def state(self, **_):
        return {"version": VERSION, "projects_root": self.cfg.projects_root, "scripts_dir": self.cfg.scripts_dir,
                "data_dir": self.cfg.data_dir, "inbox_dir": self.cfg.inbox_dir, "export_dir": self.cfg.export_dir,
                "max_lines": self.cfg.max_lines, "history_commits": self.cfg.history_commits,
                "host": socket.gethostname()}

    def list_projects(self, **_):
        return {"projects": projects.list_projects(self.cfg)}

    def project(self, p, **_):
        return projects.project_info(self.cfg, p)

    def _repo(self, p):
        repo = projects.existing_project(self.cfg, p)
        if not gitutil.is_work_tree(repo):
            raise ApiError(400, "проект %s — не git-репозиторий" % p)
        return repo

    def branches(self, p, **_):
        repo = self._repo(p)
        return {"current": gitutil.current_branch(repo), "branches": gitutil.list_branches(repo)}

    def commits(self, p, query, **_):
        repo = self._repo(p)
        ref = query.get("ref") or "HEAD"
        skip = int(query.get("skip") or 0)
        limit = min(int(query.get("limit") or 100), 500)
        q = (query.get("q") or "").strip() or None
        if not gitutil.resolve_commit(repo, ref):
            return {"commits": [], "ref": ref, "empty": True}
        return {"commits": gitutil.log_commits(repo, ref, skip=skip, limit=limit, query=q), "ref": ref}

    def commit_files(self, p, sha, **_):
        repo = self._repo(p)
        full = gitutil.resolve_commit(repo, sha)
        if not full:
            raise ApiError(404, "коммит не найден: %s" % sha)
        files = [{"status": st, "path": path, "mode": mode} for st, path, mode, _ in gitutil.diff_tree_raw(repo, full)]
        return {"sha": full, "merge": gitutil.parent_count(repo, full) > 1, "files": files}

    def exports(self, p, **_):
        projects.validate_name(p)
        d = os.path.join(self.cfg.export_dir, formats.safe_file_part(p))
        items = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                path = os.path.join(d, name)
                if name.endswith(".txt") and os.path.isfile(path):
                    with open(path, "rb") as fh:
                        head = fh.readline()
                    try:
                        hdr = formats.parse_header(head)
                    except TransferError:
                        hdr = None
                    items.append({"name": name, "project_dir": formats.safe_file_part(p), "path": path,
                                  "bytes": os.path.getsize(path), "mtime": os.path.getmtime(path),
                                  "kind": hdr and hdr["kind"], "id": hdr and hdr["id"],
                                  "part": hdr and "%d/%d" % (hdr["index"], hdr["total"])})
        return {"dir": d, "files": items}

    # --- Копирование -----------------------------------------------------------------------------

    def _copy_args(self, body):
        history = body.get("history")
        return dict(project=body.get("project"), mode=body.get("mode"), refs=body.get("commits"),
                    ref=body.get("ref"), with_binaries=_bool(body.get("with_binaries")),
                    history=None if history in (None, "") else int(history))

    def copy_preview(self, body, **_):
        return service.copy_preview(self.cfg, **self._copy_args(body))

    def copy(self, body, **_):
        return service.copy(self.cfg, **self._copy_args(body))

    def export_file(self, p, name, **_):
        if "/" in p or p.startswith(".") or "/" in name or name.startswith(".") or not name.endswith(".txt"):
            raise ApiError(400, "некорректный путь")
        path = os.path.join(self.cfg.export_dir, p, name)
        if not os.path.isfile(path):
            raise ApiError(404, "файл не найден: %s" % name)
        with open(path, "rb") as fh:
            return RawResponse(fh.read(), "text/plain; charset=utf-8")

    # --- Inbox и применение ----------------------------------------------------------------------

    def inbox_list(self, **_):
        return {"dir": self.cfg.inbox_dir, "packages": inbox.scan(self.cfg)}

    def inbox_add(self, body, **_):
        text = body.get("text") or ""
        name = inbox.save_text(self.cfg, text.encode("utf-8"), body.get("name"))
        return {"saved": name, "packages": inbox.scan(self.cfg)}

    def inbox_discard(self, body, **_):
        package = inbox.load(self.cfg, body.get("key"))
        moved = inbox.move_package(self.cfg, package, inbox.DISCARDED)
        return {"moved": moved, "packages": inbox.scan(self.cfg)}

    def apply(self, body, **_):
        package = inbox.load(self.cfg, body.get("key"))
        target = (body.get("target") or "").strip() or None
        return service.apply_package(self.cfg, package, target=target, dry_run=_bool(body.get("dry_run")),
                                     force=_bool(body.get("force")), commit=_bool(body.get("commit")),
                                     init_git=_bool(body.get("init_git", True)),
                                     branch=(body.get("branch") or "").strip() or None)

    def backups(self, query, **_):
        return {"backups": backup.list_backups(self.cfg, query.get("project") or None)}

    def restore(self, body, **_):
        with service.LOCK:
            return backup.restore(self.cfg, body.get("id"))


class RawResponse(object):
    def __init__(self, data, content_type):
        self.data = data
        self.content_type = content_type


def make_handler(app, port_ref):
    class Handler(BaseHTTPRequestHandler):
        server_version = "transfer/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if os.environ.get("TRANSFER_HTTP_LOG"):
                BaseHTTPRequestHandler.log_message(self, fmt, *args)

        def _host_ok(self):
            m = _HOST_RE.match((self.headers.get("Host") or "").lower())
            return bool(m) and (m.group(2) is None or m.group(2) == str(port_ref[0]))

        def _send(self, status, data, content_type, extra=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _json(self, status, obj):
            self._send(status, json.dumps(obj, ensure_ascii=False, default=_json_default).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method):
            if not self._host_ok():
                return self._json(403, {"error": "запрещённый Host"})
            url = urlparse(self.path)
            if not url.path.startswith("/api/"):
                return self._static(url.path)
            if not secrets.compare_digest(self.headers.get("X-Transfer-Token") or "", app.token):
                return self._json(403, {"error": "нет токена — обновите страницу"})
            query = {k: v[-1] for k, v in parse_qs(url.query).items()}
            body = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._json(413, {"error": "слишком большой запрос"})
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except ValueError:
                    return self._json(400, {"error": "тело запроса — не JSON"})
            try:
                result = app.dispatch(method, url.path, query, body)
            except ApiError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except TransferError as exc:
                return self._json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001 — показать в UI, а не уронить сервер
                traceback.print_exc()
                return self._json(500, {"error": "внутренняя ошибка: %s" % exc})
            if isinstance(result, RawResponse):
                return self._send(200, result.data, result.content_type)
            return self._json(200, result)

        def _static(self, path):
            if path in ("/", "/index.html"):
                with open(os.path.join(STATIC_DIR, "index.html"), "rb") as fh:
                    html = fh.read().replace(b"__TRANSFER_TOKEN__", app.token.encode())
                return self._send(200, html, CONTENT_TYPES[".html"], {
                    "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; "
                                               "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
            name = path.lstrip("/")
            full = os.path.join(STATIC_DIR, name)
            ext = os.path.splitext(name)[1]
            if "/" in name or name.startswith(".") or ext not in CONTENT_TYPES or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain; charset=utf-8")
            with open(full, "rb") as fh:
                return self._send(200, fh.read(), CONTENT_TYPES[ext])

    return Handler


def _json_default(obj):
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", "replace")
    raise TypeError(repr(obj))


def make_server(cfg, port=0):
    app = App(cfg)
    port_ref = [port]
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port_ref))
    port_ref[0] = server.server_address[1]
    return app, server


def serve(cfg, port=None, open_browser=True):
    try:
        app, server = make_server(cfg, port if port is not None else cfg.port)
    except OSError as exc:
        raise TransferError("не удалось занять порт %s: %s (занят? запустите с --port)" % (port, exc))
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    print("Transfer UI: %s" % url)
    print("Корень проектов: %s" % cfg.projects_root)
    print("Ctrl+C — остановить")
    sys.stdout.flush()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
