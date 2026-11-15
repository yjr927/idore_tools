import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from server import Server, Store
from creative_service import CreativeError


class FakeCreative:
    def __init__(self):
        self.calls = []
        self.key = ''

    def connection(self):
        return {'connected': bool(self.key), 'provider': 'OpenAI', 'key_storage': 'memory'}

    def connect(self, body):
        self.key = body['api_key']
        return self.connection()

    def disconnect(self):
        self.key = ''
        return self.connection()

    def models(self):
        return {'image_models': [{'id': 'image-model'}], 'text_models': [{'id': 'text-model'}]}

    def jobs(self):
        return {'jobs': []}

    def generate_images(self, body):
        self.calls.append(('images', body))
        return {'kind': 'images', 'status': 'completed'}

    def generate_draft(self, body):
        self.calls.append(('draft', body))
        if body.get('reject'):
            raise CreativeError('参数未获支持。', 422)
        return {'kind': 'draft', 'status': 'completed'}


class CreativeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.creative = FakeCreative()
        self.server = Server(('127.0.0.1', 0), self.store, creative_service=self.creative)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, method='GET', body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        supplied = {'Content-Type': 'application/json'}
        supplied.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, supplied)
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), raw
        connection.close()
        return result

    def test_connection_does_not_expose_key_or_mix_video_credentials(self):
        key = 'test-openai-secret-value'
        status, _, body = self.request('/api/creative/connection', 'POST', {'api_key': key})
        self.assertEqual(status, 200)
        self.assertNotIn(key.encode(), body)
        for endpoint in ('/api/state', '/api/creative/jobs', '/api/creative/connection'):
            self.assertNotIn(key.encode(), self.request(endpoint)[2])
        self.assertFalse(json.loads(self.request('/api/video/connection')[2])['connected'])
        self.assertEqual(self.request('/api/creative/connection', 'DELETE', {})[0], 200)
        self.assertFalse(self.creative.connection()['connected'])

    def test_explicit_posts_dispatch_separate_paid_operations(self):
        for endpoint, kind in (('images', 'images'), ('drafts', 'draft')):
            body = {'consent': True, 'idempotency_key': kind}
            status, _, result = self.request('/api/creative/' + endpoint, 'POST', body)
            self.assertEqual(status, 201)
            self.assertEqual(json.loads(result)['kind'], kind)
            self.assertEqual(self.creative.calls[-1], (kind, body))
        status, _, result = self.request('/api/creative/drafts', 'POST', {'reject': True})
        self.assertEqual(status, 422)
        self.assertEqual(json.loads(result), {'error': '参数未获支持。'})

    def test_reads_do_not_trigger_generation(self):
        for endpoint in ('connection', 'models', 'jobs', 'images', 'drafts'):
            for method in ('GET', 'HEAD'):
                status, _, body = self.request('/api/creative/' + endpoint, method)
                self.assertIn(status, (200, 404))
                if method == 'HEAD':
                    self.assertEqual(body, b'')
        for method in ('PUT', 'PATCH'):
            self.assertEqual(self.request('/api/creative/images', method, {})[0], 404)
        self.assertEqual(self.creative.calls, [])

    def test_origin_host_and_content_type_rejected_before_provider(self):
        for headers, code in (({'Origin': 'https://elsewhere.example'}, 403),
                ({'Host': 'elsewhere.example'}, 403), ({'Content-Type': 'text/plain'}, 415)):
            self.assertEqual(self.request('/api/creative/images', 'POST', {}, headers)[0], code)
        self.assertEqual(self.creative.calls, [])

    def test_generated_images_are_exact_local_files_with_private_files_unserved(self):
        directory = Path(self.tmp.name) / 'generated'
        directory.mkdir(exist_ok=True)
        filename = 'style-' + 'a' * 32 + '-before.png'
        raw = b'\x89PNG\r\n\x1a\nlocal-result'
        (directory / filename).write_bytes(raw)
        status, headers, body = self.request('/generated/' + filename)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertEqual(body, raw)
        self.assertEqual(self.request('/generated/' + filename, headers={'Range': 'bytes=0-7'})[0], 206)
        (directory / ('style-' + 'b' * 32 + '-after.png')).symlink_to(directory / filename)
        self.assertEqual(self.request('/generated/style-' + 'b' * 32 + '-after.png')[0], 404)
        for path in ('/creative-jobs.json', '/data/creative-jobs.json', '/generated/../creative-jobs.json'):
            self.assertIn(self.request(path)[0], (400, 404))


if __name__ == '__main__':
    unittest.main()
