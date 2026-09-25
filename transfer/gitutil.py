"""Вызовы git. Вывод читается байтами: имена файлов и содержимое не перекодируются."""

import os
import re
import subprocess

from . import TransferError

ZERO_SHA = "0" * 40
_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


class GitError(TransferError):
    pass


def run_git(cwd, args, input=None, check=True):
    """Запускает git и возвращает (код, stdout, stderr) в байтах."""
    cmd = ["git", "-c", "core.quotepath=off"] + list(args)
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise GitError("не удалось запустить git: %s" % exc)
    out, err = proc.communicate(input)
    if check and proc.returncode != 0:
        msg = err.decode("utf-8", "replace").strip() or out.decode("utf-8", "replace").strip()
        raise GitError("git %s: %s" % (" ".join(args[:3]), msg))
    return proc.returncode, out, err


def git(cwd, *args, **kwargs):
    return run_git(cwd, args, **kwargs)[1]


def git_str(cwd, *args, **kwargs):
    return git(cwd, *args, **kwargs).decode("utf-8", "replace").rstrip("\n")


def is_work_tree(path):
    code, out, _ = run_git(path, ["rev-parse", "--is-inside-work-tree"], check=False)
    return code == 0 and out.strip() == b"true"


def has_own_repo(path):
    """Репозиторий именно этого каталога (а не объемлющего)."""
    return os.path.exists(os.path.join(path, ".git"))


def check_ref(ref):
    if not ref or ref.startswith("-") or "\x00" in ref or "\n" in ref:
        raise GitError("некорректная ссылка на коммит: %r" % ref)
    return ref


def resolve_commit(repo, ref):
    """Полный хеш коммита или None."""
    check_ref(ref)
    code, out, _ = run_git(repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"], check=False)
    if code != 0:
        return None
    return out.decode().strip()


def current_branch(repo):
    code, out, _ = run_git(repo, ["symbolic-ref", "--short", "-q", "HEAD"], check=False)
    if code == 0:
        return out.decode("utf-8", "replace").strip()
    code, out, _ = run_git(repo, ["rev-parse", "--short", "HEAD"], check=False)
    return ("(detached %s)" % out.decode().strip()) if code == 0 else None


def list_branches(repo):
    out = git_str(repo, "for-each-ref", "--sort=-committerdate", "--format=%(refname:short)",
                  "refs/heads", "refs/remotes")
    return [b for b in out.splitlines() if b and not b.endswith("/HEAD")]


def status_count(repo):
    """Количество изменённых/неотслеживаемых путей в рабочем дереве."""
    code, out, _ = run_git(repo, ["status", "--porcelain", "-z", "--untracked-files=normal"], check=False)
    if code != 0:
        return None
    return len([x for x in out.split(b"\x00") if x and len(x) > 3 and x[2:3] == b" "])


def has_identity(repo):
    for key in ("user.name", "user.email"):
        code, out, _ = run_git(repo, ["config", key], check=False)
        if code != 0 or not out.strip():
            return False
    return True


_LOG_FMT = "%x1e%H%x1f%h%x1f%an%x1f%at%x1f%P%x1f%D%x1f%s"
_STAT_RE = re.compile(r"(\d+) files? changed")


def log_commits(repo, ref="HEAD", skip=0, limit=100, query=None):
    """Коммиты для таблицы в UI: новые сверху."""
    check_ref(ref)
    args = ["log", "--format=" + _LOG_FMT, "--shortstat", "--skip=%d" % int(skip), "-n", str(int(limit))]
    if query:
        args += ["-i", "--fixed-strings", "--grep=" + query]
    args += [ref, "--"]
    commits = _parse_log(git(repo, *args))

    # Поиск по хешу: коммит показывается первым, даже если в сообщении нет совпадения.
    if query and _SHA_RE.match(query) and not skip:
        sha = resolve_commit(repo, query)
        if sha and all(c["sha"] != sha for c in commits):
            commits = _parse_log(git(repo, "log", "--format=" + _LOG_FMT, "--shortstat", "-n", "1", sha, "--")) \
                + commits
    return commits


def _parse_log(raw):
    commits = []
    for chunk in raw.decode("utf-8", "replace").split("\x1e")[1:]:
        head, _, rest = chunk.partition("\n")
        fields = head.split("\x1f")
        if len(fields) < 7:
            continue
        sha, short, author, ts, parents, refs, subject = fields[:7]
        m = _STAT_RE.search(rest)
        parents = parents.split()
        commits.append({
            "sha": sha,
            "short": short,
            "author": author,
            "time": int(ts) if ts.isdigit() else 0,
            "parents": parents,
            "merge": len(parents) > 1,
            "refs": [r.strip() for r in refs.split(",") if r.strip()],
            "subject": subject,
            "files": int(m.group(1)) if m else (0 if not parents or len(parents) == 1 else None),
        })
    return commits


def commits_info(repo, shas):
    """Сведения о списке коммитов одним вызовом git: sha -> dict как в log_commits."""
    if not shas:
        return {}
    raw = run_git(repo, ["log", "--no-walk=unsorted", "--format=" + _LOG_FMT, "--stdin"],
                  input=("\n".join(shas) + "\n").encode())[1]
    return {c["sha"]: c for c in _parse_log(raw)}


def commit_subject(repo, sha):
    return git_str(repo, "log", "-1", "--format=%s", sha, "--")


def parent_count(repo, sha):
    return len(git_str(repo, "rev-list", "--parents", "-n", "1", sha).split()) - 1


def diff_tree_raw(repo, sha):
    """Изменения коммита: список (статус, путь, новый_режим, новый_blob).

    Для merge-коммита — относительно первого родителя, для корневого — относительно пустого дерева.
    Переименования раскладываются на удаление + добавление (--no-renames).
    """
    if parent_count(repo, sha) > 1:
        args = ["diff-tree", "-r", "-z", "--no-renames", "--raw", sha + "^1", sha]
    else:
        args = ["diff-tree", "-r", "-z", "--no-commit-id", "--no-renames", "--raw", "--root", sha]
    tokens = git(repo, *args).split(b"\x00")
    result = []
    i = 0
    while i < len(tokens):
        meta = tokens[i]
        if not meta.startswith(b":"):
            i += 1
            continue
        path = os.fsdecode(tokens[i + 1]) if i + 1 < len(tokens) else ""
        i += 2
        fields = meta[1:].decode().split()
        # :old_mode new_mode old_sha new_sha status
        if len(fields) < 5:
            continue
        result.append((fields[4][:1], path, fields[1], fields[3]))
    return result


def changed_paths(repo, sha):
    """Пути, изменённые коммитом (для merge — ничего: его изменения видны в слитых коммитах)."""
    if parent_count(repo, sha) > 1:
        return []
    out = git(repo, "diff-tree", "-r", "-z", "--no-commit-id", "--no-renames", "--name-only", "--root", sha)
    return [os.fsdecode(p) for p in out.split(b"\x00") if p]


def rev_list(repo, *args, **kwargs):
    stdin = kwargs.get("stdin")
    out = run_git(repo, ["rev-list"] + list(args), input=stdin)[1]
    return out.decode().split()


class BlobReader(object):
    """Чтение содержимого объектов одним процессом `git cat-file --batch`."""

    def __init__(self, repo):
        self.proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=repo,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, spec):
        self.proc.stdin.write(spec.encode() + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline()
        if not header or header.endswith(b"missing\n"):
            raise GitError("объект не найден: %s" % spec)
        size = int(header.split()[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)
        return data

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.stdout.close()
        finally:
            self.proc.wait()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _pathspec_chunks(paths, limit=60000):
    chunk, size = [], 0
    for p in paths:
        if chunk and size + len(p) > limit:
            yield chunk
            chunk, size = [], 0
        chunk.append(p)
        size += len(p) + 1
    if chunk:
        yield chunk


def filter_ignored(repo, paths):
    """Убирает пути, которые .gitignore прячет от git (кроме уже отслеживаемых)."""
    if not paths:
        return []
    data = b"".join(os.fsencode(p) + b"\x00" for p in paths)
    code, out, _ = run_git(repo, ["check-ignore", "--stdin", "-z"], input=data, check=False)
    ignored = set(os.fsdecode(p) for p in out.split(b"\x00") if p) if code == 0 else set()
    return [p for p in paths if p not in ignored]


def commit_paths(repo, paths, message):
    """Коммит только перечисленных путей.

    Если в индексе уже были чужие изменения, коммит не делается: иначе в него попали бы и они.
    Возвращает (sha или None, предупреждение или None).
    """
    code, _, _ = run_git(repo, ["diff", "--cached", "--quiet"], check=False)
    if code != 0:
        return None, "в индексе git уже есть подготовленные изменения — коммит не создан, сделайте его вручную"
    if not has_identity(repo):
        return None, "в git не заданы user.name/user.email — коммит не создан " \
                     "(git config --global user.name ...; git config --global user.email ...)"
    paths = filter_ignored(repo, list(paths))
    for chunk in _pathspec_chunks(paths):
        run_git(repo, ["add", "-A", "--"] + chunk)
    code, _, _ = run_git(repo, ["diff", "--cached", "--quiet"], check=False)
    if code == 0:
        return None, "изменений для коммита нет"
    run_git(repo, ["commit", "-q", "-m", message])
    return git_str(repo, "rev-parse", "HEAD"), None


def init_repo(repo, branch=None):
    run_git(repo, ["init", "-q"])
    if branch and not branch.startswith("("):
        run_git(repo, ["symbolic-ref", "HEAD", "refs/heads/" + branch], check=False)


def commit_all(repo, message, force_paths=None):
    """Коммит всего дерева. force_paths добавляются даже под .gitignore: в исходном репозитории
    такие файлы были отслеживаемыми (иначе не попали бы в снимок)."""
    if not has_identity(repo):
        return None, "в git не заданы user.name/user.email — стартовый коммит не создан " \
                     "(git config --global user.name ...; git config --global user.email ...)"
    run_git(repo, ["add", "-A"])
    for chunk in _pathspec_chunks(force_paths or []):
        run_git(repo, ["add", "-f", "--"] + chunk)
    code, _, _ = run_git(repo, ["diff", "--cached", "--quiet"], check=False)
    if code == 0:
        return None, "файлов для коммита нет"
    run_git(repo, ["commit", "-q", "-m", message])
    return git_str(repo, "rev-parse", "HEAD"), None
