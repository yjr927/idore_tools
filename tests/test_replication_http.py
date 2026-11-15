import copy
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock

from replication_service import ReplicationError
from server import Server, Store


ASSET_ID = "asset-0123456789abcdef"
TREND_ID = "tiktok-7699497385185119502"
PLAN_ID = "replication-" + "b" * 32


class FakeReplicationService:
    def __init__(self):
        self.calls = []

    def catalog(self):
        self.calls.append(("catalog",))
        return {"assets": [{"id": ASSET_ID, "material_group": "male"}],
                "references": [{"id": TREND_ID, "material_group": "male"}]}

    def sync(self):
        self.calls.append(("sync",))
        return {"synced": True, "reference_count": 1}

    def classify_asset(self, ident, body):
        self.calls.append(("classify_asset", ident, copy.deepcopy(body)))
        return {"updated": [ident], "asset": {"id": ident, **body}}

    def classify_reference(self, ident, body):
        self.calls.append(("classify_reference", ident, copy.deepcopy(body)))
        return {"updated": [ident], "reference": {"id": ident, **body}}

    def prepare(self, body):
        self.calls.append(("prepare", copy.deepcopy(body)))
        return {"plan_id": PLAN_ID, "can_submit": True, "blockers": [],
                "required_inputs": ["character_image", "reference_video"]}

    def submit(self, body):
        self.calls.append(("submit", copy.deepcopy(body)))
        if body.get("consent") is not True:
            raise ReplicationError("请明确确认本次素材传输与生成费用。", 409)
        return {"job": {"id": "video-" + "a" * 32, "status": "pending"},
                "asset_id": ASSET_ID, "reference_id": TREND_ID, "plan_id": body.get("plan_id")}


class ReplicationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.replication = FakeReplicationService()
        self.video = Mock()
        self.creative = Mock()
        self.server = Server(("127.0.0.1", 0), self.store, video_service=self.video,
            creative_service=self.creative, replication_service=self.replication)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, method="GET", body=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        encoded = raw if raw is not None else json.dumps(body) if body is not None else None
        connection.request(method, path, encoded, request_headers)
        response = connection.getresponse()
        content = response.read()
        result = response.status, dict(response.getheaders()), content
        connection.close()
        return result

    def assert_no_provider_generation(self):
        self.video.submit.assert_not_called()
        self.creative.generate_images.assert_not_called()
        self.creative.generate_draft.assert_not_called()

    def test_catalog_sync_classification_and_preparation_do_not_implicitly_generate(self):
        status, _, raw = self.request("/api/replication/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["assets"][0]["id"], ASSET_ID)
        self.assertEqual(json.loads(raw)["references"][0]["id"], TREND_ID)
        self.assertEqual(self.request("/api/replication/sync", "POST", {})[0], 200)
        classification = {"material_group": "female", "subject_group": "solo", "apply_character": False}
        status, _, raw = self.request("/api/replication/assets/" + ASSET_ID, "PATCH", classification)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["asset"]["material_group"], "female")
        reference_classification = {"material_group": "female", "subject_group": "solo"}
        status, _, raw = self.request("/api/replication/references/" + TREND_ID, "PATCH", reference_classification)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["reference"]["subject_group"], "solo")
        selection = {"asset_id": ASSET_ID, "reference_id": TREND_ID,
            "reference_video_url": "https://media.example/motion.mp4", "reference_video_confirmed": True, "prompt": "Repeat the short gesture."}
        status, _, raw = self.request("/api/replication/prepare", "POST", selection)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["plan_id"], PLAN_ID)
        self.assertTrue(json.loads(raw)["can_submit"])
        self.assertEqual(self.replication.calls, [("catalog",), ("sync",),
            ("classify_asset", ASSET_ID, classification),
            ("classify_reference", TREND_ID, reference_classification), ("prepare", selection)])
        self.assert_no_provider_generation()

    def test_job_submission_requires_explicit_post_and_preserves_public_service_rejection(self):
        body = {"plan_id": PLAN_ID, "consent": True, "idempotency_key": "one-submit",
                "model": "bytedance/seedance-2.0-fast", "duration": 5, "resolution": "720p", "aspect_ratio": "9:16", "generate_audio": False}
        status, _, raw = self.request("/api/replication/jobs", "POST", body)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(raw)["job"]["status"], "pending")
        self.assertEqual(self.replication.calls[-1], ("submit", body))
        status, _, raw = self.request("/api/replication/jobs", "POST", {"consent": False})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {"error": "请明确确认本次素材传输与生成费用。"})
        # The HTTP layer delegates paid consent to the selected service; it does
        # not also dispatch an independent provider request.
        self.assert_no_provider_generation()

    def test_host_origin_and_json_guards_stop_before_replication_service(self):
        for headers, expected in (({"Host": "attacker.example"}, 403),
                ({"Origin": "https://attacker.example"}, 403),
                ({"Sec-Fetch-Site": "cross-site"}, 403), ({"Content-Type": "text/plain"}, 415)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/replication/jobs", "POST", {}, headers)[0], expected)
        for raw in ("{bad", "[]", "null", '{"duration": NaN}'):
            self.assertEqual(self.request("/api/replication/prepare", "POST", raw=raw)[0], 400)
        self.assertEqual(self.request("/api/replication/catalog", headers={"Host": "attacker.example"})[0], 403)
        self.assertEqual(self.request("/api/replication/catalog", "OPTIONS")[0], 403)
        self.assertFalse(self.replication.calls)
        self.assert_no_provider_generation()

    def test_read_methods_invalid_assets_and_paths_cannot_trigger_mutations(self):
        cases = [("/api/replication/jobs", "GET"), ("/api/replication/jobs", "HEAD"),
            ("/api/replication/prepare", "GET"), ("/api/replication/sync", "GET"),
            ("/api/replication/jobs", "PUT"), ("/api/replication/jobs", "DELETE"),
            ("/api/replication/assets/asset-not-valid", "PATCH"),
            ("/api/replication/references/tiktok-not-valid", "PATCH"),
            ("/api/replication/references/tiktok-123", "PATCH"),
            ("/api/replication/assets/asset-0123456789abcdef/extra", "PATCH"),
            ("/api/replication/%2e%2e/state", "GET")]
        for path, method in cases:
            with self.subTest(path=path, method=method):
                status, _, raw = self.request(path, method, {} if method in ("PUT", "DELETE", "PATCH") else None)
                self.assertIn(status, (400, 404))
                if method == "HEAD": self.assertEqual(raw, b"")
        self.assertFalse(self.replication.calls)
        self.assert_no_provider_generation()


if __name__ == "__main__":
    unittest.main()
