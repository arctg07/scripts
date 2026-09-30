import os
import time

from helpers import TempEnv, git, tree, write

from transfer import backup, inbox, project_copy, service


class HistoryTest(TempEnv):
    def setUp(self):
        TempEnv.setUp(self)
        self.src = self.new_repo("svc")
        write(self.src, ".gitignore", ".idea/\nsecret.env\n")
        write(self.src, "gradlew", "#!/bin/sh\necho gradle\n", mode=0o755)
        write(self.src, "src/App.java", "class App {}\n")
        write(self.src, "lib/tool.jar", b"PK\x00v0")
        write(self.src, "src/Del.java", "class Del {}\n")
        self.commit(self.src, "c00 base")
        for i in range(1, 13):
            write(self.src, "src/App.java", "class App { int v = %d; }\n" % i)
            if i == 5:
                write(self.src, "lib/tool.jar", b"PK\x00v5")  # бинарный — в историю не попадает
            if i == 7:
                write(self.src, "src/Old.java", "class Old {}\n")
            if i == 9:
                os.remove(os.path.join(self.src, "src/Old.java"))
            if i == 10:
                write(self.src, "src/Del.java", "class Del { int v; }\n")
            env = dict(os.environ, GIT_AUTHOR_NAME="Автор %d" % i, GIT_AUTHOR_EMAIL="a%d@work" % i,
                       GIT_AUTHOR_DATE="2026-01-%02dT10:00:00+03:00" % i)
            git(self.src, "add", "-A")
            self._commit_env(env, "c%02d изменение\n\nподробности %d" % (i, i))
        # Незакоммиченное: правка отслеживаемого файла и новый файл.
        write(self.src, "gradlew", "#!/bin/sh\necho gradle 2\n", mode=0o755)
        write(self.src, "src/New.java", "class New {}\n")
        os.remove(os.path.join(self.src, "src/Del.java"))  # удалён, но не закоммичен
        write(self.src, ".idea/workspace.xml", "<x/>")

    def _commit_env(self, env, message):
        import subprocess
        subprocess.check_call(["git", "commit", "-q", "-m", message], cwd=self.src, env=env)

    def export(self, **kw):
        result = project_copy.copy_project(self.cfg, "svc", **kw)
        blobs = []
        for part in result["parts"]:
            with open(part["path"], "rb") as fh:
                blobs.append(fh.read())
        self.inbox_files(blobs)
        packages = inbox.scan(self.cfg)
        self.assertEqual(packages[0]["status"], "ready", packages[0]["errors"])
        return result, inbox.load(self.cfg, packages[0]["key"]), packages[0]

    def subjects(self, repo, rev="HEAD", n=20):
        return git(repo, "log", "--format=%s", "-n", str(n), rev).splitlines()

    def source_tree(self, rev):
        """Дерево коммита источника без бинарных файлов."""
        return [l for l in git(self.src, "ls-tree", "-r", rev).splitlines() if not l.endswith("lib/tool.jar")]

    def recreated_tree(self, repo, rev):
        return [l for l in git(repo, "ls-tree", "-r", rev).splitlines() if not l.endswith("lib/tool.jar")]

    def test_copy_includes_history(self):
        result, _, summary = self.export()
        self.assertEqual(len(result["history"]), 10)
        self.assertEqual(result["history"][0]["subject"], "c12 изменение")
        self.assertEqual([c["subject"] for c in summary["commits"]][-1], "c03 изменение")
        preview = project_copy.preview(self.cfg, "svc", history_limit=3)
        self.assertEqual([c["subject"] for c in preview["history"]], ["c12 изменение", "c11 изменение", "c10 изменение"])

    def test_branch_on_diverged_project(self):
        dst = self.clone_at(self.src, "home", "HEAD~11")  # c01
        write(dst, "src/Home.java", "class Home {}\n")
        git(dst, "add", "-A")
        git(dst, "commit", "-q", "-m", "home local")
        home_head = git(dst, "rev-parse", "HEAD")
        _, package, _ = self.export()

        preview = service.apply_package(self.cfg, package, target="home", dry_run=True, branch="work")
        today = time.strftime("%d-%m-%y")
        self.assertEqual(preview["branch"], "work-" + today)
        self.assertEqual(len(preview["history"]), 10)

        report = service.apply_package(self.cfg, package, target="home", branch="work")
        self.assertEqual(report["git"]["branch"], "work-" + today)
        self.assertEqual(git(dst, "symbolic-ref", "--short", "HEAD"), "work-" + today)
        self.assertEqual(git(dst, "rev-parse", "main"), home_head)  # исходная ветка не тронута
        subjects = self.subjects(dst, n=13)
        self.assertTrue(subjects[0].startswith("Импорт svc: незакоммиченные изменения"))
        self.assertEqual(subjects[1:11], ["c%02d изменение" % i for i in range(12, 2, -1)])
        self.assertTrue(subjects[11].startswith("Импорт svc: состояние на"))
        self.assertEqual(subjects[12], "home local")
        # Автор, дата и полное сообщение сохранены, деревья совпадают с источником.
        self.assertEqual(git(dst, "log", "-1", "--format=%an|%ae|%aI|%B", "HEAD~1"),
                         "Автор 12|a12@work|2026-01-12T10:00:00+03:00|c12 изменение\n\nподробности 12")
        for back in range(10):
            self.assertEqual(self.recreated_tree(dst, "HEAD~%d" % (back + 1)), self.source_tree("HEAD~%d" % back))
        self.assertEqual(self.recreated_tree(dst, "HEAD~11"), self.source_tree("HEAD~10"))
        # Рабочий каталог — снимок, всё закоммичено.
        self.assertEqual(git(dst, "status", "--porcelain"), "")
        self.assertEqual(tree(dst)["src/New.java"], ("file", b"class New {}\n", False))
        self.assertNotIn("src/Home.java", tree(dst))
        self.assertNotIn("src/Del.java", tree(dst))
        self.assertIn("src/Del.java", git(dst, "show", "--stat", "--format=", "HEAD~3"))  # правка c10 сохранена

        # Откат возвращает файлы и HEAD на main, ветка остаётся.
        result = backup.restore(self.cfg, report["backup"])
        self.assertIn("main", result["git"])
        self.assertEqual(git(dst, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(dst, "status", "--porcelain"), "")
        self.assertTrue(git(dst, "rev-parse", "--verify", "work-" + today))

    def test_branch_keeps_untracked_and_previously_ignored_files(self):
        dst = self.clone_at(self.src, "home", "HEAD~3")
        write(dst, ".gitignore", ".idea/\nsecret.env\nnotes/\n")
        write(dst, "notes/local.md", "мои заметки\n")  # игнорировался дома, в снимке .gitignore без notes/
        write(dst, "draft.md", "черновик\n")  # неотслеживаемый
        write(dst, "HomeOnly.java", "class HomeOnly {}\n")
        git(dst, "add", ".gitignore", "HomeOnly.java")
        git(dst, "commit", "-q", "-m", "home")
        _, package, _ = self.export()
        preview = service.apply_package(self.cfg, package, target="home", dry_run=True, branch="work")
        self.assertEqual(preview["deleted"], ["HomeOnly.java", "src/Del.java"])
        report = service.apply_package(self.cfg, package, target="home", branch="work")
        self.assertEqual(report["deleted"], ["HomeOnly.java", "src/Del.java"])
        got = tree(dst)
        self.assertIn("notes/local.md", got)
        self.assertIn("draft.md", got)
        self.assertNotIn("HomeOnly.java", got)
        git(dst, "checkout", "-q", "main")  # отслеживаемые домашние файлы — в прежней ветке
        self.assertIn("HomeOnly.java", tree(dst))

    def test_incremental_reuses_present_commits(self):
        dst = self.clone_at(self.src, "home", "HEAD~4")  # c08 — первые 6 коммитов истории уже есть
        _, package, _ = self.export()
        report = service.apply_package(self.cfg, package, target="home", branch="work")
        self.assertEqual(report["git"]["reused"], 6)
        subjects = self.subjects(dst, n=6)
        self.assertEqual(subjects[1:5], ["c12 изменение", "c11 изменение", "c10 изменение", "c09 изменение"])
        self.assertEqual(subjects[5], "c08 изменение")  # сразу исходный коммит, без «состояния на»
        self.assertEqual(git(dst, "status", "--porcelain"), "")

    def test_branch_name_collision(self):
        dst = self.clone_at(self.src, "home", "HEAD~2")
        git(dst, "branch", "work-" + time.strftime("%d-%m-%y"))
        _, package, _ = self.export()
        report = service.apply_package(self.cfg, package, target="home", branch="work")
        self.assertEqual(report["git"]["branch"], "work-%s-2" % time.strftime("%d-%m-%y"))

    def test_new_project_gets_history(self):
        _, package, _ = self.export(with_binaries=True)
        report = service.apply_package(self.cfg, package, target="svc-copy")
        dst = os.path.join(self.root, "svc-copy")
        self.assertEqual(git(dst, "symbolic-ref", "--short", "HEAD"), "main")
        subjects = self.subjects(dst)
        self.assertEqual(len(subjects), 12)  # состояние + 10 коммитов + незакоммиченное
        self.assertEqual(subjects[1], "c12 изменение")
        self.assertEqual(git(dst, "ls-tree", "-r", "HEAD~1"), git(self.src, "ls-tree", "-r", "HEAD"))
        self.assertEqual(git(dst, "status", "--porcelain"), "")
        self.assertEqual(len(report["git"]["commits"]), 12)

    def test_short_history_starts_at_root(self):
        self.cfg = self.make_cfg(history_commits=50)
        _, package, _ = self.export()
        service.apply_package(self.cfg, package, target="svc-copy")
        dst = os.path.join(self.root, "svc-copy")
        subjects = self.subjects(dst, n=50)
        self.assertEqual(subjects[-1], "c00 base")  # корневой коммит без «состояния на»
        self.assertEqual(len(subjects), 14)
        self.assertEqual(self.recreated_tree(dst, "HEAD~13"), self.source_tree("HEAD~12"))

    def test_without_history(self):
        dst = self.clone_at(self.src, "home", "HEAD~3")
        _, package, summary = self.export(history_limit=0)
        self.assertEqual(summary["commits"], [])
        service.apply_package(self.cfg, package, target="home", branch="work")
        self.assertTrue(self.subjects(dst, n=1)[0].startswith("Применён снимок svc"))
        self.assertEqual(self.subjects(dst, n=2)[1], "c09 изменение")
