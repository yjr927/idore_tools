import copy
from urllib.error import HTTPError
import json
import unittest
from unittest.mock import patch

from scripts.agent_film_planner import OPENROUTER, WORKER, FilmPlanError, _post, plan_film, plan_named_film


def trend(gender='male'):
    url = 'https://www.tiktok.com/@somebody/video/7700000000000000000'
    return {'id': 'tiktok-7700000000000000000', 'url': url, 'transformation': 'none', 'source_kind': 'chatgpt_work', 'original_watched': True,
            'material_group': gender, 'checks': {'solo': True}, 'metrics': {'views': 1000000, 'likes': 100000},
            'metric_display': {'views': '1,000,000', 'likes': '100,000'}, 'title': 'Hold the look',
            'motion_summary': 'Turn on the first beat, eye contact and sleeve tug at the last beat.',
            'last_seen_at': '2026-11-22T14:00:00Z',
            'related_videos': [
                {'url': 'https://www.tiktok.com/@other' + str(i) + '/video/' + str(7700000000000000000 + i),
                 'same_pattern': True, 'original_watched': True, 'note': 'Same pause and sleeve tug.'}
                for i in [1, 2]]}


def photo(gender='male'):
    return {'id': 'role-' + gender, 'sha256': '1'*64, 'enabled': True, 'auto_match_eligible': True,
            'name': 'Rin', 'cast_type': 'solo', 'material_group': gender, 'ip': 'Night Shift',
            'image_url': 'https://raw.githubusercontent.com/yjr927/idore_tools/'+'1'*40+'/site/characters/Rin/face.png'}


def decision(gender='male'):
    return {'decision': 'render', 'trend_id': trend()['id'], 'shots': [
        {'photo_id': photo(gender)['id'], 'framing': f, 'overlay': o} for f, o in [
            ('wide', 'told him to act normal'), ('medium', 'the sleeve tug was rehearsed'),
            ('close', 'so was the look back')]], 'caption': 'that sleeve has been through three rehearsals',
            'hashtags': ['#CharacterEdit', '#SleeveTug']}


def transport(output, calls):
    def send(url, payload, key=None):
        calls.append((url, payload, key))
        if url == WORKER + 'request':
            content = [{'type':'text','text':'Only the verified slate.'}] + [
                {'type':'image_url','image_url':{'url':photo['image_url']}} for photo in payload['photos']]
            return {'model':payload['model'], 'messages':[{'role':'system','content':'Direct the cut.'},
                                                         {'role':'user','content':content}],
                    'response_format':{'type':'json_schema','json_schema':{'name':'test','schema':{}}}}
        if url == OPENROUTER:
            return {'choices':[{'finish_reason':'stop', 'message':{'content':json.dumps(output)}}]}
        if url == WORKER + 'accept':
            return json.loads(payload['response'])
        raise AssertionError('unexpected destination')
    return send


class PlannerTests(unittest.TestCase):
    def test_named_sources_plan_once_with_observed_counts_and_approved_editorial_cast(self):
        for name, creator, video, group, observed_group, likes in [
            ('momoko','itsmomoko.jp','7703265627972128021','female','female',37),
            ('bay','mybaybie','7709762212679683342','male','unknown',4752),
        ]:
            source = dict(trend(observed_group), id='tiktok-' + video, url=f'https://www.tiktok.com/@{creator}/video/{video}',
                          kind='hands', checks={'fixed_camera':True, 'solo':True}, transformation='unknown',
                          related_videos=[], metrics={'likes':likes,'views':None}, metric_display={},
                          motion_summary='观察到双手贴近脸侧，再交叉双臂。动作不限定男女角色用途。')
            calls = []
            output = dict(decision(group),trend_id=source['id'])
            slot = '2026-11-23-' + name
            result = plan_named_film([source], [photo('male'), photo('female')], slot=slot,
                                     api_key='fake-key', client=transport(output,calls))
            self.assertEqual(result['selection_basis'],'user_selected_reference')
            stored = calls[0][1]['trends'][0]
            self.assertEqual(stored['metrics'],{'views':None,'likes':likes})
            self.assertEqual((stored['source_material_group'],stored['material_group']), (observed_group,group))
            self.assertEqual((stored['source_transformation'],stored['transformation']),('unknown','none'))
            self.assertEqual(stored['other_creators'],0)
            self.assertEqual(sum(url == OPENROUTER for url,_,_ in calls),1)
            for planner, wrong_slot, reference in [
                (plan_film,slot,source), (plan_named_film,slot.replace('23','24'),source),
                (plan_named_film,slot,dict(source,url=source['url'].replace(creator,'unrelated'))),
                (plan_named_film,slot,dict(source,original_watched=False)),
            ]:
                with self.assertRaises(FilmPlanError):
                    planner([reference], [photo(group)], slot=wrong_slot, api_key='fake-key',
                            client=lambda *_: self.fail('unexpected request for unapproved source'))

    def test_worker_directs_one_openrouter_inference_for_each_cast(self):
        for g in ('male', 'female'):
            with self.subTest(group=g):
                calls = []
                film = plan_film([trend(g)], [photo('male'), photo('female')], slot='evening',
                                 api_key='fake-key', client=transport(decision(g), calls))
                self.assertEqual([(url,key) for url,_,key in calls], [(WORKER+'request',None),(OPENROUTER,'fake-key'),(WORKER+'accept',None)])
                self.assertEqual(calls[0][1]['photos'][0]['photo_id'],'role-'+g)
                self.assertEqual(calls[1][1]['response_format']['type'],'json_schema')
                self.assertNotIn('fake-key', json.dumps([payload for _,payload,_ in calls]))
                self.assertEqual((film['provider'], film['execution_host'], film['material_group'], film['photo_ids']),
                                 ('openrouter','cloudflare', g, ['role-'+g]))
                self.assertTrue(film['model'].startswith('openai/'))
                self.assertEqual(film['hook'], film['shots'][0]['overlay'])
                self.assertNotEqual(film['caption'], film['hook'])
                self.assertEqual(film['source_url'], trend()['url'])

    def test_hold_before_provider_for_missing_evidence_gender_or_safe_photos(self):
        cases = [
            (dict(trend(), related_videos=[]), photo(), 'waiting_trend'),
            (dict(trend(), metric_display=None), photo(), 'waiting_trend'),
            (dict(trend(), id='tiktok-7700000000000000001'), photo(), 'waiting_trend'),
            (trend('unknown'), photo(), 'waiting_trend'),
            (dict(trend(),transformation='makeup'), photo(), 'waiting_material'),
            (trend('female'), photo('male'), 'waiting_material'),
            (dict(trend(), checks={'solo':False}), photo(), 'waiting_material'),
            (trend(), dict(photo(), enabled=False), 'waiting_material'),
            (trend(), dict(photo(), image_url='https://example.com/f.png'), 'waiting_material'),
        ]
        for source, image, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(FilmPlanError) as caught:
                    plan_film([source], [image], slot='morning', client=lambda _: self.fail('unexpected paid request'))
                self.assertEqual(caught.exception.code, expected)

    def test_invalid_response_never_retries(self):
        valid = decision()
        bad_id = copy.deepcopy(valid)
        bad_id['shots'][2]['photo_id'] = 'role-female'
        options = [(bad_id, 'invalid_output'), (dict(valid, decision='hold'), 'no_fit'),
                   (dict(valid, trend_id='made_up'), 'invalid_output'), (dict(valid, hashtags=['#viral','#fyp']), 'invalid_output'),
                   (dict(valid, caption='wait for it'), 'invalid_output')]
        for output, expected in options:
            calls = []
            with self.subTest(expected=expected):
                with self.assertRaises(FilmPlanError) as caught:
                    plan_film([trend()], [photo()], slot='morning', api_key='fake-key', client=transport(output, calls))
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(sum(call[0] == OPENROUTER for call in calls), 1)

    def test_missing_key_quota_and_ambiguous_submission_are_distinct(self):
        with self.assertRaises(FilmPlanError) as caught:
            plan_film([trend()], [photo()], slot='morning', api_key='')
        self.assertEqual(caught.exception.code, 'missing_key')
        for error, expected in [(HTTPError(OPENROUTER,402,'',{},None),'insufficient_quota'),
                                (OSError('unknown result'),'submission_uncertain')]:
            with patch('scripts.agent_film_planner.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(FilmPlanError) as caught:
                    _post(OPENROUTER, {}, 'test-key-only')
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(opener.return_value.open.call_count, 1)
        with self.assertRaises(FilmPlanError):
            _post(WORKER + 'request', {}, 'must-not-transmit')

    def test_modified_worker_message_never_reaches_openrouter(self):
        for tamper in ('model','url'):
            calls=[]
            def send(url, payload, key=None):
                r=transport(decision(),calls)(url,payload,key)
                if tamper == 'model': r['model']='other/model'
                else: r['messages'][1]['content'][1]['image_url']['url']='https://example.org/unapproved.jpg'
                return r
            with self.assertRaises(FilmPlanError):
                plan_film([trend()],[photo()],slot='evening',client=send,api_key='fake-key')
            self.assertEqual([c[0] for c in calls], [WORKER+'request'])

    def test_previously_used_trend_does_not_ask_model_again(self):
        with self.assertRaises(FilmPlanError) as caught:
            plan_film([trend()], [photo()], slot='next', prior_films=[{'source_id': trend()['id']}],
                      client=lambda _: self.fail('unexpected paid request'))
        self.assertEqual(caught.exception.code, 'waiting_trend')
