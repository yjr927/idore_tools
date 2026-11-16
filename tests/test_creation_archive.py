import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
import zipfile

from scripts.archive_generated_videos import ArchiveStopped, GitHub, final_videos, merge, trusted_run


VIDEO_ID = 'video-' + 'a' * 32
MP4 = b'\x00\x00\x00\x18ftypisom' + b'\x00' * 24


def artifact(summary, summary_name='generation-summary.json', extra=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        archive.writestr(summary_name, json.dumps(summary))
        archive.writestr('generated/' + VIDEO_ID + '.mp4', MP4)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)
    return target.getvalue()


def completed():
    return {'id': VIDEO_ID, 'status': 'completed', 'model': 'bytedance/seedance-2.5'}


class CreationArchiveTests(unittest.TestCase):
    def test_artifact_redirect_never_forwards_github_token(self):
        api, storage = MagicMock(), MagicMock()
        api.open.side_effect = HTTPError('https://api.github.com/repos/yjr927/idore_tools/actions/artifacts/1/zip',
            302, 'Found', {'Location': 'https://artifact.blob.core.windows.net/results/zip?sig=private'}, None)
        storage.open.return_value.read.return_value = b'zip'
        with patch('scripts.archive_generated_videos.build_opener', side_effect=[api, storage]):
            self.assertEqual(GitHub('yjr927/idore_tools', 'test-secret').read('/actions/artifacts/1/zip', True), b'zip')
        self.assertEqual(api.open.call_args.args[0].get_header('Authorization'), 'Bearer test-secret')
        self.assertIsNone(storage.open.call_args.args[0].get_header('Authorization'))
        api.open.side_effect = HTTPError('https://api.github.com', 302, 'Found',
            {'Location': 'https://evil.example/zip'}, None)
        with patch('scripts.archive_generated_videos.build_opener', return_value=api):
            with self.assertRaises(ArchiveStopped):
                GitHub('yjr927/idore_tools', 'test-secret').read('/actions/artifacts/1/zip', True)

    def test_only_completed_matching_provider_video(self):
        rows = final_videos(artifact(completed(), extra={'clay-bizzyboom.mp4': MP4, 'source.mp4': MP4}))
        self.assertEqual(rows, [(MP4, 'bytedance/seedance-2.5')])
        pending = completed(); pending['status'] = 'pending'
        self.assertEqual(final_videos(artifact(pending)), [])

    def test_partial_creative_failure_keeps_completed_video(self):
        summary = {'task': 'full_video', 'status': 'not_completed', 'video': completed(),
                   'draft': {'status': 'failed'}}
        self.assertEqual(len(final_videos(artifact(summary, 'creative-summary.json'))), 1)
        run = {'id': 123, 'status': 'completed', 'conclusion': 'failure', 'event': 'workflow_dispatch',
               'head_branch': 'main', 'head_repository': {'full_name': 'yjr927/idore_tools'},
               'path': '.github/workflows/creative-generation.yml'}
        self.assertEqual(trusted_run(run, 'yjr927/idore_tools'), 'openai-creative-123')

    def test_fork_and_feature_branch_are_rejected_automatically(self):
        run = {'id': 123, 'status': 'completed', 'conclusion': 'success', 'event': 'workflow_dispatch',
               'head_branch': 'feature', 'head_repository': {'full_name': 'yjr927/idore_tools'},
               'path': '.github/workflows/video-generation.yml'}
        with self.assertRaises(ArchiveStopped): trusted_run(run, 'yjr927/idore_tools')
        run['head_branch'] = 'main'; run['head_repository']['full_name'] = 'someone/fork'
        with self.assertRaises(ArchiveStopped): trusted_run(run, 'yjr927/idore_tools')

    def test_only_two_explicit_historical_runs(self):
        run = {'id': 43772946438, 'status': 'completed', 'conclusion': 'failure',
               'head_repository': {'full_name': 'yjr927/idore_tools'}}
        self.assertEqual(trusted_run(run, 'yjr927/idore_tools', True), 'api-test-short-video-43772946438')
        run['id'] = 123
        with self.assertRaises(ArchiveStopped): trusted_run(run, 'yjr927/idore_tools', True)

    def test_zip_traversal_symlink_and_size_rejected(self):
        for path in ('../source.mp4', '/source.mp4', 'a\\source.mp4'):
            with self.assertRaises(ArchiveStopped): final_videos(artifact(completed(), extra={path: MP4}))
        target = io.BytesIO()
        with zipfile.ZipFile(target, 'w') as archive:
            link = zipfile.ZipInfo('link'); link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, '../secret')
        with self.assertRaises(ArchiveStopped): final_videos(target.getvalue())
        with patch('scripts.archive_generated_videos.MAX_VIDEO', 1):
            with self.assertRaises(ArchiveStopped): final_videos(artifact(completed()))

    def test_manifest_merge_preserves_metadata_and_sha_deduplicates(self):
        with tempfile.TemporaryDirectory() as scratch:
            site = Path(scratch)
            run = {'id': 123, 'created_at': '2026-11-14T00:00:00Z',
                   'archive_created_at': '2026-11-16T02:15:10Z'}
            def fake_ffmpeg(args, **kwargs):
                Path(args[-1]).write_bytes(b'fake-poster')
            with patch('scripts.archive_generated_videos.probe', return_value=(6, '720p', '9:16')):
                with patch('scripts.archive_generated_videos.subprocess.run', side_effect=fake_ffmpeg):
                    self.assertEqual(merge(site, [(MP4, 'bytedance/seedance-2.5')], run, 'yjr927/idore_tools'), 1)
                    self.assertEqual(merge(site, [(MP4, 'bytedance/seedance-2.5')], run, 'yjr927/idore_tools'), 0)
            document = json.loads((site / 'data/creations.json').read_text())
            self.assertEqual(len(document['items']), 1)
            item = document['items'][0]
            self.assertEqual(item['created_at'], run['archive_created_at'])
            self.assertNotIn('prompt', item)
            self.assertEqual((site / item['video_path']).read_bytes(), MP4)
            self.assertEqual(item['id'], 'video-' + item['sha256'][:24])


if __name__ == '__main__':
    unittest.main()
