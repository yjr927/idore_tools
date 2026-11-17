import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.request import Request

import tiktok_draft_service as drafts

TOKEN = "act.private-test-access-token"
UPLOAD = "https://open-upload.tiktokapis.com/video/?upload_id=123&upload_token=PRIVATE_CAPABILITY"
META = {"width": 720, "height": 1280, "fps": 24, "duration": 6, "codec": "h264"}


def mp4():
    def box(kind, data): return (len(data) + 8).to_bytes(4, "big") + kind + data
    return box(b"ftyp", b"isom\0\0\0\0isom") + box(b"moov", b"") + box(b"mdat", b"example-video-bytes")


class Transport:
    def __init__(self):
        self.calls, self.uploads = [], []
        self.status = "SEND_TO_USER_INBOX"
        self.init_failure = None
        self.upload_url = UPLOAD
        self.last_range = None

    def __call__(self, request, *, timeout, max_bytes):
        self.calls.append(request)
        if request.full_url == drafts.BASE + drafts.INIT_PATH:
            if self.init_failure:
                if isinstance(self.init_failure, Exception): raise self.init_failure
                return 200, {}, json.dumps({"error": {"code": self.init_failure, "message": TOKEN + UPLOAD}}).encode()
            data = {"publish_id": "v_inbox_file~v2.12345", "upload_url": self.upload_url}
        elif request.get_method() == "PUT":
            self.uploads.append(request.data)
            self.last_range = request.get_header("Content-range")
            end, total = self.last_range.split(" ")[1].split("/")
            final = int(end.split("-")[1]) + 1 == int(total)
            return 201 if final else 206, {}, b""
        elif request.full_url == drafts.BASE + drafts.STATUS_PATH:
            data = {"status": self.status}
        else:
            raise AssertionError("unexpected endpoint")
        return 200, {}, json.dumps({"error": {"code": "ok"}, "data": data}).encode()


class TikTokDraftTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.source = self.root / "approved.mp4"
        self.source.write_bytes(mp4())
        self.digest = hashlib.sha256(mp4()).hexdigest()
        self.transport = Transport()
        self.environment = patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": TOKEN})
        self.environment.start()
        self.service = drafts.TikTokDraftService(self.root / "private", self.transport, lambda path: META)

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def send(self, service=None, **overrides):
        arguments = {"idempotency_key": "review-one", "expected_sha256": self.digest, "caption_revision": "a" * 64}
        arguments.update(overrides)
        return (service or self.service).send_draft(self.source, **arguments)

    def test_not_configured_makes_no_calls_and_does_not_reserve_an_upload(self):
        with patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": ""}):
            service = drafts.TikTokDraftService(self.root / "empty", self.transport)
        self.assertFalse(service.connection()["configured"])
        self.assertEqual(self.send(service)["status"], "not_configured")
        self.assertFalse(service.path.exists())
        self.assertEqual(self.transport.calls, [])

    def test_inbox_upload_has_no_caption_or_direct_publish_and_never_persists_secrets(self):
        result = self.send()
        self.assertEqual(result["status"], "draft_sent")
        init, upload, status = self.transport.calls
        self.assertEqual(json.loads(init.data), {"source_info": {"source": "FILE_UPLOAD",
            "video_size": len(mp4()), "chunk_size": len(mp4()), "total_chunk_count": 1}})
        self.assertEqual(init.get_header("Authorization"), "Bearer " + TOKEN)
        self.assertEqual(upload.full_url, UPLOAD)
        self.assertIsNone(upload.get_header("Authorization"))
        self.assertEqual(upload.data, mp4())
        self.assertEqual(upload.get_header("Content-range"), f"bytes 0-{len(mp4()) - 1}/{len(mp4())}")
        self.assertEqual(status.full_url, drafts.BASE + drafts.STATUS_PATH)
        self.assertFalse(result["caption_transfer_supported"])
        receipt = self.service.path.read_text()
        for private in (TOKEN, UPLOAD, "PRIVATE_CAPABILITY", str(self.source)):
            self.assertNotIn(private, receipt + json.dumps(result))
        self.assertEqual(self.service.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.send(), result)
        self.assertEqual(len(self.transport.calls), 3)

    def test_ambiguous_init_stays_reserved_after_restart_and_does_not_retry(self):
        self.transport.init_failure = RuntimeError(TOKEN + UPLOAD)
        result = self.send()
        self.assertEqual(result["status"], "needs_attention")
        self.assertNotIn(TOKEN, self.service.path.read_text())
        self.assertNotIn(UPLOAD, self.service.path.read_text())
        restarted = drafts.TikTokDraftService(self.root / "private", self.transport, lambda path: META)
        self.assertEqual(self.send(restarted), result)
        self.assertEqual(restarted.refresh("review-one"), result)
        self.assertEqual(len(self.transport.calls), 1)
        with self.assertRaisesRegex(drafts.TikTokDraftError, "approval_revision_changed"):
            self.send(restarted, caption_revision="b" * 64)

    def test_only_inbox_delivery_is_success_and_later_status_checks_do_not_initialize(self):
        self.transport.status = "PROCESSING_UPLOAD"
        self.assertEqual(self.send()["status"], "uploading")
        self.transport.status = "PUBLISH_COMPLETE"
        result = self.service.refresh("review-one")
        self.assertEqual(result["status"], "needs_attention")
        self.assertEqual(result["reason"], "creator_already_posted_in_tiktok")
        self.transport.status = "SEND_TO_USER_INBOX"
        self.assertEqual(self.service.refresh("review-one")["status"], "draft_sent")
        self.assertEqual(sum(request.full_url.endswith(drafts.INIT_PATH) for request in self.transport.calls), 1)

    def test_wrong_hash_and_invalid_video_metadata_fail_before_reserving_or_network(self):
        with self.assertRaisesRegex(drafts.TikTokDraftError, "approved_video_hash_changed"):
            self.send(expected_sha256="b" * 64)
        service = drafts.TikTokDraftService(self.root / "bad-meta", self.transport,
            lambda path: {**META, "width": 320})
        with self.assertRaisesRegex(drafts.TikTokDraftError, "outside_upload_limits"):
            self.send(service)
        self.assertEqual(self.transport.calls, [])
        self.assertFalse(service.path.exists())

    def test_chunk_ranges_cover_every_byte_once_and_merge_final_remainder(self):
        self.assertEqual(drafts.chunk_plan(4_194_304), (4_194_304, 1))
        self.assertEqual(drafts.chunk_plan(64_000_001), (10_000_000, 6))
        # Exercise streaming/range protocol with compact fixture bytes.
        with patch.object(drafts, "chunk_plan", return_value=(10, len(mp4()) // 10)):
            self.assertEqual(self.send()["status"], "draft_sent")
        uploads = [request for request in self.transport.calls if request.get_method() == "PUT"]
        self.assertEqual(b"".join(request.data for request in uploads), mp4())
        offset = 0
        for request in uploads:
            size = len(request.data)
            self.assertEqual(request.get_header("Content-length"), str(size))
            self.assertEqual(request.get_header("Content-range"), f"bytes {offset}-{offset + size - 1}/{len(mp4())}")
            offset += size

    def test_upstream_pending_limit_and_malicious_upload_url_are_safe_failures(self):
        self.transport.init_failure = "spam_risk_too_many_pending_share"
        result = self.send()
        self.assertEqual(result["reason"], "tiktok_pending_drafts_limit")
        self.assertNotIn(TOKEN, json.dumps(result))
        transport = Transport()
        transport.upload_url = UPLOAD.replace("tiktokapis.com", "tiktokapis.com.evil.example")
        service = drafts.TikTokDraftService(self.root / "wrong-url", transport, lambda path: META)
        result = self.send(service)
        self.assertEqual(result["reason"], "unsupported_tiktok_upload_address")
        self.assertEqual(len(transport.calls), 1)
        self.assertNotIn("evil.example", service.path.read_text())
        with self.assertRaises(drafts.TikTokDraftError):
            drafts._transport(Request(UPLOAD, method="PUT", headers={"Authorization": "Bearer " + TOKEN}), timeout=1, max_bytes=20)

    def test_missing_or_corrupt_private_receipts_never_silently_reset(self):
        self.assertEqual(self.service.refresh("no-such-request")["reason"], "private_receipt_missing_do_not_resubmit")
        self.service.path.write_text('{"schema_version":1,"uploads":{"old":{"status":"uploading"}}}')
        with self.assertRaisesRegex(drafts.TikTokDraftError, "private_draft_state_invalid"):
            drafts.TikTokDraftService(self.root / "private", self.transport)


if __name__ == "__main__":
    unittest.main()
