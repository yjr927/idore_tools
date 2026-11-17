import copy
import unittest

from scripts.pipeline_state import PipelineError, approve, approved_content, match_character, revision, sync_queue, touch


class PipelineStateTests(unittest.TestCase):
    def setUp(self):
        self.character = {'id': 'kai', 'name': 'KAI', 'material_group': 'male', 'cast_type': 'solo', 'enabled': True}
        self.video = {'id': 'tiktok-7706633304702848276', 'url': 'https://www.tiktok.com/@bizzyboom/video/7706633304702848276',
            'material_group': 'male', 'checks': {'solo': True}, 'transformation': 'none', 'original_watched': True, 'kind': 'hands'}

    def test_matching_never_crosses_gender_or_people_count(self):
        self.assertEqual(match_character(self.video, [self.character])[1], 'queued')
        for changed in ({'material_group': 'female'}, {'checks': {'solo': False}}):
            self.assertEqual(match_character(dict(self.video, **changed), [self.character])[1], 'waiting_material')
        self.assertEqual(match_character(dict(self.video, material_group='unknown'), [self.character])[1], 'waiting_classification')

    def test_transformation_requires_defined_before_and_after(self):
        self.assertEqual(match_character(dict(self.video, transformation='outfit'), [self.character])[1], 'waiting_material')
        self.assertEqual(match_character(dict(self.video, transformation='unknown'), [self.character])[1], 'waiting_classification')

    def test_sync_uses_new_york_day_distinct_sources_and_does_not_reset_progress(self):
        videos = [dict(self.video, id=f'tiktok-{7706633304702848276+i}') for i in range(5)]
        discovery = {'videos': videos, 'runs': [{'observed_at': '2026-11-17T01:00:00Z', 'video_snapshots': videos + videos}]}
        doc = {'items': []}
        sync_queue(doc, discovery, [self.character], '2026-11-16')
        self.assertEqual(len(doc['items']), 4)
        doc['items'][0]['status'] = 'pending_review'
        sync_queue(doc, discovery, [self.character], '2026-11-16')
        self.assertEqual(len(doc['items']), 4)
        self.assertEqual(doc['items'][0]['status'], 'pending_review')
        self.assertEqual(sync_queue({'items': []}, discovery, [], '2026-11-17')['items'], [])

    def item(self):
        i = {'id': 'review-1', 'status': 'pending_review', 'caption': 'A gesture from KAI.',
             'hashtags': [{'tag': '#KAI'}], 'video_path': 'generated/video-a.mp4', 'sha256': 'a'*64}
        touch(i)
        return i

    def test_approval_binds_edited_copy_and_media(self):
        i = self.item()
        approve(i, i['revision'], 'Your next move.', '#KAI #VirtualIdol', 'owner')
        self.assertTrue(approved_content(i))
        self.assertEqual(i['caption'], 'Your next move.')
        i['status'] = 'uploading'
        self.assertTrue(approved_content(i))
        for field, value in [('caption', 'Changed'), ('sha256', 'b'*64), ('video_path', 'generated/other.mp4')]:
            changed = copy.deepcopy(i)
            changed[field] = value
            self.assertFalse(approved_content(changed))

    def test_stale_or_missing_copy_cannot_approve(self):
        i = self.item()
        with self.assertRaises(PipelineError):
            approve(i, '0'*64, 'Hi', '#KAI', 'owner')
        i['status'] = 'generating_copy'
        touch(i)
        with self.assertRaises(PipelineError):
            approve(i, i['revision'], 'Hi', '#KAI', 'owner')

    def test_invalid_tags_never_change_review_state(self):
        for tags in ('', 'not-a-hashtag', '#KAI https://evil.invalid'):
            i = self.item()
            old = copy.deepcopy(i)
            with self.assertRaises(PipelineError):
                approve(i, i['revision'], 'Hi', tags, 'owner')
            self.assertEqual(i, old)


if __name__ == '__main__':
    unittest.main()
