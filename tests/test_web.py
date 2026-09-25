import http.client
import json
import threading
import unittest

from helpers import TempEnv, write

from transfer.web.server import make_server


class WebTest(TempEnv):
    def setUp(self):
        TempEnv.setUp(self)
        repo = self.new_repo("svc")
        write(repo, "a.txt", "a\n")
        self.commit(repo, "first")
        write(repo, "a.txt", "b\n")
        self.sha = self.commit(repo, "second")
        self.app, self.server = make_server(self.cfg, 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.daemon = True
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        TempEnv.tearDown(self)

    def request(self, method, path, body=None, token=True, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or "127.0.0.1:%d" % self.port}
        if token:
            headers["X-Transfer-Token"] = self.app.token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        ctype = resp.getheader("Content-Type") or ""
        return resp.status, (json.loads(raw.decode()) if "json" in ctype else raw)

    def test_index_has_token_and_csp(self):
        status, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertIn(self.app.token.encode(), body)

    def test_api_requires_token_and_local_host(self):
        self.assertEqual(self.request("GET", "/api/projects", token=False)[0], 403)
        self.assertEqual(self.request("GET", "/api/projects", host="evil.example:%d" % self.port)[0], 403)
        status, body = self.request("GET", "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["projects"]], ["svc"])

    def test_static_rejects_traversal(self):
        self.assertEqual(self.request("GET", "/../server.py", token=False)[0], 404)
        self.assertEqual(self.request("GET", "/%2e%2e/server.py", token=False)[0], 404)

    def test_copy_then_apply_flow(self):
        status, commits = self.request("GET", "/api/projects/svc/commits?limit=5")
        self.assertEqual(status, 200)
        self.assertEqual(commits["commits"][0]["sha"], self.sha)

        status, preview = self.request("POST", "/api/copy/preview", {"project": "svc", "mode": "commits",
                                                                     "commits": [self.sha]})
        self.assertEqual(status, 200, preview)
        self.assertEqual([f["path"] for f in preview["files"]], ["a.txt"])

        status, result = self.request("POST", "/api/copy", {"project": "svc", "mode": "last", "ref": "main"})
        self.assertEqual(status, 200, result)
        part = result["parts"][0]
        status, text = self.request("GET", "/api/export/%s/%s" % (part["project_dir"], part["name"]))
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith(b"#@transfer v1 kind=changes project=svc"))

        status, saved = self.request("POST", "/api/inbox", {"text": text.decode()})
        self.assertEqual(status, 200)
        pkg = saved["packages"][0]
        self.assertEqual((pkg["status"], pkg["project"]), ("ready", "svc"))

        status, report = self.request("POST", "/api/apply", {"key": pkg["key"], "dry_run": True})
        self.assertEqual(status, 200, report)
        self.assertEqual(report["unchanged"], 1)  # применяем к тому же проекту — ничего не меняется

        status, err = self.request("POST", "/api/apply", {"key": pkg["key"], "target": "../x"})
        self.assertEqual(status, 400)
        self.assertIn("error", err)

    def test_errors_are_json(self):
        status, body = self.request("GET", "/api/projects/nope/branches")
        self.assertEqual(status, 400)
        self.assertIn("не найден", body["error"])
        status, body = self.request("GET", "/api/nothing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
