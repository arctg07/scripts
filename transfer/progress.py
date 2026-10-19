"""Ход долгих операций: лог, прогресс и остановка.

Операция выполняется внутри Job (в веб-UI — в отдельном потоке). Код движка пишет в лог через
progress.log() / progress.step(), не зная, кто его вызвал: без активной задачи (тесты, CLI без -v)
эти вызовы ничего не делают. gitutil.run_git отмечает в логе каждый вызов git с длительностью,
а при остановке убивает текущий процесс git.
"""

import os
import secrets
import signal
import sys
import threading
import time
import traceback

from . import TransferError

MAX_LINES = 20000
SLOW_GIT = 2.0  # секунд: такие вызовы git выделяются в логе
KEEP_JOBS = 20

_local = threading.local()
_jobs = {}
_jobs_lock = threading.Lock()


class Cancelled(TransferError):
    pass


def fmt_size(n):
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return ("%d %s" % (n, unit)) if unit == "Б" else ("%.1f %s" % (n, unit))
        n /= 1024.0


class Job(object):
    def __init__(self, title, meta=None, echo=None):
        self.id = secrets.token_hex(6)
        self.title = title
        self.meta = meta or {}
        self.echo = echo  # поток, куда дублируется лог (консоль ui.sh или stderr CLI)
        self.started = time.time()
        self.finished = None
        self.status = "running"  # running | done | error | cancelled
        self.result = None
        self.error = None
        self.cancellable = True
        self.stage = None
        self.done = 0
        self.total = 0
        self._lines = []
        self._dropped = 0
        self._cancel = threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    # --- запись -----------------------------------------------------------------------------------

    def log(self, text, level="info"):
        t = time.time() - self.started
        with self._lock:
            self._lines.append((round(t, 2), level, text))
            if len(self._lines) > MAX_LINES:
                cut = MAX_LINES // 2
                del self._lines[:cut]
                self._dropped += cut
        if self.echo:
            try:
                self.echo.write("[%7.2fs] %s%s\n" % (t, level.upper() + ": " if level in ("warn", "error") else "", text))
                self.echo.flush()
            except (OSError, ValueError):
                pass

    def step(self, stage, done, total):
        if stage != self.stage:
            self.log("%s (%d)" % (stage, total) if total else stage, "stage")
        self.stage, self.done, self.total = stage, done, total
        self.check()

    def check(self):
        if self._cancel.is_set() and self.cancellable:
            raise Cancelled("операция остановлена")

    # --- остановка --------------------------------------------------------------------------------

    def cancel(self):
        """False — операцию уже нельзя прервать (идёт запись результата) или она завершена."""
        if self.status != "running" or not self.cancellable:
            return False
        self._cancel.set()
        self.log("запрошена остановка", "warn")
        with self._lock:
            proc = self._proc
        if proc is not None:
            _kill(proc)
        return True

    def no_cancel(self):
        """С этого места операция доводится до конца: дальше пишутся файлы результата."""
        self.check()
        self.cancellable = False

    def attach(self, proc):
        with self._lock:
            self._proc = proc
        if self._cancel.is_set() and self.cancellable:
            _kill(proc)

    def detach(self):
        with self._lock:
            self._proc = None

    # --- чтение -----------------------------------------------------------------------------------

    def info(self, since=0):
        with self._lock:
            start = max(0, since - self._dropped)
            lines = self._lines[start:]
            nxt = self._dropped + len(self._lines)
        end = self.finished or time.time()
        data = {"id": self.id, "title": self.title, "meta": self.meta, "status": self.status,
                "elapsed": round(end - self.started, 1), "stage": self.stage, "done": self.done,
                "total": self.total, "cancellable": self.cancellable and self.status == "running",
                "cancelling": self._cancel.is_set(), "lines": [list(x) for x in lines], "next": nxt,
                "error": self.error}
        if self.status == "done":
            data["result"] = self.result
        return data


def _kill(proc):
    if os.name == "posix":
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


# --- Вызовы из кода движка -------------------------------------------------------------------------

def current():
    return getattr(_local, "job", None)


def log(text, level="info"):
    job = current()
    if job:
        job.log(text, level)


def step(stage, done, total):
    job = current()
    if job:
        job.step(stage, done, total)


def check():
    job = current()
    if job:
        job.check()


def no_cancel():
    job = current()
    if job:
        job.no_cancel()


class timed(object):
    """with progress.timed("Архив"): ... — в лог пишется начало и длительность этапа."""

    def __init__(self, label):
        self.label = label

    def __enter__(self):
        self.t0 = time.time()
        log(self.label + "…", "stage")
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            log("%s — %.2f с" % (self.label, time.time() - self.t0))


# --- Запуск ----------------------------------------------------------------------------------------

def run(job, fn):
    """Выполняет fn() в контексте job в текущем потоке. Ошибки остаются в job, не пробрасываются."""
    prev = current()
    _local.job = job
    try:
        job.result = fn()
        job.status = "done"
        job.log("готово за %.1f с" % (time.time() - job.started), "ok")
    except Cancelled as exc:
        job.status, job.error = "cancelled", str(exc)
        job.log("остановлено через %.1f с" % (time.time() - job.started), "warn")
    except TransferError as exc:
        job.status, job.error = "error", str(exc)
        job.log("ошибка: %s" % exc, "error")
    except Exception as exc:  # noqa: BLE001 — показать в UI, а не потерять в потоке
        job.status, job.error = "error", "внутренняя ошибка: %s" % exc
        for line in traceback.format_exc().rstrip().splitlines():
            job.log(line, "error")
    finally:
        job.cancellable = False
        job.finished = time.time()
        _local.job = prev
    return job


def run_verbose(title, fn, echo=sys.stderr):
    """Для CLI: fn() с подробным логом в echo; ошибка пробрасывается как TransferError."""
    job = run(Job(title, echo=echo), fn)
    if job.status != "done":
        raise TransferError(job.error)
    return job.result


def start(title, fn, meta=None, echo=sys.stderr):
    """Запускает fn в фоновом потоке. Одновременно выполняется одна задача."""
    with _jobs_lock:
        busy = active()
        if busy:
            raise TransferError("уже выполняется: %s — дождитесь окончания или остановите" % busy.title)
        job = Job(title, meta, echo)
        _jobs[job.id] = job
        for old in sorted(_jobs.values(), key=lambda j: j.started)[:-KEEP_JOBS]:
            if old.status != "running":
                del _jobs[old.id]
    thread = threading.Thread(target=run, args=(job, fn), name="job-" + job.id)
    thread.daemon = True
    thread.start()
    return job


def get(job_id):
    return _jobs.get(job_id)


def active():
    for job in list(_jobs.values()):
        if job.status == "running":
            return job
    return None
