import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server import Server, Store
from video_service import VideoError


JOB_ID = "video-12345678123412341234123456789abc"
FILE_NAME = "video-" + "a" * 32 + ".mp4"


class FakeVideoService:
    def __init__(self):
        self.calls = []
        self.private_key = ""

    def connection(self):
        self.calls.append(("connection",))
        return {"connected": bool(self.private_key), "provider": "openrouter"}

    def connect(self, body):
        self.calls.append(("connect", body))
        self.private_key = body.get("api_key", "")
        return {"connected": bool(self.private_key)}

    def disconnect(self):
        self.calls.append(("disconnect",))
        self.private_key = ""
        return {"connected": False}

    def models(self):
        self.calls.append(("models",))
        return {"data": [{"id": "bytedance/seedance-test"}]}

    def jobs(self):
        self.calls.append(("jobs",))
        return [{"id": JOB_ID, "status": "pending"}]

    def submit(self, body):
        self.calls.append(("submit", body))
        if body.get("reject"):
            raise VideoError("参数无效；未提交生成。", 422)
        return {"id": JOB_ID, "status": "pending"}

    def refresh(self, ident):
        self.calls.append(("refresh", ident))
        return {"id": ident, "status": "completed"}

    def download(self, ident):
        self.calls.append(("download", ident))
        return {"id": ident, "status": "completed", "result_url": "/generated/" + FILE_NAME}


class VideoHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.service = FakeVideoService()
        self.server = Server(("127.0.0.1", 0), self.store, video_service=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, headers=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        request_body = raw_body if raw_body is not None else json.dumps(body) if body is not None else None
        conn.request(method, path, request_body, request_headers)
        response = conn.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        conn.close()
        return result

    def test_provider_routes_dispatch_explicit_actions_and_propagate_public_errors(self):
        status, _, _ = self.request("/api/video/connection", "POST", {"api_key": "test-private-key"})
        self.assertEqual(status, 200)
        for path, expected_call in (("/api/video/connection", "connection"),
                ("/api/video/models", "models"), ("/api/video/jobs", "jobs")):
            self.assertEqual(self.request(path)[0], 200)
            self.assertEqual(self.service.calls[-1][0], expected_call)
        body = {"model": "bytedance/seedance-test", "prompt": "A character waves", "consent": True}
        status, _, raw = self.request("/api/video/jobs", "POST", body)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["id"], JOB_ID)
        self.assertEqual(self.service.calls[-1], ("submit", body))
        for action in ("refresh", "download"):
            self.assertEqual(self.request(f"/api/video/jobs/{JOB_ID}/{action}", "POST", {})[0], 200)
            self.assertEqual(self.service.calls[-1], (action, JOB_ID))
        status, _, raw = self.request("/api/video/jobs", "POST", {"reject": True})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(raw), {"error": "参数无效；未提交生成。"})
        self.assertEqual(self.request("/api/video/connection", "DELETE", {})[0], 200)
        self.assertEqual(self.service.calls[-1], ("disconnect",))

    def test_reads_and_unsupported_methods_never_submit_or_download(self):
        for path, method in (("/api/video/jobs", "GET"), ("/api/video/jobs", "HEAD"),
                (f"/api/video/jobs/{JOB_ID}/refresh", "GET"),
                (f"/api/video/jobs/{JOB_ID}/download", "GET"),
                (f"/api/video/jobs/{JOB_ID}/download", "HEAD"),
                ("/api/video/jobs", "PUT"), ("/api/video/jobs", "PATCH")):
            with self.subTest(path=path, method=method):
                status, _, raw = self.request(path, method, {} if method in ("PUT", "PATCH") else None)
                self.assertIn(status, (200, 404))
                if method == "HEAD": self.assertEqual(raw, b"")
        self.assertFalse(any(call[0] in ("submit", "refresh", "download") for call in self.service.calls))

    def test_local_origin_and_json_boundaries_apply_to_paid_routes(self):
        cases = [({"Host": "attacker.example"}, 403), ({"Origin": "https://attacker.example"}, 403),
            ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Type": "text/plain"}, 415)]
        for headers, expected in cases:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/video/jobs", "POST", {}, headers)[0], expected)
        for body in ("{bad", "[]", "null", '{"duration": NaN}'):
            self.assertEqual(self.request("/api/video/jobs", "POST", raw_body=body)[0], 400)
        self.assertEqual(self.request("/api/video/models", "OPTIONS")[0], 403)
        self.assertEqual(self.request("/api/video/models", headers={"Host": "attacker.example"})[0], 403)
        self.assertFalse(self.service.calls)

    def test_private_connection_data_and_arbitrary_files_are_not_public(self):
        private_key = "test-private-key-do-not-return"
        self.request("/api/video/connection", "POST", {"api_key": private_key})
        for path in ("/api/state", "/api/video/connection", "/api/video/jobs"):
            status, _, raw = self.request(path)
            self.assertEqual(status, 200)
            self.assertNotIn(private_key.encode(), raw)
        for path in ("/video_service.py", "/data/state.json", "/.env", "/generated/key.json",
                "/generated/credentials", "/generated/video-wrong.mp4", "/generated/%2e%2e/state.json",
                "/api/video/jobs/not-a-local-id/download"):
            with self.subTest(path=path):
                self.assertIn(self.request(path)[0], (400, 404))
        self.assertNotIn(private_key, (Path(self.tmp.name) / "state.json").read_text())

    def test_generated_result_serves_bounded_ranges_and_rejects_symlinks(self):
        directory = Path(self.tmp.name) / "generated"; directory.mkdir(exist_ok=True)
        raw_video = b"\x00\x00\x00\x18ftypmp42" + bytes(range(50))
        (directory / FILE_NAME).write_bytes(raw_video)
        path = "/generated/" + FILE_NAME
        status, headers, raw = self.request(path, headers={"Range": "bytes=4-11"})
        self.assertEqual(status, 206)
        self.assertEqual(raw, raw_video[4:12])
        self.assertEqual(headers["Content-Range"], f"bytes 4-11/{len(raw_video)}")
        self.assertEqual(headers["Content-Type"], "video/mp4")
        status, headers, raw = self.request(path, "HEAD", headers={"Range": "bytes=4-11"})
        self.assertEqual(status, 206); self.assertEqual(raw, b"")
        self.assertEqual(int(headers["Content-Length"]), 8)
        self.assertEqual(self.request(path, headers={"Range": "bytes=99999-"})[0], 416)
        link_name = "video-" + "b" * 32 + ".mp4"
        (directory / link_name).symlink_to(Path(self.tmp.name) / "state.json")
        self.assertEqual(self.request("/generated/" + link_name)[0], 404)

    def test_generation_page_is_an_explicit_local_entry_point(self):
        status, headers, raw = self.request("/generate")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<html", raw.lower())
        self.assertFalse(any(call[0] == "submit" for call in self.service.calls))


if __name__ == "__main__":
    unittest.main()
