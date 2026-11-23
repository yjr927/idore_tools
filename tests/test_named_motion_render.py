"""Named dance production uses exact sources, original motion when available, and one paid job."""
from datetime import datetime
import hashlib
from io import BytesIO
from email.message import Message
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from scripts import observed_dance, run_pipeline as runner
from scripts.named_motion import NAMED, NAMED_METHOD, named_source
from scripts.pipeline_state import PipelineError
from scripts.prepare_motion_source import PreflightStopped
from video_service import VideoService

SLOTS = list(NAMED)
NOTES = {
    '2026-11-23-momoko': '开始屈臂将拳放脸侧，随后双掌在侧边呈交叉角度并抬起一膝，最后一臂横放胸前。',
    '2026-11-23-bay': '侧转时双手平放在脸侧，随后双臂在胸前交叉转动；最后放下手臂。不限定男女角色用途。',
}


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data').mkdir()
        (self.root / 'characters').mkdir()
        (self.root / 'characters/r.png').write_bytes(b'fixture image')
        patch.object(runner, 'SITE', self.root).start()
        patch.object(runner, 'STATE', self.root / 'data/tasks.json').start()
        patch.dict('os.environ', {'OPENROUTER_API_KEY': 'local-test-only'}, clear=True).start()
        self.clock = patch.object(runner, 'datetime').start()
        self.clock.now.return_value = datetime(2026, 11, 23, 9, 1, tzinfo=ZoneInfo('America/New_York'))
        self.original = patch.object(observed_dance, 'official_original',
                                     side_effect=PreflightStopped('public_page_access_required')).start()
        self.clay = patch.object(runner, 'prepare_clay').start()
        self.video = patch('video_service.VideoService').start().return_value
        self.done = {'id': 'video-' + 'a'*32, 'remote_id': 'vid-existing-job', 'status': 'completed'}
        self.video.submit.return_value = self.done
        self.video.recover.return_value = self.done

    def fixture(self, slot):
        entry = NAMED[slot]
        raw = dict(id=entry['source_id'], url=entry['url'], title=entry['title'], kind='hands',
                   source_kind='chatgpt_work', original_watched=True, material_group='female' if slot == SLOTS[0] else 'unknown',
                   checks={'solo': True, 'fixed_camera': True}, transformation='unknown',
                   motion_summary=NOTES[slot], last_seen_at='2026-11-19T12:00:00Z')
        source = named_source(slot, raw)
        character = dict(id='role-1', name='Test role', material_group=source['material_group'], enabled=True,
                         auto_match_eligible=True, cast_type='solo', asset_kind='single_character',
                         sha256=hashlib.sha256(b'fixture image').hexdigest(),
                         image_url='https://raw.githubusercontent.com/yjr927/idore_tools/'+'a'*40+'/site/characters/r.png')
        row = dict(id='agent-'+slot, sequence_slot=slot, generation_method=NAMED_METHOD,
                   agent_plan={'photo_ids':[character['id']], 'source_url':source['url'], 'source_id':source['id']})
        return row, source, character

    def run_row(self, row, source, char):
        return runner.render_named_motion({'items': [row]}, row, source, char, self.root)

    def test_both_dances_use_distinct_observed_beats_and_only_approved_first_frame(self):
        for slot,gesture in zip(SLOTS, ('lifting one knee', 'both forearms')):
            row, src, char = self.fixture(slot)
            def submitted(payload):
                saved = json.loads(runner.STATE.read_text())['items'][0]
                self.assertTrue(saved['submission_reserved'])
                self.assertEqual(saved['motion_reference_method'], 'observed_description')
                self.assertEqual(saved['motion_reference_reason'], 'public_page_access_required')
                request = VideoService(self.root / 'check')._request(payload)[2]
                self.assertEqual(request['frame_images'], [{'type':'image_url','image_url':{'url':char['image_url']},'frame_type':'first_frame'}])
                self.assertNotIn('input_references', request)
                self.assertEqual(request['duration'], 6)
                self.assertIn(gesture, request['prompt'])
                self.assertNotIn(src['url'], str(request))
                return self.done
            self.video.submit.side_effect = submitted
            output = self.run_row(row, src, char)
            self.assertEqual(output.name, self.done['id']+'.mp4')
            self.assertEqual(row['remote_id'], self.done['remote_id'])
            self.assertEqual(row['source_transformation'], 'unknown')
        self.assertEqual(self.original.call_count, 2)
        self.clay.assert_not_called()

    def test_original_traced_when_official_video_available(self):
        row, src, char = self.fixture(SLOTS[0])
        self.original.side_effect = None
        def clay(doc, row, scratch, *, original):
            self.assertEqual(original, self.root / 'observed-original.mp4')
            row['clay_reference_url'] = 'https://example.org/named-clay.mp4'
            return row['clay_reference_url']
        self.clay.side_effect = clay
        self.run_row(row, src, char)
        payload = self.video.submit.call_args.args[0]
        self.assertEqual(payload['mode'], 'references')
        self.assertEqual(payload['references'][1]['url'], row['clay_reference_url'])
        self.assertEqual(row['motion_reference_method'], 'source_motion')
        self.assertEqual(self.video.submit.call_count, 1)

    def test_ambiguous_submission_no_fallback_or_retry_then_remote_resume(self):
        row, src, char = self.fixture(SLOTS[0])
        self.video.submit.side_effect = TimeoutError()
        with self.assertRaises(PipelineError): self.run_row(row,src,char)
        self.assertTrue(row['submission_reserved'])
        with self.assertRaises(PipelineError) as caught: self.run_row(row,src,char)
        self.assertEqual(caught.exception.code, 'submission_uncertain')
        row['remote_id'] = self.done['remote_id']
        self.run_row(row,src,char)
        self.video.recover.assert_called_once_with(row['remote_id'],row['model'])
        self.assertEqual(self.video.submit.call_count,1)
        self.assertEqual(self.original.call_count,1)

    def test_wrong_identity_date_or_unapproved_photo_makes_no_requests(self):
        row, src, char = self.fixture(SLOTS[0])
        with self.assertRaises(PipelineError): self.run_row(row,dict(src, url=NAMED[SLOTS[1]]['url']),char)
        with self.assertRaises(PipelineError): self.run_row(row,src,dict(char,sha256='0'*64))
        with self.assertRaises(PipelineError): self.run_row(row,src,dict(char,enabled=False))
        self.clock.now.return_value = datetime(2026,11,22,23,0,tzinfo=ZoneInfo('America/New_York'))
        with self.assertRaises(PipelineError) as caught: self.run_row(row,src,char)
        self.assertEqual(caught.exception.code,'outside_named_window')
        self.original.assert_not_called()
        self.video.submit.assert_not_called()

    def test_source_identity_failure_never_submits_description(self):
        row, src, char = self.fixture(SLOTS[0])
        self.original.side_effect=PipelineError('source_mismatch','Wrong source')
        with self.assertRaises(PipelineError): self.run_row(row,src,char)
        self.video.submit.assert_not_called()


class OfficialPageTests(unittest.TestCase):
    def response(self, detail):
        entry=NAMED[SLOTS[0]]
        raw=json.dumps({'__DEFAULT_SCOPE__': {'webapp.video-detail': detail}})
        body=BytesIO(('<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__">'+raw+'</script>').encode())
        body.headers=Message()
        body.headers['Content-Type']='text/html'
        body.geturl=lambda:entry['url']
        return body

    def test_one_ordinary_permalink_then_bound_official_media(self):
        entry=NAMED[SLOTS[0]]
        source={'id':entry['source_id'],'url':entry['url']}
        address='https://v16-web.us.tiktok.com/video/tos/useast/clip/?mime_type=video_mp4'
        clip={'id':entry['source_id'][7:],'author':{'uniqueId':'itsmomoko.jp'},'video':{'playAddr':address}}
        page=self.response({'itemInfo':{'itemStruct':clip}})
        with patch.object(observed_dance,'build_opener') as opener, patch.object(observed_dance,'download_reference') as download:
            opener.return_value.open.return_value = page
            observed_dance.official_original(source,Path('/scratch/original.mp4'))
            self.assertEqual(opener.return_value.open.call_args.args[0].full_url,entry['url'])
            download.assert_called_once_with(address,Path('/scratch/original.mp4'))
            clip['id']='wrong-id'
            opener.return_value.open.return_value=self.response({'itemInfo':{'itemStruct':clip}})
            with self.assertRaises(PipelineError):
                observed_dance.official_original(source,Path('/scratch/original.mp4'))
            self.assertEqual(download.call_count, 1)

    def test_login_or_challenge_page_does_not_fetch_another_url(self):
        entry=NAMED[SLOTS[1]]
        with patch.object(observed_dance,'build_opener') as opener, patch.object(observed_dance,'download_reference') as download:
            body=BytesIO(b'Verify to continue')
            body.headers=Message();body.headers['Content-Type']='text/html';body.geturl=lambda:entry['url']
            opener.return_value.open.return_value=body
            with self.assertRaises(PreflightStopped) as caught:
                observed_dance.official_original({'url':entry['url'],'id':entry['source_id']},Path('/scratch/original.mp4'))
            self.assertEqual(caught.exception.reason, 'public_page_access_required')
            self.assertEqual(opener.return_value.open.call_count,1)
            download.assert_not_called()
