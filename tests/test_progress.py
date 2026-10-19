import os
import threading
import time
import unittest

from helpers import TempEnv, write

from transfer import gitutil, progress, project_copy, storage


class ProgressTest(TempEnv):
    def test_snapshot_copy_logs_stages_and_git_calls(self):
        repo = self.new_repo("svc")
        for i in range(3):
            write(repo, "a.txt", "v%d\n" % i)
            self.commit(repo, "c%d" % i)
        job = progress.run(progress.Job("copy"), lambda: project_copy.copy_project(self.cfg, "svc", False, 3))
        self.assertEqual(job.status, "done", job.error)
        texts = [text for _, _, text in job.info()["lines"]]
        self.assertTrue(any(t.startswith("git log -z --first-parent") for t in texts), texts)
        self.assertTrue(any("3/3" in t and "c2" in t for t in texts), texts)
        self.assertTrue(job.result["parts"])
        self.assertFalse(job.info()["cancellable"])

    def test_cancel_kills_running_git(self):
        repo = self.new_repo("svc")
        job = progress.Job("slow")
        threading.Timer(0.3, job.cancel).start()
        t0 = time.time()
        progress.run(job, lambda: gitutil.git(repo, "-c", "alias.slow=!sleep 20", "slow"))
        self.assertEqual(job.status, "cancelled")
        self.assertLess(time.time() - t0, 5)

    def test_no_cancel_after_results_are_written(self):
        job = progress.Job("x")

        def fn():
            progress.no_cancel()
            self.assertFalse(job.cancel())
            progress.check()
            return 1
        progress.run(job, fn)
        self.assertEqual((job.status, job.result), ("done", 1))

    def test_one_job_at_a_time(self):
        gate = threading.Event()
        job = progress.start("first", gate.wait, echo=None)
        try:
            with self.assertRaises(Exception):
                progress.start("second", lambda: None, echo=None)
        finally:
            gate.set()
        for _ in range(50):
            if job.status != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job.status, "done")


class StorageTest(TempEnv):
    def test_list_and_delete(self):
        repo = self.new_repo("svc")
        write(repo, "a.txt", "a\n")
        self.commit(repo, "first")
        project_copy.copy_project(self.cfg, "svc", False, 0)
        applied = os.path.join(self.cfg.inbox_dir, "applied", "20260101-000000-x")
        write(applied, "p.txt", "data\n")
        backup = os.path.join(self.cfg.backups_dir, "svc", "2026-01-01_00-00-00-abcd")
        write(backup, "meta.json", '{"project": "svc", "kind": "snapshot"}')
        write(backup, "files/a.txt", "old\n")

        items = storage.list_items(self.cfg)["items"]
        by_area = {i["area"]: i for i in items}
        self.assertEqual(sorted(by_area), ["applied", "backups", "export"])
        self.assertEqual((by_area["export"]["project"], by_area["export"]["kind"]), ("svc", "snapshot"))
        self.assertEqual(by_area["backups"]["files"], 2)

        bad = storage.delete(self.cfg, [{"area": "applied", "key": "../../x"}, {"area": "nope", "key": "a"}])
        self.assertEqual((bad["removed"], len(bad["errors"])), ([], 2))

        result = storage.delete(self.cfg, [{"area": i["area"], "key": i["key"]} for i in items])
        self.assertEqual((len(result["removed"]), result["errors"]), (3, []))
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(storage.list_items(self.cfg)["items"], [])
        self.assertFalse(os.path.exists(os.path.join(self.cfg.backups_dir, "svc")))


if __name__ == "__main__":
    unittest.main()
