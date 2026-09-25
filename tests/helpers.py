"""Временные репозитории и сравнение деревьев для тестов."""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from transfer.config import DEFAULTS, Config  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def run(cwd, *args, **kwargs):
    return subprocess.check_output(list(args), cwd=cwd, stderr=subprocess.STDOUT, **kwargs)


def git(cwd, *args):
    return run(cwd, "git", *args).decode("utf-8", "replace").strip()


def write(root, rel, data, mode=None):
    path = os.path.join(root, rel)
    if not os.path.isdir(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
    with open(path, "wb") as fh:
        fh.write(data.encode("utf-8") if isinstance(data, str) else data)
    if mode is not None:
        os.chmod(path, mode)


def tree(root, skip=(".git",)):
    """rel -> (тип, содержимое/цель симлинка, exec)."""
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip and not os.path.islink(os.path.join(dirpath, d))]
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames + [d for d in os.listdir(dirpath) if os.path.islink(os.path.join(dirpath, d))
                                 and os.path.isdir(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            rel = name if rel_dir == "." else os.path.join(rel_dir, name).replace(os.sep, "/")
            if os.path.islink(full):
                result[rel] = ("link", os.readlink(full), False)
            else:
                with open(full, "rb") as fh:
                    result[rel] = ("file", fh.read(), bool(os.stat(full).st_mode & stat.S_IXUSR))
    return result


class TempEnv(unittest.TestCase):
    """Корень проектов и каталог данных во временной папке; git с собственной идентичностью."""

    max_lines = 7000

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="transfer-test-")
        self.root = os.path.join(self.tmp, "root")
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.root)
        os.makedirs(self.data)
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".gitconfig"), "w") as fh:
            fh.write("[user]\n\tname = Test\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n"
                     "[core]\n\tautocrlf = false\n")
        self._env = dict(os.environ)
        os.environ["HOME"] = home
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        os.environ.pop("GIT_CONFIG_GLOBAL", None)
        self.cfg = self.make_cfg()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_cfg(self, **overrides):
        data = dict(DEFAULTS, projects_root=self.root, max_lines=self.max_lines)
        data.update(overrides)
        return Config(data, SCRIPTS_DIR, self.data)

    def new_repo(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        git(path, "init", "-q")
        git(path, "symbolic-ref", "HEAD", "refs/heads/main")
        return path

    def commit(self, repo, message):
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(repo, "rev-parse", "HEAD")

    def clone_at(self, src, name, rev):
        dst = os.path.join(self.root, name)
        git(self.root, "clone", "-q", src, dst)
        git(dst, "checkout", "-q", "-B", "main", rev)
        return dst

    def inbox_files(self, parts, names=None):
        """Кладёт части в inbox под произвольными именами."""
        inbox = self.cfg.inbox_dir
        if not os.path.isdir(inbox):
            os.makedirs(inbox)
        paths = []
        for i, data in enumerate(parts):
            name = names[i] if names else "incoming-%d.txt" % i
            path = os.path.join(inbox, name)
            with open(path, "wb") as fh:
                fh.write(data)
            paths.append(path)
        return paths
