import copy
from datetime import datetime
import hashlib
from importlib.util import find_spec
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from scripts import run_agent_motion as editor
from scripts.agent_film_planner import FilmPlanError


class AgentMotionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.site = Path(self.temp.name)
        (self.site / 'characters').mkdir()
        (self.site / 'data').mkdir()
        raw = b'fixture for verified file identity'
        (self.site / 'characters/a.png').write_bytes(raw)
        self.photo = dict(id='role-a', name='Rin', ip='Example', cast_type='solo', material_group='male',
             asset_kind='portrait', enabled=True, auto_match_eligible=True, auto_match_blockers=[],
             sha256=hashlib.sha256(raw).hexdigest(), image_url='https://raw.githubusercontent.com/'
             + editor.REPOSITORY + '/' + 'a'*40 + '/site/characters/a.png')
        self.source = dict(id='tiktok-7700000000000000000', url='https://www.tiktok.com/@s/video/7700000000000000000',
             source_kind='chatgpt_work', original_watched=True, title='Sleeve tug', material_group='male',
             kind='hands', transformation='none', checks={'solo':True}, metrics={'views':1000000, 'likes':100000},
             metric_display={'views':'1,000,000','likes':'100,000'}, motion_summary='A turn, then a sleeve tug.',
             last_seen_at='2026-11-22T14:00:00Z', related_videos=[dict(
                 url=f'https://www.tiktok.com/@p{i}/video/{7700000000000000000+i}', same_pattern=True,
                 original_watched=True,note='Same turn and sleeve tug.') for i in (1, 2)])
        self.plan = dict(provider='openrouter', model='openai/test-model', execution_host='cloudflare', source_id=self.source['id'], source_url=self.source['url'],
             material_group='male', cast_type='solo', photo_ids=['role-a'], caption='the sleeve had one job',
             hashtags=['#SleeveTug','#CharacterEdit'], hook='did you catch the turn',
             shots=[dict(photo_id='role-a',framing=f,overlay=t) for f,t in (
                  ('wide','did you catch the turn'),('medium','watch the sleeve'),('close','there it is'))])
        self.doc = {'items':[]}
        self.events = []
        self.planner = Mock(return_value=self.plan)
        self.renderer = Mock(return_value=None)

    def run_slot(self, slot='2026-11-22-evening'):
        return editor.produce(self.doc, [self.photo], {'videos':[self.source]}, self.site, '7', slot,
             lambda d: self.events.append(d['items'][0]['status']), self.planner, self.renderer)

    def test_schedule_uses_new_york_day_and_hour(self):
        for day, offset in [('2026-07-03','-04:00'), ('2026-11-22','-05:00')]:
            for clock, tag in [('08:59', None), ('09:00','morning'), ('13:59','morning'),
                               ('14:00','afternoon'), ('17:59','afternoon'), ('18:00','evening')]:
                self.assertEqual(editor.current_slot(datetime.fromisoformat(day + 'T' + clock + offset)),
                                 day + '-' + tag if tag else None)

    def completed_film(self, slot, stamp, key, method=editor.METHOD):
        digest = hashlib.sha256(key.encode()).hexdigest()
        return dict(kind='character', generation_method=method, sequence_slot=slot, generated_at=stamp,
                    sha256=digest, video_path=f'generated/video-{digest[:24]}.mp4')

    def test_evening_catches_up_three_slots_and_repeat_produces_nothing(self):
        at = datetime.fromisoformat('2026-11-23T02:01:00+00:00')
        completed = []

        def archive(doc, catalog, discovery, site, run, slot, save):
            completed.append(self.completed_film(slot, at.isoformat(), slot))
            (site/'data/creations.json').write_text(json.dumps({'items':completed}))
            return True

        args = (self.doc, [], {}, self.site, '123', None, lambda: at, archive)
        self.assertEqual(editor.produce_due(*args), 3)
        self.assertEqual([f['sequence_slot'] for f in completed],
                         ['2026-11-22-morning','2026-11-22-afternoon','2026-11-22-evening'])
        self.assertEqual(editor.produce_due(*args), 0)

    def test_resumed_older_film_and_authentic_original_count_by_new_york_completion_day(self):
        at = datetime.fromisoformat('2026-11-23T02:01:00+00:00')
        stamp = at.isoformat()
        yesterday = self.completed_film('2026-11-21-evening',stamp,'old reservation')
        original = self.completed_film('2026-11-22-evening',stamp,'original','original_character_edit')
        test_clip = self.completed_film('irrelevant',stamp,'test','api_test')
        clips = [yesterday,yesterday,original,test_clip]
        self.assertEqual(editor.next_slot(at, clips, []), '2026-11-22-morning')
        clips.append(self.completed_film('2026-11-22-morning',stamp,'last one'))
        self.assertIsNone(editor.next_slot(at, clips, []))
        next_day = datetime.fromisoformat('2026-11-23T14:01:00+00:00')
        self.assertEqual(editor.next_slot(next_day, clips, []), '2026-11-23-morning')

    def test_one_waiting_run_stops_without_reserving_three_paid_plans(self):
        producer = Mock(return_value=False)
        at = datetime.fromisoformat('2026-11-22T23:10:00-05:00')
        self.assertEqual(editor.produce_due(self.doc, [], {}, self.site, '7', None, lambda: at, producer), 0)
        self.assertEqual(producer.call_count, 1)

    def test_old_terminal_attention_does_not_starve_other_due_slots(self):
        at = datetime.fromisoformat('2026-11-22T21:00:00-05:00')
        rows = [dict(generation_method=editor.METHOD, sequence_slot='2026-11-22-morning', status='needs_attention')]
        self.assertEqual(editor.next_slot(at, [], rows), '2026-11-22-afternoon')

    def test_only_vetted_sources_with_approved_same_group_role_can_reach_openai(self):
        for changes, status in [({'metric_display':{}}, 'waiting_trend'), ({'checks':{'solo':False}}, 'waiting_material'),
            ({'material_group':'female'},'waiting_material'), ({'kind':'pov'},'waiting_material'),
            ({'transformation':'unknown'},'waiting_material')]:
            original = copy.deepcopy(self.source)
            self.source.update(changes)
            self.assertFalse(self.run_slot())
            self.assertEqual(self.doc['items'][0]['status'], status)
            self.source = original
        self.photo['enabled']=False
        self.assertFalse(self.run_slot())
        self.planner.assert_not_called()
        self.renderer.assert_not_called()

    def test_openai_failure_recorded_without_resubmitting_or_rendering(self):
        self.planner.side_effect = FilmPlanError('insufficient_quota')
        self.assertFalse(self.run_slot())
        self.assertFalse(self.run_slot())
        self.planner.assert_called_once()
        self.renderer.assert_not_called()
        self.assertEqual(self.doc['items'][0]['error_code'], 'insufficient_quota')

    def test_pending_job_resumes_across_slots_without_a_second_planning_call(self):
        self.assertFalse(self.run_slot())
        self.assertFalse(self.run_slot('2026-11-23-morning'))
        self.planner.assert_called_once()
        self.assertEqual(self.renderer.call_count, 2)
        self.assertEqual(len(self.doc['items']), 1)
        self.assertEqual(self.doc['items'][0]['source_peers'][0]['creator'], 'p1')

    def test_unknown_prior_reservation_blocks_duplicate_paid_planning(self):
        self.doc['items']=[{'id':'agent-2026-11-22-evening','sequence_slot':'2026-11-22-evening',
                           'generation_method':'agent_motion','status':'agent_planning','plan_reserved_at':'yesterday'}]
        self.assertFalse(self.run_slot())
        self.assertEqual(self.doc['items'][0]['error_code'],'submission_uncertain')
        self.planner.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe') and find_spec('PIL'), 'FFmpeg and Pillow required')
    def test_actual_edit_archive_and_repeat(self):
        raw = editor.SITE/'generated/video-36dc967ec3fc7e4cd1060f94.mp4'
        self.renderer.return_value=raw
        self.assertTrue(self.run_slot())
        self.assertFalse(self.run_slot())
        self.planner.assert_called_once()
        self.renderer.assert_called_once()
        self.assertEqual(self.events, ['agent_planning','plan_ready','rendering_sequence','generated'])
        f=json.loads((self.site/'data/creations.json').read_text())['items'][0]
        self.assertEqual((f['aspect_ratio'],f['duration'],f['generation_method']),('9:16',6,'agent_motion'))
        self.assertEqual(f['hook'],self.plan['shots'][0]['overlay'])
        self.assertEqual(f['peer_count'],2)
        self.assertLess(f['bytes'], editor.MAX_MEDIA)
        digest=hashlib.sha256((self.site/f['video_path']).read_bytes()).hexdigest()
        self.assertEqual(digest, f['sha256'])
        self.assertNotEqual(digest, hashlib.sha256(raw.read_bytes()).hexdigest())
        probe=subprocess.check_output(['ffprobe','-v','error','-show_entries','stream=codec_name',
                     '-of','default=nw=1:nk=1',str(self.site/f['video_path'])],text=True).strip()
        self.assertEqual(probe,'h264')


if __name__=='__main__':
    unittest.main()
