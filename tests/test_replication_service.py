import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from replication_service import (ReplicationError, ReplicationService, CANDIDATE_URL,
                                 MAX_IMAGE_BYTES, _effective_reference, _transport)
from video_service import VideoError
from urllib.request import Request
from urllib.error import HTTPError

ASSET_A = 'asset-' + 'a' * 16
ASSET_B = 'asset-' + 'b' * 16
ASSET_C = 'asset-' + 'c' * 16
REF_A = 'tiktok-7704859708200389902'
REF_B = 'tiktok-7703871221569424661'
MEDIA = 'https://media.example.com/actual-action.mp4?signature=private-media-marker'
PNG = b'\x89PNG\r\n\x1a\nprivate-character-image-bytes'


def video(ident=REF_A, **extra):
    result = dict(id=ident, url='https://www.tiktok.com/@creator/video/' + ident[7:],
        title='Observed original dance', source_kind='chatgpt_work', original_watched=True,
        material_group='female', checks={'solo': True}, first_seen_at='2026-11-12T01:31:04Z',
        last_seen_at='2026-11-12T01:31:04Z', kind='hands', motion_summary='One person repeats a gesture.')
    result.update(extra)
    return result


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.media = self.directory / 'media'
        self.media.mkdir()
        self.lock = threading.RLock()
        self.data = {'assets': []}
        self.saved = 0
        for ident, character in ((ASSET_A, 'ExistingRole'), (ASSET_B, 'ExistingRole'), (ASSET_C, 'OtherRole')):
            self.data['assets'].append(dict(id=ident, name='User character image', character=character, stage='daily',
                kind='image', rights='owned', approved=True, is_demo=False, url='/media/' + ident + '.png'))
            (self.media / (ident + '.png')).write_bytes(PNG)

    def save(self):
        self.saved += 1
        (self.directory / 'state.json').write_text(json.dumps(self.data))


class Video:
    def __init__(self):
        self.requests = []
        self.results = {}
        self.failure = None
        self.hook = None

    def submit(self, body):
        if self.hook:
            self.hook(body)
        token = body['idempotency_key']
        if token in self.results:
            return copy.deepcopy(self.results[token])
        self.requests.append(copy.deepcopy(body))
        job = dict(id='video-' + 'd' * 32, idempotency_key=token, status='pending', model=body['model'])
        self.results[token] = job
        if self.failure:
            job['status'] = 'submission_uncertain'
            raise self.failure
        return copy.deepcopy(job)


class ReplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.provider = Video()
        self.path = Path(self.tmp.name) / 'candidates.json'
        self.document = {'videos': [video()], 'runs': []}
        self.write()
        self.network = []
        def transport(request, **kwargs):
            self.network.append((request, kwargs))
            return 200, {}, json.dumps(self.document).encode()
        self.service = ReplicationService(self.store, self.provider, candidate_path=self.path, request_fn=transport)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self):
        self.path.write_text(json.dumps(self.document))

    def classify(self, ident=ASSET_A, material='female', subject='solo', **extra):
        return self.service.classify_asset(ident, dict(material_group=material, subject_group=subject, **extra))

    def prepare(self, **extra):
        values = dict(asset_id=ASSET_A, reference_id=REF_A, reference_video_url=MEDIA, reference_video_confirmed=True)
        values.update(extra)
        return self.service.prepare(values)

    def submit(self, plan, **extra):
        values = dict(plan_id=plan['plan_id'], idempotency_key='explicit-one', model='bytedance/seedance-2.5',
                      duration=5, resolution='720p', aspect_ratio='9:16', generate_audio=False, consent=True)
        values.update(extra)
        return self.service.submit(values)

    def test_catalog_real_existing_library_images_only_without_guessing_gender(self):
        self.store.data['assets'].extend([
            dict(self.store.data['assets'][0], id='asset-' + 'e' * 16, is_demo=True),
            dict(self.store.data['assets'][0], id='asset-' + 'f' * 16, rights='unknown'),
            dict(self.store.data['assets'][0], id='asset-' + '1' * 16, approved=False),
            dict(self.store.data['assets'][0], id='asset-' + '2' * 16, kind='video')])
        result = self.service.catalog()
        self.assertEqual(len(result['assets']), 3)
        self.assertEqual(result['assets'][0]['character'], 'ExistingRole')
        self.assertEqual(result['assets'][0]['material_group'], 'unknown')
        self.assertEqual(result['assets'][0]['subject_group'], 'unknown')
        self.assertFalse(result['assets'][0]['ready'])
        self.assertEqual(result['references'][0]['material_group'], 'female')
        self.assertEqual(result['references'][0]['capture_date'], '2026-11-11')
        self.assertFalse(self.network)
        self.assertFalse(self.provider.requests)

    def test_explicit_classification_single_or_same_character_only(self):
        result = self.classify()
        self.assertEqual(result['updated'], [ASSET_A])
        self.assertNotIn('material_group', self.store.data['assets'][1])
        self.assertEqual(self.store.data['assets'][0]['classification_method'], 'manual')
        result = self.classify(material='male', apply_character=True)
        self.assertEqual(result['updated'], [ASSET_A, ASSET_B])
        self.assertNotIn('material_group', self.store.data['assets'][2])
        self.assertEqual(self.store.saved, 2)
        self.assertFalse(self.provider.requests)

    def test_invalid_classification_cannot_change_other_fields_or_bulk_by_default(self):
        before = copy.deepcopy(self.store.data)
        for body in ({'material_group': [], 'subject_group': 'solo'},
                     {'material_group': 'female', 'subject_group': 'solo', 'api_key': 'private-key'},
                     {'material_group': 'female', 'subject_group': 'solo', 'apply_character': 'true'},
                     {'material_group': 'girl', 'subject_group': 'solo'}):
            with self.subTest(body=body), self.assertRaises(ReplicationError):
                self.service.classify_asset(ASSET_A, body)
        self.assertEqual(self.store.data, before)
        with self.assertRaises(ReplicationError):
            self.classify('asset-not-real')

    def test_explicit_local_reference_label_persists_without_changing_public_capture_or_history(self):
        self.document['videos'][0]['material_group'] = 'unknown'
        self.document['runs'] = [{'id': 'existing-run', 'videos': [copy.deepcopy(self.document['videos'][0])]}]
        self.document['work_daily_selections'] = {'2026-11-15': {'kpop': [REF_A]}}
        self.write()
        public_before = self.path.read_bytes()
        assets_before = copy.deepcopy(self.store.data)
        result = self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        reference = result['reference']
        self.assertEqual(reference['material_group'], 'male')
        self.assertEqual(reference['subject_group'], 'solo')
        self.assertEqual(reference['classification_method'], 'manual_user')
        self.assertTrue(reference['classification_local'])
        self.assertTrue(reference['ready'])
        self.assertEqual(self.path.read_bytes(), public_before)
        self.assertEqual(self.store.data, assets_before)
        self.assertEqual(self.store.saved, 0)
        saved = json.loads(self.service.reference_classifications_path.read_text())
        item = saved['classifications'][0]
        self.assertEqual(set(item), {'reference_id', 'material_group', 'subject_group', 'classified_at'})
        self.assertEqual(item['reference_id'], REF_A)
        self.assertEqual(self.service.reference_classifications_path.stat().st_mode & 0o777, 0o600)
        serialized = json.dumps(saved)
        for private in ('https://', 'Observed original dance', MEDIA, 'base64', 'api_key'):
            self.assertNotIn(private, serialized)
        repeated = self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        self.assertEqual(repeated['reference']['classification_at'], reference['classification_at'])
        restarted = ReplicationService(self.store, self.provider, candidate_path=self.path)
        self.assertEqual(restarted.catalog()['references'][0]['material_group'], 'male')
        self.assertEqual(restarted.catalog()['references'][0]['classification_at'], reference['classification_at'])
        self.assertFalse(self.provider.requests)
        self.assertFalse(self.network)

    def test_manual_unknown_reference_override_blocks_and_invalidates_ready_plan(self):
        self.classify()
        plan = self.prepare()
        self.assertTrue(plan['can_submit'])
        result = self.service.classify_reference(REF_A, {'material_group': 'unknown', 'subject_group': 'unknown'})
        self.assertFalse(result['reference']['ready'])
        self.assertEqual(result['reference']['material_group'], 'unknown')
        self.assertEqual(result['reference']['subject_group'], 'unknown')
        self.assertFalse(self.prepare()['can_submit'])
        with self.assertRaises(ReplicationError):
            self.submit(plan)
        self.assertFalse(self.provider.requests)

    def test_reference_manual_known_labels_require_existing_watched_source(self):
        self.document['videos'][0].update(source_kind='tiktok_public_browser', original_watched=False)
        self.write()
        with self.assertRaises(ReplicationError) as unseen:
            self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        self.assertEqual(unseen.exception.status, 409)
        self.assertFalse(self.service.reference_classifications_path.exists())
        result = self.service.classify_reference(REF_A, {'material_group': 'unknown', 'subject_group': 'unknown'})
        self.assertFalse(result['reference']['original_watched'])
        self.assertFalse(result['reference']['ready'])
        for ident in ('https://www.tiktok.com/@creator/video/123', 'tiktok-7700000000000000000', '../file'):
            with self.subTest(ident=ident), self.assertRaises(ReplicationError) as absent:
                self.service.classify_reference(ident, {'material_group': 'male', 'subject_group': 'solo'})
            self.assertEqual(absent.exception.status, 404)

    def test_reference_manual_labels_cannot_forge_watch_proof_or_accept_unknown_fields(self):
        for body in ({'material_group': [], 'subject_group': 'solo'},
                     {'material_group': 'male', 'subject_group': 'solo', 'original_watched': True},
                     {'material_group': 'male', 'subject_group': 'solo', 'reference_video_url': MEDIA},
                     {'material_group': 'male', 'subject_group': 'pair'},
                     {'material_group': 'male'}):
            with self.subTest(body=body), self.assertRaises(ReplicationError):
                self.service.classify_reference(REF_A, body)
        self.assertFalse(self.service.reference_classifications_path.exists())
        self.assertFalse(self.provider.requests)

    def test_reference_override_does_not_restore_missing_or_revoked_public_watch_evidence(self):
        self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        self.document['videos'][0].update(source_kind='tiktok_public_browser', original_watched=False)
        self.write()
        reference = self.service.catalog()['references'][0]
        self.assertFalse(reference['original_watched'])
        self.assertFalse(reference['ready'])
        self.assertEqual(reference['material_group'], 'unknown')
        self.assertEqual(reference['classification_method'], 'manual_pending_watch')
        self.document['videos'] = []
        self.write()
        self.assertEqual(self.service.catalog()['references'], [])

    def test_reference_classification_save_failure_rolls_back_and_symlinks_rejected(self):
        before = self.service.catalog()['references'][0]
        with patch.object(self.service, '_save_reference_classifications', side_effect=ReplicationError('Unavailable', 500)):
            with self.assertRaises(ReplicationError):
                self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        self.assertEqual(self.service.catalog()['references'][0], before)
        outside = Path(self.tmp.name) / 'outside-classifications.json'
        outside.write_text('{"schema_version":1,"classifications":[]}')
        self.service.reference_classifications_path.symlink_to(outside)
        with self.assertRaises(ReplicationError):
            self.service.classify_reference(REF_A, {'material_group': 'male', 'subject_group': 'solo'})
        with self.assertRaises(ReplicationError):
            ReplicationService(self.store, self.provider, candidate_path=self.path)
        self.assertEqual(json.loads(outside.read_text())['classifications'], [])

    def test_missing_or_share_reference_media_returns_blockers_without_api_calls(self):
        self.classify()
        for changes in (dict(reference_video_url=''), dict(reference_video_confirmed=False),
                        dict(reference_video_url='https://www.tiktok.com/@creator/video/7704859708200389902'),
                        dict(reference_video_url='https://127.0.0.1/private.mp4')):
            with self.subTest(changes=changes):
                plan = self.prepare(**changes)
                self.assertFalse(plan['can_submit'])
                self.assertTrue(plan['blockers'])
                self.assertEqual(plan['required_inputs'], ['character_image', 'reference_video'])
                with self.assertRaises(ReplicationError):
                    self.submit(plan)
        self.assertFalse(self.network)
        self.assertFalse(self.provider.requests)
        self.assertFalse(self.service.links_path.exists())

    def test_unknown_gender_mismatch_and_multi_person_are_hard_blockers(self):
        plan = self.prepare()
        self.assertFalse(plan['can_submit'])
        self.classify(material='male')
        self.assertFalse(self.prepare()['can_submit'])
        self.classify(subject='group')
        self.assertFalse(self.prepare()['can_submit'])
        self.classify()
        self.document['videos'][0]['checks']['solo'] = False
        self.write()
        self.assertFalse(self.prepare()['can_submit'])
        self.document['videos'][0].update(material_group='unknown', checks={'solo': True})
        self.write()
        self.assertFalse(self.prepare()['can_submit'])
        self.assertFalse(self.provider.requests)

    def test_watched_review_overlay_makes_legacy_female_reference_eligible(self):
        self.classify()
        self.document['videos'][0].pop('material_group')
        self.document['videos'][0].update(original_watched=False, source_kind='tiktok_public_browser', checks={'solo': None},
            material_classification=dict(material_group='female', subject_group='solo', original_watched=True,
                                         observed_at='2026-11-15T15:00:00Z', method='watched_original'))
        self.write()
        catalog = self.service.catalog()['references'][0]
        self.assertTrue(catalog['original_watched'])
        self.assertEqual(catalog['material_group'], 'female')
        self.assertEqual(catalog['subject_group'], 'solo')
        self.assertTrue(self.prepare()['can_submit'])

    def test_sort_uses_first_capture_not_recent_review_or_recheck(self):
        self.document['videos'] = [video(last_seen_at='2026-11-16T12:00:00Z'),
                                  video(REF_B, first_seen_at='2026-11-13T12:00:00Z')]
        self.write()
        self.assertEqual([item['id'] for item in self.service.catalog()['references']], [REF_B, REF_A])

    def test_actual_character_plus_video_reference_and_source_link_before_paid_call(self):
        self.classify()
        plan = self.prepare(prompt='Follow the short repeated hand gesture.')
        self.assertTrue(plan['can_submit'])
        self.assertNotIn('private-media-marker', json.dumps(plan))
        self.assertFalse(self.service.links_path.exists())
        def before_submit(body):
            saved = json.loads(self.service.links_path.read_text())
            self.assertEqual(saved['links'][0]['asset_id'], ASSET_A)
            self.assertEqual(saved['links'][0]['reference_id'], REF_A)
            self.assertNotIn('job_id', saved['links'][0])
        self.provider.hook = before_submit
        result = self.submit(plan)
        request = self.provider.requests[0]
        self.assertEqual(request['mode'], 'references')
        self.assertEqual(len(request['references']), 2)
        self.assertEqual(request['references'][0]['type'], 'image_url')
        self.assertEqual(request['references'][1], {'type': 'video_url', 'url': MEDIA})
        self.assertTrue(request['references'][0]['url'].startswith('data:image/png;base64,'))
        self.assertNotIn('frame_images', request)
        self.assertIn('IMAGE reference defines the character identity', request['prompt'])
        self.assertIn('VIDEO reference supplies', request['prompt'])
        self.assertIn('Follow the short repeated hand gesture.', request['prompt'])
        self.assertEqual(result['job']['source_asset_id'], ASSET_A)
        self.assertEqual(result['job']['source_reference_id'], REF_A)
        self.assertEqual(result['job']['source_character'], 'ExistingRole')
        stored = self.service.links_path.read_text()
        for private in (MEDIA, 'private-media-marker', 'base64', 'Follow the short', 'private-character'):
            self.assertNotIn(private, stored)
        self.assertEqual(self.service.links_path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.network)

    def test_no_consent_audio_or_expired_plan_never_submits(self):
        self.classify()
        with patch('replication_service.time.time', return_value=1000):
            plan = self.prepare()
        for changes in (dict(consent=False), dict(generate_audio=True), dict(plan_id='invalid')):
            with self.subTest(changes=changes), self.assertRaises(ReplicationError):
                self.submit(plan, **changes)
        with patch('replication_service.time.time', return_value=1901), self.assertRaises(ReplicationError) as expired:
            self.submit(plan)
        self.assertEqual(expired.exception.status, 409)
        self.assertFalse(self.provider.requests)

    def test_content_fingerprint_stable_across_prepare_ids_and_changes_with_asset_bytes(self):
        self.classify()
        first, second = self.prepare(), self.prepare()
        self.assertNotEqual(first['plan_id'], second['plan_id'])
        self.assertRegex(first['content_fingerprint'], r'^[a-f0-9]{64}$')
        self.assertEqual(first['content_fingerprint'], second['content_fingerprint'])
        (self.store.media / (ASSET_A + '.png')).write_bytes(PNG + b'different-character-file-content')
        changed = self.prepare()
        self.assertTrue(changed['can_submit'])
        self.assertNotEqual(first['content_fingerprint'], changed['content_fingerprint'])
        self.assertNotIn('private-media-marker', json.dumps(first))
        self.assertFalse(self.provider.requests)

    def test_classification_or_reference_changes_invalidate_prepared_plan(self):
        self.classify()
        plan = self.prepare()
        self.classify(material='male')
        with self.assertRaises(ReplicationError):
            self.submit(plan)
        self.classify()
        self.document['videos'][0]['material_classification'] = dict(material_group='unknown', subject_group='unknown',
            original_watched=False, observed_at='2026-11-17T12:00:00Z')
        self.write()
        with self.assertRaises(ReplicationError):
            self.submit(plan)
        self.assertFalse(self.provider.requests)

    def test_asset_changes_size_missing_symlink_and_path_traversal_block_before_pay(self):
        self.classify()
        plan = self.prepare()
        path = self.store.media / (ASSET_A + '.png')
        path.write_bytes(PNG + b'changed')
        with self.assertRaises(ReplicationError):
            self.submit(plan)
        path.write_bytes(PNG + b'x' * MAX_IMAGE_BYTES)
        self.assertFalse(self.prepare()['can_submit'])
        path.unlink()
        self.assertFalse(self.prepare()['can_submit'])
        outside = Path(self.tmp.name) / 'outside.png'; outside.write_bytes(PNG)
        path.symlink_to(outside)
        self.assertFalse(self.prepare()['can_submit'])
        self.store.data['assets'][0]['url'] = '/media/../../outside.png'
        self.assertFalse(self.prepare()['can_submit'])
        self.assertFalse(self.provider.requests)

    def test_idempotency_delegated_and_safe_source_links_survive_restart(self):
        self.classify()
        plan = self.prepare()
        result = self.submit(plan)
        self.submit(plan)
        self.assertEqual(len(self.provider.requests), 1)
        restarted = ReplicationService(self.store, self.provider, candidate_path=self.path)
        annotated = restarted.annotate_jobs({'jobs': [self.provider.results['explicit-one']]})
        self.assertEqual(annotated['jobs'][0]['source_asset_id'], ASSET_A)
        self.assertEqual(annotated['jobs'][0]['source_reference_id'], REF_A)
        annotated['jobs'][0]['source_character'] = 'Changed'
        self.assertEqual(restarted.annotate_jobs({'jobs': [self.provider.results['explicit-one']]})['jobs'][0]['source_character'], 'ExistingRole')
        self.classify(ASSET_B)
        other_plan = self.prepare(asset_id=ASSET_B)
        with self.assertRaises(ReplicationError):
            self.submit(other_plan)
        self.assertEqual(len(self.provider.requests), 1)

    def test_uncertain_provider_preserves_safe_source_mapping_without_retry(self):
        self.classify()
        plan = self.prepare()
        self.provider.failure = VideoError('提交待核对，未自动重试。', 502)
        with self.assertRaises(ReplicationError) as uncertain:
            self.submit(plan)
        self.assertEqual(uncertain.exception.status, 502)
        rows = self.service.annotate_jobs({'jobs': list(self.provider.results.values())})['jobs']
        self.assertEqual(rows[0]['status'], 'submission_uncertain')
        self.assertEqual(rows[0]['source_asset_id'], ASSET_A)
        self.assertNotIn(MEDIA, self.service.links_path.read_text())

    def test_failed_source_record_write_blocks_each_attempt_before_provider(self):
        self.classify()
        plan = self.prepare()
        with patch.object(self.service, '_save_links', side_effect=ReplicationError('Storage unavailable', 500)) as save:
            for _ in range(2):
                with self.assertRaises(ReplicationError):
                    self.submit(plan)
            self.assertEqual(save.call_count, 2)
        self.assertFalse(self.provider.requests)
        self.assertFalse(self.service.links_path.exists())
        self.submit(plan)
        self.assertEqual(len(self.provider.requests), 1)
        self.assertTrue(self.service.links_path.exists())

    def test_sync_fixed_public_url_no_credentials_validates_and_replaces_atomically(self):
        self.document['videos'].append(video(REF_B))
        result = self.service.sync()
        self.assertTrue(result['synced'])
        self.assertEqual(result['reference_count'], 2)
        request = self.network[0][0]
        self.assertEqual(request.full_url, CANDIDATE_URL)
        self.assertIsNone(request.get_header('Authorization'))
        self.assertEqual(len(self.service.catalog()['references']), 2)
        self.assertFalse(list(self.path.parent.glob('.discovery-*.tmp')))
        self.assertFalse(self.provider.requests)

    def test_invalid_or_redirect_sync_preserves_old_file(self):
        original = self.path.read_bytes()
        for status, data in ((302, b''), (200, b'not-json'), (200, b'{"videos": []}' + b'x'),
                             (200, json.dumps({'videos': [video(url='https://evil.example/private')]}).encode()),
                             (200, json.dumps({'videos': [video(), video()]}).encode())):
            self.service._request_fn = lambda request, status=status, data=data, **kw: (status, {}, data)
            with self.subTest(status=status), self.assertRaises(ReplicationError):
                self.service.sync()
            self.assertEqual(self.path.read_bytes(), original)

    def test_source_file_symlink_or_invalid_private_links_cannot_be_read_or_overwritten(self):
        original = self.path.read_bytes()
        outside = Path(self.tmp.name) / 'outside.json'; outside.write_bytes(original)
        self.path.unlink(); self.path.symlink_to(outside)
        with self.assertRaises(ReplicationError):
            self.service.catalog()
        with self.assertRaises(ReplicationError):
            self.service.sync()
        self.assertEqual(outside.read_bytes(), original)
        invalid = Path(self.tmp.name) / 'replication-links.json'; invalid.write_text('{"schema_version":1,"links":[{"api_key":"secret"}]}')
        with self.assertRaises(ReplicationError):
            ReplicationService(self.store, self.provider, candidate_path=self.path)


class PrecedenceTests(unittest.TestCase):
    def reviewed(self):
        return video(material_group='male', last_seen_at='2026-11-15T12:00:00Z',
            material_classification=dict(material_group='female', subject_group='solo', original_watched=True,
                                         observed_at='2026-11-12T12:00:00Z'),
            observations=[dict(material_group='male', checks={'solo': True}, original_watched=True, observed_at='2026-11-10T12:00:00Z'),
                          dict(checks={'solo': True}, original_watched=True, observed_at='2026-11-15T12:00:00Z')])

    def test_v1_recheck_does_not_promote_old_retained_material_over_review(self):
        self.assertEqual(_effective_reference(self.reviewed())[:2], ('female', 'solo'))

    def test_new_watched_axis_overrides_only_that_review_axis(self):
        row = self.reviewed()
        row['observations'].append(dict(material_group='male', original_watched=True, observed_at='2026-11-16T12:00:00Z'))
        self.assertEqual(_effective_reference(row)[:2], ('male', 'solo'))

    def test_unknown_or_unwatched_new_review_clears_old_known_labels(self):
        for watched in (True, False):
            row = self.reviewed()
            row['material_classification'].update(material_group='unknown', subject_group='unknown',
                original_watched=watched, observed_at='2026-11-17T12:00:00Z')
            self.assertEqual(_effective_reference(row)[:2], ('unknown', 'unknown'))

    def test_unwatched_known_values_cannot_make_reference_ready(self):
        row = video(original_watched=False)
        self.assertEqual(_effective_reference(row)[:2], ('unknown', 'unknown'))
        row['material_classification'] = dict(material_group='female', subject_group='solo', original_watched=False,
                                             observed_at='2026-11-15T12:00:00Z')
        self.assertEqual(_effective_reference(row)[:2], ('unknown', 'unknown'))

    def test_fixed_transport_disallows_alternate_urls_and_redirect_follow(self):
        with self.assertRaises(ReplicationError):
            _transport(Request('https://evil.example/data.json'), timeout=1, max_bytes=100)
        with patch('replication_service.build_opener') as opener:
            opener.return_value.open.side_effect = HTTPError(CANDIDATE_URL, 302, 'move', {}, None)
            status, _, raw = _transport(Request(CANDIDATE_URL), timeout=1, max_bytes=100)
        self.assertEqual((status, raw), (302, b''))


if __name__ == '__main__':
    unittest.main()
