import os
import shutil
import unittest

from helpers import FIXTURES, TempEnv, git, run, tree, write

from transfer import TransferError, backup, formats, git_apply, git_copy, inbox, service


class GitCopyApplyTest(TempEnv):
    def setUp(self):
        TempEnv.setUp(self)
        self.src = self.new_repo("svc")
        write(self.src, "a.txt", "a\n")
        write(self.src, "src/Old.java", "class Old {}\n")
        write(self.src, "rename-me.txt", "r\n")
        self.base = self.commit(self.src, "base")

        write(self.src, "a.txt", "a\nb\n")
        write(self.src, "src/New.java", "class New {}\n")
        self.c1 = self.commit(self.src, "c1: add New, change a")

        os.remove(os.path.join(self.src, "src/Old.java"))
        os.rename(os.path.join(self.src, "rename-me.txt"), os.path.join(self.src, "renamed.txt"))
        write(self.src, "run.sh", "#!/bin/sh\necho hi\n", mode=0o755)
        write(self.src, "файл й.txt", "юникод\n")
        write(self.src, "crlf.txt", b"a\r\nb\r\n")
        write(self.src, "no-eol.txt", b"tail")
        write(self.src, "blank-tail.txt", b"x\n\n\n")
        write(self.src, "empty.txt", b"")
        write(self.src, "logo.bin", b"PNG\x00\x01\x02")
        os.symlink("a.txt", os.path.join(self.src, "link"))
        self.c2 = self.commit(self.src, "c2: misc")

        # Ветка и merge-коммит.
        git(self.src, "checkout", "-q", "-b", "feature")
        write(self.src, "feature.txt", "f\n")
        self.cf = self.commit(self.src, "feature work")
        git(self.src, "checkout", "-q", "main")
        write(self.src, "a.txt", "a\nb\nc\n")
        self.c3 = self.commit(self.src, "c3: change a again")
        git(self.src, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
        self.merge = git(self.src, "rev-parse", "HEAD")

    def expected_tree(self, skip=("logo.bin", "link")):
        t = tree(self.src)
        for k in skip:
            t.pop(k, None)
        return t

    def copy_and_load(self, refs):
        result = git_copy.copy_commits(self.cfg, "svc", refs)
        blobs = []
        for part in result["parts"]:
            with open(part["path"], "rb") as fh:
                blobs.append(fh.read())
        self.inbox_files(blobs)
        packages = inbox.scan(self.cfg)
        self.assertEqual(len(packages), 1)
        return result, inbox.load(self.cfg, packages[0]["key"])

    def test_range_roundtrip_is_exact(self):
        result, package = self.copy_and_load(["%s..HEAD" % self.base])
        skipped = {s["path"]: s["reason"] for s in result["skipped"]}
        self.assertEqual(skipped, {"logo.bin": "бинарный", "link": "симлинк"})
        work = self.clone_at(self.src, "svc-work", self.base)
        report = service.apply_package(self.cfg, package, target="svc-work")
        self.assertIsNone(report["error"])
        self.assertEqual(tree(work), self.expected_tree())
        self.assertIn("rename-me.txt", report["deleted"])
        self.assertTrue(os.access(os.path.join(work, "run.sh"), os.X_OK))
        # Пакет ушёл из inbox в applied.
        self.assertEqual(inbox.scan(self.cfg), [])

    def test_small_parts(self):
        self.cfg = self.make_cfg(max_lines=7)
        result, package = self.copy_and_load(["%s..HEAD" % self.base])
        self.assertGreater(len(result["parts"]), 3)
        work = self.clone_at(self.src, "svc-work", self.base)
        service.apply_package(self.cfg, package, target="svc-work")
        self.assertEqual(tree(work), self.expected_tree())

    def test_merge_commit_uses_first_parent(self):
        plan = git_copy.plan_copy(self.src, [self.merge])
        self.assertEqual([f["path"] for f in plan["files"]], ["feature.txt"])

    def test_default_is_head(self):
        plan = git_copy.plan_copy(self.src, [])
        self.assertEqual([c["sha"] for c in plan["commits"]], [self.merge])

    def test_unselected_commit_between_is_reported(self):
        plan = git_copy.plan_copy(self.src, [self.c3, self.c1])
        self.assertEqual([c["sha"] for c in plan["commits"]], [self.c1, self.c3])  # порядок истории
        between = plan["between"]["commits"]
        self.assertEqual([c["sha"] for c in between], [])  # c2 не менял a.txt / New.java
        plan = git_copy.plan_copy(self.src, [self.c1, self.c2])
        self.assertEqual(plan["between"]["commits"], [])
        write(self.src, "a.txt", "zzz\n")
        mid = self.commit(self.src, "touches a.txt")
        write(self.src, "a.txt", "yyy\n")
        last = self.commit(self.src, "touches a.txt again")
        plan = git_copy.plan_copy(self.src, [self.c3, last])
        self.assertEqual([c["sha"] for c in plan["between"]["commits"]], [mid])

    def test_bad_refs(self):
        with self.assertRaises(TransferError):
            git_copy.expand_refs(self.src, ["no-such-branch"])
        with self.assertRaises(TransferError):
            git_copy.expand_refs(self.src, ["--output=/tmp/x"])

    def test_dry_run_changes_nothing(self):
        _, package = self.copy_and_load(["%s..HEAD" % self.base])
        work = self.clone_at(self.src, "svc-work", self.base)
        before = tree(work)
        report = service.apply_package(self.cfg, package, target="svc-work", dry_run=True)
        self.assertEqual(tree(work), before)
        self.assertIn("src/New.java", report["new"])
        self.assertIn("a.txt", report["updated"])
        self.assertEqual(len(inbox.scan(self.cfg)), 1)

    def test_commit_after_apply(self):
        _, package = self.copy_and_load([self.c1])
        work = self.clone_at(self.src, "svc-work", self.base)
        report = service.apply_package(self.cfg, package, target="svc-work", commit=True)
        self.assertTrue(report["git"]["commit"])
        self.assertEqual(git(work, "log", "-1", "--format=%s"), "c1: add New, change a")
        self.assertEqual(git(work, "status", "--porcelain"), "")

    def test_commit_each_commit_separately(self):
        git(self.src, "commit", "-q", "--amend", "--no-edit", "--author", "Автор <author@example.com>",
            "--date", "2026-01-02T03:04:05")
        self.merge = git(self.src, "rev-parse", "HEAD")
        _, package = self.copy_and_load([self.c1, self.c2, self.c3, self.merge])
        work = self.clone_at(self.src, "svc-work", self.base)
        report = service.apply_package(self.cfg, package, target="svc-work", commit=True)
        self.assertEqual([c["subject"] for c in report["git"]["commits"]],
                         ["c1: add New, change a", "c2: misc", "c3: change a again", "merge feature"])
        self.assertEqual(git(work, "log", "--format=%s", "%s..HEAD" % self.base).splitlines(),
                         ["merge feature", "c3: change a again", "c2: misc", "c1: add New, change a"])
        fmt = "--format=%an <%ae> %ad %B"
        self.assertEqual(git(work, "log", "-1", fmt, "--date=raw"), git(self.src, "log", "-1", fmt, "--date=raw"))
        self.assertEqual(git(work, "log", "-1", "--format=%cn"), "Test")
        # Каждый коммит меняет то же, что в источнике (a.txt: промежуточная версия в c1, итоговая в c3).
        new = git(work, "rev-list", "--reverse", "%s..HEAD" % self.base).split()
        for old, sha in zip((self.c1, self.c2, self.c3, self.merge), new):
            want = git(self.src, "diff-tree", "-r", "--no-renames", "--no-commit-id", old + "^", old).splitlines()
            self.assertEqual(git(work, "diff-tree", "-r", "--no-commit-id", sha).splitlines(),
                             [l for l in want if not l.endswith(("logo.bin", "link"))])
        self.assertEqual(git(work, "status", "--porcelain"), "")

    def test_commit_skips_already_present(self):
        _, package = self.copy_and_load([self.c1, self.c3])
        work = self.clone_at(self.src, "svc-work", self.c1)
        report = service.apply_package(self.cfg, package, target="svc-work", commit=True)
        self.assertEqual([c["subject"] for c in report["git"]["commits"]], ["c3: change a again"])
        self.assertEqual(report["git"]["reused"], 1)
        self.assertEqual(git(work, "status", "--porcelain"), "")

    def test_commit_in_project_inside_bigger_repo(self):
        _, package = self.copy_and_load([self.c1, self.c3])
        outer = os.path.join(self.root, "outer")
        git(self.root, "clone", "-q", self.src, os.path.join(outer, "svc-work"))
        work = os.path.join(outer, "svc-work")
        git(work, "checkout", "-q", "-B", "main", self.base)
        shutil.rmtree(os.path.join(work, ".git"))
        git(outer, "init", "-q")
        self.commit(outer, "outer base")
        self.cfg = self.make_cfg(projects_root=outer)
        report = service.apply_package(self.cfg, package, target="svc-work", commit=True)
        self.assertEqual(len(report["git"]["commits"]), 2)
        self.assertEqual(git(outer, "show", "HEAD~1:svc-work/a.txt"), "a\nb")
        self.assertEqual(git(outer, "show", "HEAD:svc-work/a.txt"), "a\nb\nc")
        self.assertEqual(git(outer, "status", "--porcelain"), "")

    def test_commit_skipped_when_index_has_foreign_changes(self):
        _, package = self.copy_and_load([self.c1])
        work = self.clone_at(self.src, "svc-work", self.base)
        write(work, "other.txt", "x\n")
        git(work, "add", "other.txt")
        report = service.apply_package(self.cfg, package, target="svc-work", commit=True)
        self.assertIsNone(report["git"]["commit"])
        self.assertTrue(any("индексе" in w for w in report["warnings"]))

    def test_backup_and_restore(self):
        _, package = self.copy_and_load(["%s..%s" % (self.base, self.c2)])
        work = self.clone_at(self.src, "svc-work", self.base)
        before = tree(work)
        report = service.apply_package(self.cfg, package, target="svc-work")
        self.assertNotEqual(tree(work), before)
        backup.restore(self.cfg, report["backup"])
        self.assertEqual(tree(work), before)
        with self.assertRaises(TransferError):
            backup.restore(self.cfg, report["backup"])  # повторно нельзя

    def test_changes_require_existing_project(self):
        _, package = self.copy_and_load([self.c1])
        with self.assertRaisesRegex(TransferError, "проект не найден"):
            service.apply_package(self.cfg, package, target="nope")

    def test_rejects_path_traversal(self):
        payload = formats.build_changes([], [("../evil.txt", b"x\n")], [])
        work = self.clone_at(self.src, "svc-work", self.base)
        with self.assertRaises(TransferError):
            git_apply.apply_changes(self.cfg, payload, "svc-work")
        payload = formats.build_changes([], [(".git/hooks/post-checkout", b"x\n")], [])
        with self.assertRaises(TransferError):
            git_apply.apply_changes(self.cfg, payload, "svc-work")
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.txt")))

    def test_rejects_write_through_symlinked_dir(self):
        work = self.clone_at(self.src, "svc-work", self.base)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(work, "escape"))
        payload = formats.build_changes([], [("escape/pwned.txt", b"x\n")], [])
        with self.assertRaises(TransferError):
            git_apply.apply_changes(self.cfg, payload, "svc-work")
        self.assertEqual(os.listdir(outside), [])

    @unittest.skipUnless(shutil.which("bash"), "нужен bash")
    def test_output_is_readable_by_legacy_git_apply(self):
        # a.txt меняют оба коммита: промежуточная версия уходит в блок VERSION, старый скрипт его пропускает.
        result = git_copy.copy_commits(self.cfg, "svc", [self.c1, self.c3])
        with open(result["parts"][0]["path"], "rb") as fh:
            self.assertIn(b"\nVERSION: %s a.txt\n" % self.c1.encode(), fh.read())
        work = self.clone_at(self.src, "svc-work", self.base)
        os.makedirs(os.path.join(work, "sh"))
        shutil.copy(os.path.join(FIXTURES, "legacy_git_apply.sh"), os.path.join(work, "sh", "git-apply.sh"))
        run(work, "bash", "sh/git-apply.sh", result["parts"][0]["path"])
        shutil.rmtree(os.path.join(work, "sh"))
        got = tree(work)
        for rel in ("a.txt", "src/New.java"):
            self.assertEqual(got[rel], (
                "file", git(self.src, "show", "%s:%s" % (self.c3, rel)).encode() + b"\n", False))
        self.assertEqual(git(work, "status", "--porcelain"), "M a.txt\n?? src/New.java")  # ничего лишнего


if __name__ == "__main__":
    unittest.main()
