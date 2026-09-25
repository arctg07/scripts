import glob
import io
import os
import shutil
import tarfile
import unicodedata
import unittest

from helpers import FIXTURES, TempEnv, git, run, tree, write

from transfer import TransferError, backup, inbox, project_apply, project_copy, service


class SnapshotTest(TempEnv):
    def setUp(self):
        TempEnv.setUp(self)
        self.src = self.new_repo("svc")
        write(self.src, ".gitignore", "build/\n.idea/\nsecret.env\nreports/\n")
        write(self.src, "gradlew", "#!/bin/sh\necho gradle\n", mode=0o755)
        write(self.src, "src/main/App.java", "class App {}\n")
        write(self.src, "docs/файл й.md", "юникод\n")
        write(self.src, "lib/tool.jar", b"PK\x03\x04\x00\x00")
        write(self.src, "reports/tracked.md", "tracked despite ignore\n")
        git(self.src, "add", "-f", "reports/tracked.md")
        os.symlink("src/main/App.java", os.path.join(self.src, "app-link"))
        self.commit(self.src, "base")
        write(self.src, "untracked-new.txt", "new\n")  # неигнорируемый — попадает
        write(self.src, "secret.env", "TOKEN=1\n")  # игнорируемый — не попадает
        write(self.src, "build/out.class", b"\xca\xfe")
        write(self.src, ".idea/workspace.xml", "<x/>")

    def snapshot_parts(self, name="svc", **kw):
        result = project_copy.copy_project(self.cfg, name, **kw)
        blobs = []
        for part in result["parts"]:
            with open(part["path"], "rb") as fh:
                blobs.append(fh.read())
        return result, blobs

    def load_single(self):
        packages = inbox.scan(self.cfg)
        self.assertEqual(len(packages), 1, packages)
        self.assertEqual(packages[0]["status"], "ready", packages[0]["errors"])
        return inbox.load(self.cfg, packages[0]["key"])

    def expected(self):
        t = tree(self.src, skip=(".git", "build", ".idea"))
        for rel in ("secret.env", "lib/tool.jar"):
            t.pop(rel)
        return t

    def test_create_new_project(self):
        self.cfg = self.make_cfg(max_lines=5)
        result, blobs = self.snapshot_parts()
        self.assertEqual(result["mode"], "git")
        self.assertEqual(result["keep"], ["lib/tool.jar"])
        self.assertGreater(len(blobs), 1)
        self.inbox_files(list(reversed(blobs)), ["x.txt"] + ["y%d" % i for i in range(len(blobs) - 1)])
        report = service.apply_package(self.cfg, self.load_single(), target="svc-copy")
        self.assertTrue(report["create"])
        dst = os.path.join(self.root, "svc-copy")
        self.assertEqual(tree(dst), self.expected())
        self.assertTrue(report["git"]["commit"])
        self.assertEqual(git(dst, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(git(dst, "status", "--porcelain"), "")
        self.assertIn("reports/tracked.md", git(dst, "ls-files"))
        # Создание проекта попадает в историю, но не откатывается.
        with self.assertRaisesRegex(TransferError, "создан этим применением"):
            backup.restore(self.cfg, report["backup"])

    def test_apply_to_existing_project(self):
        _, blobs = self.snapshot_parts()
        dst = os.path.join(self.root, "svc-work")
        shutil.copytree(self.src, dst, symlinks=True)
        write(dst, "src/main/App.java", "class App { changed }\n")
        write(dst, "src/main/Stale.java", "class Stale {}\n")  # нет в снимке — удаляется
        write(dst, ".idea/local.xml", "<y/>")  # настройки IDE не удаляются
        write(dst, "lib/tool.jar", b"PK-local\x00")  # KEEP — не трогается
        os.remove(os.path.join(dst, "gradlew"))
        self.inbox_files(blobs)
        package = self.load_single()

        preview = service.apply_package(self.cfg, package, target="svc-work", dry_run=True)
        self.assertEqual(preview["deleted"], ["src/main/Stale.java"])
        self.assertEqual(preview["updated"], ["src/main/App.java"])
        self.assertEqual(preview["new"], ["gradlew"])

        before = tree(dst)
        report = service.apply_package(self.cfg, package, target="svc-work")
        after = tree(dst)
        self.assertEqual(after["src/main/App.java"], tree(self.src)["src/main/App.java"])
        self.assertNotIn("src/main/Stale.java", after)
        self.assertFalse(os.path.exists(os.path.join(dst, "src/main/Stale.java")))
        self.assertEqual(after[".idea/local.xml"], before[".idea/local.xml"])
        self.assertEqual(after["lib/tool.jar"], ("file", b"PK-local\x00", False))
        self.assertTrue(after["gradlew"][2])

        backup.restore(self.cfg, report["backup"])
        self.assertEqual(tree(dst), before)

    def test_too_many_deletions_needs_force(self):
        _, blobs = self.snapshot_parts()
        dst = os.path.join(self.root, "other")
        os.makedirs(dst)
        for i in range(30):
            write(dst, "src/Other%d.java" % i, "class Other%d {}\n" % i)
        self.inbox_files(blobs)
        package = self.load_single()
        report = service.apply_package(self.cfg, package, target="other")
        self.assertTrue(report["blocked"])
        self.assertEqual(len(os.listdir(os.path.join(dst, "src"))), 30)  # ничего не тронуто
        report = service.apply_package(self.cfg, package, target="other", force=True)
        self.assertFalse(report["blocked"])
        self.assertEqual(os.listdir(os.path.join(dst, "src")), ["main"])  # Other*.java удалены

    def test_project_without_git(self):
        plain = os.path.join(self.root, "plain")
        write(plain, "src/A.java", "class A {}\n")
        write(plain, "src/main/build/Keep.java", "package build;\n")  # build/ внутри src — обычный пакет
        write(plain, "build/libs/app.jar", b"\x00")
        write(plain, "target/x.txt", "x")
        write(plain, "node_modules/m/index.js", "x")
        write(plain, "A.iml", "<m/>")
        result, blobs = self.snapshot_parts("plain")
        self.assertEqual(result["mode"], "find")
        self.assertTrue(result["warnings"])
        self.inbox_files(blobs)
        report = service.apply_package(self.cfg, self.load_single(), target="plain-copy", init_git=False)
        self.assertEqual(sorted(tree(os.path.join(self.root, "plain-copy"))), ["src/A.java", "src/main/build/Keep.java"])
        self.assertIsNone(report["git"])

    def test_with_binaries(self):
        _, blobs = self.snapshot_parts(with_binaries=True)
        self.inbox_files(blobs)
        service.apply_package(self.cfg, self.load_single(), target="svc-copy")
        got = tree(os.path.join(self.root, "svc-copy"))
        self.assertEqual(got["lib/tool.jar"], ("file", b"PK\x03\x04\x00\x00", False))

    def test_rejects_malicious_manifest(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in (("manifest.txt", b"FORMAT\t2\nFILE\t../evil.txt\n"), ("files/../evil.txt", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        with self.assertRaises(TransferError):
            project_apply.apply_snapshot(self.cfg, buf.getvalue(), "evil-target")
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.txt")))

    def test_rejects_symlink_escape(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            manifest = b"FORMAT\t2\nFILE\tesc\nFILE\tesc/pwned.txt\n"
            for name, data in (("manifest.txt", manifest), ("files/esc/pwned.txt", b"x")):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            link = tarfile.TarInfo("files/esc")
            link.type = tarfile.SYMTYPE
            link.linkname = self.tmp
            tar.addfile(link)
        with self.assertRaises(TransferError):
            project_apply.apply_snapshot(self.cfg, buf.getvalue(), "esc-target")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pwned.txt")))

    @unittest.skipUnless(shutil.which("bash") and shutil.which("base64"), "нужны bash и base64")
    def test_reads_legacy_bash_snapshot(self):
        os.makedirs(os.path.join(self.src, "sh"))
        shutil.copy(os.path.join(FIXTURES, "legacy_project_copy.sh"), os.path.join(self.src, "sh", "project-copy.sh"))
        run(self.src, "bash", "sh/project-copy.sh", env=dict(os.environ, MAX_ENCODED_LINES="5"))
        legacy = sorted(glob.glob(os.path.join(self.src, "sh", "export", "project-copy-*-part-*.txt")))
        self.assertGreater(len(legacy), 1)
        os.makedirs(self.cfg.inbox_dir)
        for path in legacy:
            shutil.copy(path, self.cfg.inbox_dir)
        packages = inbox.scan(self.cfg)
        self.assertEqual(len(packages), 1)
        self.assertTrue(packages[0]["legacy"])
        self.assertEqual(packages[0]["status"], "ready", packages[0]["errors"])
        package = inbox.load(self.cfg, packages[0]["key"])
        with self.assertRaisesRegex(TransferError, "укажите проект"):
            service.apply_package(self.cfg, package)
        service.apply_package(self.cfg, package, target="from-legacy")
        got = tree(os.path.join(self.root, "from-legacy"))
        self.assertEqual(got["src/main/App.java"], tree(self.src)["src/main/App.java"])
        self.assertTrue(got["gradlew"][2])

    @unittest.skipUnless(shutil.which("bash") and shutil.which("tar") and shutil.which("base64"), "нужны утилиты")
    def test_bootstrap_one_liner(self):
        _, blobs = self.snapshot_parts()
        paths = self.inbox_files(blobs)
        boot = os.path.join(self.tmp, "boot")
        os.makedirs(boot)
        decode = "base64 -d" if run(self.tmp, "bash", "-c", "echo QQ== | base64 -d 2>/dev/null || true") == b"A" \
            else "base64 -D"
        run(self.tmp, "bash", "-c", "cat %s | grep -v '^#@transfer' | %s | tar -xzf - -C %s" % (
            " ".join("'%s'" % p for p in paths), decode, boot))
        # bsdtar на macOS распаковывает не-ASCII имена в NFD — сравниваем в NFC.
        got = {unicodedata.normalize("NFC", k): v for k, v in tree(os.path.join(boot, "files")).items()}
        self.assertEqual(got, self.expected())


if __name__ == "__main__":
    unittest.main()
