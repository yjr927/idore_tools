"""Orchestration boundaries: durable claims, exact review, and no paid retries."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import run_pipeline as runner
from scripts.pipeline_state import PipelineError, approve, approved_content, touch


class PipelineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.site = Path(self.tmp.name) / 'site'
        self.state = self.site / 'data/pipeline.json'
        self.state.parent.mkdir(parents=True)
        self.addCleanup(patch.stopall)
        patch.object(runner, 'SITE', self.site).start()
        patch.object(runner, 'STATE', self.state).start()
        patch.object(runner, 'ROOT', Path(self.tmp.name)).start()
        patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': runner.REPOSITORY,
            'GITHUB_RUN_ID': '12345678', 'GITHUB_ACTOR': 'reviewer',
            'OPENROUTER_API_KEY': 'not-a-real-cloud-key'}, clear=True).start()
        for name in ('creations', 'characters'):
            (self.site / ('data/' + name + '.json')).write_text('{"items":[]}')
        (self.site / 'data/discovery.json').write_text('{"runs":[],"videos":[]}')
        self.character = {'id': 'kai', 'name': 'KAI', 'material_group': 'male',
                          'cast_type': 'solo', 'enabled': True,
                          'image_url': 'https://example.com/kai.png'}

    def item(self):
        item = {'id': 'task-1234567890123456789', 'source_id': 'tiktok-1234567890123456789',
            'source_url': 'https://www.tiktok.com/@test/video/1234567890123456789',
            'title': 'Gesture dance', 'character': 'KAI', 'character_id': 'kai',
            'material_group': 'male', 'cast_type': 'solo', 'original_watched': True,
            'kind': 'hands', 'transformation': 'none', 'status': 'queued',
            'clay_reference_url': 'https://example.com/clay.mp4'}
        touch(item)
        return item

    def test_read_only_connection_check_does_not_erase_known_quota_failure(self):
        document = {'connections': {'openai': {'configured': True, 'status': 'insufficient_quota'}}}
        report = {'providers': {'openai': {'status': 'passed'}, 'openrouter': {'status': 'passed'}}}
        with patch('scripts.check_api_connections.check_connections', return_value=report):
            runner.connections(document)
        self.assertEqual(document['connections']['openai']['status'], 'insufficient_quota')
        self.assertEqual(document['connections']['openrouter']['status'], 'connected')

    def test_router_copy_success_and_errors_do_not_clear_direct_openai_history(self):
        item = self.approved()
        document = self.document(item)
        document['connections']['openai'] = {'configured': True, 'status': 'insufficient_quota'}
        with patch('scripts.pipeline_copy.generate_copy', return_value={'caption': 'KAI in motion.', 'hashtags': [{'tag': '#KAI'}]}):
            runner.copy_for(document, item)
        self.assertEqual(document['connections']['copy']['provider'], 'openrouter')
        self.assertEqual(document['connections']['copy']['status'], 'connected')
        self.assertEqual(document['connections']['openai']['status'], 'insufficient_quota')
        item['stage'] = 'copy'
        runner.record_failure(document, item, PipelineError('insufficient_quota', 'OpenRouter quota test'))
        self.assertEqual(document['connections']['copy']['status'], 'insufficient_quota')
        self.assertEqual(document['connections']['openai']['status'], 'insufficient_quota')

    @staticmethod
    def document(item):
        return {'schema_version': 1, 'items': [item], 'connections': {}, 'requests': []}

    def approved(self):
        item = self.item()
        item.update(status='pending_review', video_path='generated/video-' + 'a' * 24 + '.mp4',
                    sha256='a' * 64, caption='Watch KAI catch the rhythm.',
                    hashtags=[{'tag': '#KAI', 'category': 'brand', 'reason': 'Character',
                               'source_url': None, 'observed_at': None}])
        touch(item)
        approve(item, item['revision'], item['caption'], '#KAI #HandDance #Gesture', 'reviewer')
        self.assertTrue(approved_content(item))
        return item

    def test_video_claim_is_durable_before_submit_and_ambiguous_retry_is_blocked(self):
        item = self.item(); document = self.document(item)
        def submit(body):
            persisted = json.loads(self.state.read_text())['items'][0]
            self.assertTrue(persisted['submission_reserved'])
            self.assertEqual(persisted['status'], 'generating_video')
            self.assertEqual(body['mode'], 'references')
            self.assertEqual(len(body['references']), 2)
            raise TimeoutError('Do not serialize upstream data.')
        with patch('video_service.VideoService') as service, patch('scripts.run_clay_video_generation.ObservedTransport') as transport:
            transport.return_value.diagnosis = {}
            service.return_value.submit.side_effect = submit
            with self.assertRaises(PipelineError) as caught:
                runner.generate_video(document, item, self.character, Path(self.tmp.name))
            self.assertEqual(caught.exception.code, 'submission_uncertain')
            with self.assertRaises(PipelineError) as caught:
                runner.generate_video(document, item, self.character, Path(self.tmp.name))
            self.assertEqual(caught.exception.code, 'submission_uncertain')
            self.assertEqual(service.return_value.submit.call_count, 1)
            self.assertEqual(service.call_count, 1)

    def test_material_classification_gates_precede_video_service(self):
        cases = [({'material_group': 'female'}, {}), ({'cast_type': 'group'}, {}),
                 ({'original_watched': False}, {}), ({'transformation': 'unknown'}, {}),
                 ({'kind': 'pov'}, {}), ({}, {'enabled': False}),
                 ({'material_group': 'unknown'}, {'material_group': 'unknown'})]
        for item_change, character_change in cases:
            item = dict(self.item(), **item_change)
            character = dict(self.character, **character_change)
            with self.subTest(item=item_change, character=character_change):
                with patch('video_service.VideoService') as service:
                    service.return_value.submit.side_effect = TimeoutError('Paid boundary must not be reached.')
                    with self.assertRaises(PipelineError) as caught:
                        runner.generate_video(self.document(item), item, character, Path(self.tmp.name))
                    self.assertEqual(caught.exception.code, 'material_required')
                    service.assert_not_called()

    def test_checkpoint_failure_stops_both_paid_boundaries(self):
        item = self.item(); document = self.document(item)
        failure = PipelineError('checkpoint_failed', 'No durable checkpoint.')
        with patch.object(runner, 'checkpoint', side_effect=failure), patch('video_service.VideoService') as service:
            with self.assertRaises(PipelineError):
                runner.generate_video(document, item, self.character, Path(self.tmp.name))
            service.return_value.submit.assert_not_called()
        item = self.approved(); document = self.document(item)
        with patch.object(runner, 'checkpoint', side_effect=failure), patch('scripts.pipeline_copy.generate_copy') as copy_call:
            with self.assertRaises(PipelineError): runner.copy_for(document, item, allow_repeat=True)
            copy_call.assert_not_called()

    def test_copy_adapter_checkpoints_claim_and_revokes_approval_before_paid_call(self):
        item = self.approved(); document = self.document(item)
        previous_sha = item['sha256']
        result = {'caption': 'New caption.', 'captions': ['New caption.', 'Second.', 'Third.'],
                  'hashtags': [{'tag': '#KAI', 'reason': 'Character', 'category': 'brand',
                                'source_url': None, 'observed_at': None}],
                  'sources': [], 'trend_note': 'No verified recent trend.'}
        def generate(completed, **kwargs):
            persisted = json.loads(self.state.read_text())['items'][0]
            self.assertTrue(persisted['copy_reserved'])
            self.assertEqual(persisted['status'], 'generating_copy')
            self.assertEqual(persisted['approval']['status'], 'pending')
            self.assertFalse(approved_content(persisted))
            self.assertEqual(completed['id'], Path(item['video_path']).stem)
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['sha256'], previous_sha)
            self.assertEqual(kwargs['site_dir'], self.site)
            return result
        with patch('scripts.pipeline_copy.generate_copy', side_effect=generate):
            runner.copy_for(document, item, allow_repeat=True)
        self.assertEqual(item['status'], 'pending_review')
        self.assertEqual(item['approval']['status'], 'pending')
        self.assertEqual(item['caption'], 'New caption.')
        self.assertFalse(approved_content(item))

    def test_copy_failure_preserves_claim_and_invalidates_old_approval(self):
        item = self.approved(); document = self.document(item)
        with patch('scripts.pipeline_copy.generate_copy', side_effect=TimeoutError('not public')) as generate:
            with self.assertRaises(TimeoutError): runner.copy_for(document, item, allow_repeat=True)
            self.assertTrue(item['copy_reserved'])
            self.assertFalse(approved_content(item))
            with self.assertRaises(PipelineError) as caught: runner.copy_for(document, item)
        self.assertEqual(caught.exception.code, 'copy_submission_uncertain')
        self.assertEqual(generate.call_count, 1)

    def test_unconfigured_tiktok_retains_approval_without_network(self):
        item = self.approved(); document = self.document(item)
        with patch('tiktok_draft_service.TikTokDraftService') as service:
            runner.send_draft(document, item)
            service.assert_not_called()
        self.assertEqual(item['status'], 'approved')
        self.assertEqual(item['tiktok']['status'], 'not_configured')
        self.assertTrue(approved_content(item))
        self.assertNotIn('upload_reserved', item)

    def test_changed_caption_cannot_upload_under_old_approval(self):
        item = self.approved(); item['caption'] = 'Changed after approval.'
        before = copy.deepcopy(item)
        with patch('tiktok_draft_service.TikTokDraftService') as service:
            with self.assertRaises(PipelineError) as caught:
                runner.send_draft(self.document(item), item)
            service.assert_not_called()
        self.assertEqual(caught.exception.code, 'approval_required')
        self.assertEqual(item, before)

    def run_action(self, item, action, **settings):
        self.state.write_text(json.dumps(self.document(item)))
        body = {'action': action, 'item_id': item['id'], 'request_id': 'review-request-123',
                'revision': item['revision']}
        body.update(settings)
        with patch.dict(os.environ, {'PIPELINE_INPUTS': json.dumps(body)}):
            code = runner.main()
        return code, json.loads(self.state.read_text())

    def test_stale_approval_invalid_caption_tags_and_request_do_not_mutate_item(self):
        item = self.approved()
        item.update(status='pending_review', approval={'status': 'pending'})
        touch(item)
        cases = [({'revision': 'stale'}, 'approve'),
                 ({'caption': '', 'hashtags': '#KAI'}, 'approve'),
                 ({'caption': 'A caption.', 'hashtags': 'bad-tag'}, 'approve'),
                 ({'request_id': '!'}, 'approve'), ({}, 'invalid-action')]
        with patch('scripts.pipeline_copy.generate_copy') as paid_copy, patch('tiktok_draft_service.TikTokDraftService') as upload:
            for settings, action in cases:
                with self.subTest(action=action, settings=settings):
                    code, persisted = self.run_action(copy.deepcopy(item), action, **settings)
                    self.assertEqual(code, 1)
                    self.assertEqual(persisted['items'][0], item)
            paid_copy.assert_not_called(); upload.assert_not_called()

    def test_retry_cannot_rewrite_already_uploaded_item(self):
        item = self.approved()
        item.update(status='draft_sent', upload_reserved=True)
        touch(item)
        before = copy.deepcopy(item)
        with patch('scripts.pipeline_copy.generate_copy') as copy_call:
            code, persisted = self.run_action(item, 'retry')
            self.assertEqual(code, 1)
            copy_call.assert_not_called()
        self.assertEqual(persisted['items'][0], before)

    def test_duplicate_request_cannot_start_another_paid_operation(self):
        item = self.item(); document = self.document(item)
        document['requests'] = ['duplicate-request-123']
        self.state.write_text(json.dumps(document))
        settings = {'action': 'sync', 'request_id': 'duplicate-request-123'}
        with patch.dict(os.environ, {'PIPELINE_INPUTS': json.dumps(settings)}):
            with patch.object(runner, 'generate_video') as video, patch('scripts.pipeline_copy.generate_copy') as copy_call:
                self.assertEqual(runner.main(), 0)
                video.assert_not_called(); copy_call.assert_not_called()
        self.assertEqual(json.loads(self.state.read_text())['items'][0], item)

    def test_legacy_scheduler_only_recovers_existing_nonagent_job(self):
        rows = [dict(self.item(), id='queued'),
                dict(self.item(), id='unconfirmed', submission_reserved=True, status='generating_video'),
                dict(self.item(), id='agent', agent_plan={'trend_id': 'test'}, remote_id='job-A', status='generating_video'),
                dict(self.item(), id='existing', remote_id='job-B', status='generating_video')]
        document = self.document(rows[0])
        document['items'] = rows
        self.state.write_text(json.dumps(document))
        (self.site / 'data/characters.json').write_text(json.dumps({'items': [self.character]}))
        with patch.object(runner, 'generate_video') as recover, patch.object(runner, 'copy_for') as paid_copy:
            self.assertEqual(runner.main(), 0)
        self.assertEqual([c.args[1]['id'] for c in recover.call_args_list], ['existing'])
        paid_copy.assert_not_called()

    def test_legacy_retry_cannot_generate_unplanned_video(self):
        with patch.object(runner, 'generate_video') as paid_render:
            code, _ = self.run_action(self.item(), 'retry')
        self.assertEqual(code, 1)
        paid_render.assert_not_called()

    def test_planned_motion_rechecks_real_evidence_and_returns_unarchived_clip(self):
        source_id = '1234567890123456789'
        source = {'id': 'tiktok-' + source_id, 'url': 'https://www.tiktok.com/@one/video/' + source_id,
                  'material_group': 'male', 'original_watched': True, 'source_kind': 'chatgpt_work',
                  'checks': {'solo': True}, 'kind': 'hands', 'transformation': 'none', 'motion_summary': 'arm swing',
                  'metrics': {'views': 1500000, 'likes': 120000}, 'metric_display': {'views': '1.5M', 'likes': '120K'},
                  'related_videos': [{'url': f'https://www.tiktok.com/@peer{p}/video/123456789012345670{p}',
                                      'original_watched': True, 'same_pattern': True, 'note': 'Repeated arm swing'}
                                     for p in (1, 2)]}
        item = {'id': 'agent-slot', 'agent_plan': {'trend_id': source['id'], 'photo_ids': ['kai']}}
        character = dict(self.character, sha256='a' * 64, auto_match_eligible=True)
        scratch = Path(self.tmp.name)
        with patch.object(runner, 'render_motion_file', return_value=scratch / 'unedited.mp4') as motion:
            for changes, image in [({'metrics': {'views': 999, 'likes': 120000}}, character),
                                   ({}, dict(character, material_group='female')),
                                   ({}, dict(character, id='alternate'))]:
                with self.subTest(changes=changes, image=image['id']), self.assertRaises(PipelineError):
                    runner.render_agent_motion(self.document(item), item, dict(source, **changes), image, scratch)
            motion.assert_not_called()
            self.assertEqual(runner.render_agent_motion(self.document(item), item, source, character, scratch),
                             scratch / 'unedited.mp4')
            motion.assert_called_once()
            with self.assertRaisesRegex(PipelineError, '已绑定'):
                runner.render_agent_motion(self.document(item), item, source, dict(character, sha256='b' * 64), scratch)
        self.assertEqual(json.loads((self.site / 'data/creations.json').read_text())['items'], [])

    @staticmethod
    def account_env(account='openid-private-account-A', label='@kai_owner'):
        return {'TIKTOK_ACCESS_TOKEN': 'not-a-real-tiktok-access-token',
                'TIKTOK_ACCOUNT_ID': account, 'TIKTOK_ACCOUNT_LABEL': label}

    @staticmethod
    def fingerprint(account):
        return hashlib.sha256(('tiktok-account-v1:' + account).encode()).hexdigest()

    def test_tiktok_connection_requires_account_and_label_and_hides_openid(self):
        report = {'providers': {'openai': {'status': 'passed'}, 'openrouter': {'status': 'passed'}}}
        document = self.document(self.item())
        env = self.account_env()
        with patch('scripts.check_api_connections.check_connections', return_value=report):
            for missing in ('TIKTOK_ACCESS_TOKEN', 'TIKTOK_ACCOUNT_ID', 'TIKTOK_ACCOUNT_LABEL'):
                with self.subTest(missing=missing), patch.dict(os.environ, dict(env, **{missing: ''})):
                    runner.connections(document)
                    self.assertFalse(document['connections']['tiktok']['configured'])
                    self.assertEqual(document['connections']['tiktok']['account_fingerprint'], '')
            with patch.dict(os.environ, env):
                runner.connections(document)
        account = document['connections']['tiktok']
        self.assertTrue(account['configured'])
        self.assertEqual(account['account_label'], '@kai_owner')
        self.assertEqual(account['account_fingerprint'], self.fingerprint(env['TIKTOK_ACCOUNT_ID']))
        public = self.state.read_text()
        self.assertNotIn(env['TIKTOK_ACCOUNT_ID'], public)
        self.assertNotIn(env['TIKTOK_ACCESS_TOKEN'], public)

    def test_approval_without_connected_account_cannot_upload_after_activation(self):
        item = self.approved()
        item.update(status='pending_review', approval={'status': 'pending'})
        touch(item)
        code, document = self.run_action(item, 'approve', caption=item['caption'],
            hashtags='#KAI #Gesture', target_account='')
        self.assertEqual(code, 0)
        current = document['items'][0]
        self.assertEqual(current['status'], 'approved')
        self.assertIsNone(current['approval']['target_account'])
        with patch.dict(os.environ, self.account_env()), patch('tiktok_draft_service.TikTokDraftService') as service:
            runner.send_draft(document, current)
            service.assert_not_called()
        self.assertEqual(current['status'], 'pending_review')
        self.assertEqual(current['approval'], {'status': 'pending'})
        self.assertFalse(approved_content(current))
        self.assertNotIn('upload_reserved', current)

    def test_account_change_revokes_approval_without_upload(self):
        item = self.approved()
        item['approval']['target_account'] = self.fingerprint('openid-private-account-A')
        with patch.dict(os.environ, self.account_env('openid-private-account-B', '@new_owner')):
            with patch('tiktok_draft_service.TikTokDraftService') as service:
                runner.send_draft(self.document(item), item)
                service.assert_not_called()
        self.assertEqual(item['status'], 'pending_review')
        self.assertEqual(item['approval'], {'status': 'pending'})
        self.assertNotIn('upload_reserved', item)

    def test_old_page_account_fingerprint_cannot_mutate_approval(self):
        item = self.approved()
        item.update(status='pending_review', approval={'status': 'pending'})
        touch(item)
        before = copy.deepcopy(item)
        with patch.dict(os.environ, self.account_env('openid-private-account-B', '@new_owner')):
            with patch('tiktok_draft_service.TikTokDraftService') as service:
                code, document = self.run_action(item, 'approve', caption='Approved caption.',
                    hashtags='#KAI #Gesture', target_account=self.fingerprint('openid-private-account-A'))
                service.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(document['items'][0], before)

    def test_approval_binds_the_exact_reviewed_runtime_account(self):
        item = self.approved()
        item.update(status='pending_review', approval={'status': 'pending'})
        touch(item)
        env = self.account_env()
        target = self.fingerprint(env['TIKTOK_ACCOUNT_ID'])
        with patch.dict(os.environ, env), patch.object(runner, 'send_draft') as upload:
            code, document = self.run_action(item, 'approve', caption='Approved caption.',
                hashtags='#KAI #Gesture', target_account=target)
            upload.assert_called_once()
        current = document['items'][0]
        self.assertEqual(code, 0)
        self.assertEqual(current['approval']['target_account'], target)
        self.assertEqual(current['approval']['account_label'], '@kai_owner')
        self.assertTrue(approved_content(current))

    def test_private_receipt_and_public_claim_survive_upload_exception(self):
        item = self.approved(); document = self.document(item)
        env = self.account_env()
        item['approval']['target_account'] = self.fingerprint(env['TIKTOK_ACCOUNT_ID'])
        receipt = Path(self.tmp.name) / 'artifacts/tiktok-private/12345678/tiktok-draft-receipts.json'
        def fail_upload(*args, **kwargs):
            self.assertTrue(receipt.is_file())
            persisted = json.loads(self.state.read_text())['items'][0]
            self.assertTrue(persisted['upload_reserved'])
            self.assertEqual(persisted['status'], 'uploading')
            receipt.write_text(json.dumps({'schema_version': 1, 'uploads': {
                item['id']: {'publish_id': 'private-publish-id', 'phase': 'init_accepted'}}}))
            raise TimeoutError('Uncertain outcome; do not send again.')
        with patch.dict(os.environ, env), patch('tiktok_draft_service.TikTokDraftService') as service:
            service.return_value.send_draft.side_effect = fail_upload
            with self.assertRaises(TimeoutError): runner.send_draft(document, item)
            service.assert_called_once_with(receipt.parent)
            self.assertTrue(receipt.is_file())
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertIn('private-publish-id', receipt.read_text())
            self.assertNotIn('private-publish-id', self.state.read_text())
            with self.assertRaises(PipelineError) as caught: runner.send_draft(document, item)
            self.assertEqual(caught.exception.code, 'upload_uncertain')
            self.assertEqual(service.return_value.send_draft.call_count, 1)


if __name__ == '__main__': unittest.main()
