import copy
import json
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs

from scripts.resolve_tiktok_reference import (API_URL, MAX_JSON_BYTES,
    TikTokReferenceError, resolve_tiktok_reference)


PUBLIC = "https://www.tiktok.com/@bizzyboom/video/7706633304702848276"
# Synthetic capability, never a real source URL.
MEDIA = ("https://v16m.tiktokcdn-us.com/" + "a" * 32 + "/" + "b" * 8
         + "/video/tos/alisg/tos-alisg-pve-0068c001/example_clip/"
         + "?mime_type=video_mp4&signature=DO_NOT_EXPORT_SENTINEL")


class Transport:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.document = {"code": 0, "data": {"id": "7706633304702848276",
            "author": {"unique_id": "bizzyboom"}, "duration": 6,
            "size": 287427, "play": MEDIA}}

    def __call__(self, request, **kwargs):
        self.calls.append((request, kwargs))
        return self.status, {}, json.dumps(self.document).encode()


class TikTokReferenceTests(unittest.TestCase):
    def test_resolves_one_matching_public_video_without_exporting_capability(self):
        transport = Transport()
        result = resolve_tiktok_reference(PUBLIC + "?share_tracking=private", transport)
        self.assertEqual((result.url, result.duration), (MEDIA, 6.0))
        self.assertEqual(result.safe_summary(), {"video_id": "7706633304702848276",
            "author": "bizzyboom", "duration": 6.0, "media_type": "video/mp4"})
        self.assertNotIn("DO_NOT_EXPORT", repr(result) + json.dumps(result.safe_summary()))
        request, options = transport.calls[0]
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(request.full_url, API_URL)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(parse_qs(request.data.decode()), {"url": [PUBLIC], "hd": ["0"]})
        self.assertFalse(request.has_header("Authorization"))
        self.assertFalse(request.has_header("Cookie"))
        self.assertLessEqual(options["timeout"], 30)
        self.assertLessEqual(options["max_bytes"], 2 * 1024 * 1024)

    def test_url_confusion_and_shortlinks_never_reach_upstream(self):
        transport = Transport()
        for url in (PUBLIC.replace("https:", "http:"),
                    PUBLIC.replace("www.tiktok.com", "www.tiktok.com.evil.example"),
                    PUBLIC.replace("www.tiktok.com", "www.tiktok.com@evil.example"),
                    PUBLIC.replace("www.tiktok.com", "evil.example@www.tiktok.com"),
                    PUBLIC.replace("www.tiktok.com", "127.0.0.1"),
                    PUBLIC.replace("/@bizzyboom", "/%40bizzyboom"),
                    PUBLIC + "/extra", PUBLIC + "#private", PUBLIC + "\n",
                    "https://vm.tiktok.com/example/", "https://vt.tiktok.com/example/"):
            with self.subTest(url=url), self.assertRaises(TikTokReferenceError):
                resolve_tiktok_reference(url, transport)
        self.assertEqual(transport.calls, [])

    def test_upstream_failures_never_leak_error_bodies_or_exception_text(self):
        transport = Transport()
        transport.document = {"code": -1, "msg": MEDIA + " api-key-secret"}
        for status in (200, 302, 429, 500):
            transport.status = status
            with self.subTest(status=status), self.assertRaises(TikTokReferenceError) as caught:
                resolve_tiktok_reference(PUBLIC, transport)
            self.assertNotIn("DO_NOT_EXPORT", str(caught.exception))
            self.assertNotIn("api-key-secret", repr(caught.exception))
        def failed(*args, **kwargs):
            raise URLError(MEDIA + " api-key-secret")
        with self.assertRaisesRegex(TikTokReferenceError, "^upstream_unavailable$"):
            resolve_tiktok_reference(PUBLIC, failed)
        for raw in (b"invalid " + MEDIA.encode(), b"x" * (MAX_JSON_BYTES + 1)):
            with self.assertRaises(TikTokReferenceError) as caught:
                resolve_tiktok_reference(PUBLIC, lambda *a, **kw: (200, {}, raw))
            self.assertNotIn("DO_NOT_EXPORT", str(caught.exception))

    def test_mismatched_identity_or_untrusted_media_cannot_be_submitted(self):
        original = Transport().document
        cases = [("id", "7706633304702848277", "source_id_mismatch"),
                 ("author", {"unique_id": "someone_else"}, "source_author_mismatch"),
                 ("play", MEDIA.replace("tiktokcdn-us.com", "tiktokcdn-us.com.evil.example"), "unsupported_source_media"),
                 ("play", MEDIA.replace("v16m.tiktokcdn-us.com", "127.0.0.1"), "unsupported_source_media"),
                 ("play", MEDIA.replace("video_mp4", "text_html"), "unsupported_source_media"),
                 ("play", MEDIA.replace("https:", "http:"), "unsupported_source_media"),
                 ("size", 200_000_001, "invalid_source_size"),
                 ("duration", True, "invalid_source_duration"),
                 ("duration", 10 ** 400, "invalid_source_duration")]
        for key, value, reason in cases:
            transport = Transport()
            transport.document = copy.deepcopy(original)
            transport.document["data"][key] = value
            with self.subTest(key=key, reason=reason), self.assertRaisesRegex(TikTokReferenceError, "^" + reason + "$"):
                resolve_tiktok_reference(PUBLIC, transport)
        transport = Transport()
        transport.document["code"] = False
        with self.assertRaisesRegex(TikTokReferenceError, "^source_unavailable$"):
            resolve_tiktok_reference(PUBLIC, transport)

    def test_default_transport_refuses_redirect_without_following_it(self):
        from email.message import Message
        from urllib.error import HTTPError
        headers = Message()
        headers["Location"] = MEDIA
        with patch("scripts.resolve_tiktok_reference.build_opener") as opener:
            opener.return_value.open.side_effect = HTTPError(API_URL, 302, MEDIA, headers, None)
            with self.assertRaisesRegex(TikTokReferenceError, "^upstream_http_error$"):
                resolve_tiktok_reference(PUBLIC)
            self.assertEqual(opener.return_value.open.call_count, 1)


if __name__ == "__main__":
    unittest.main()
