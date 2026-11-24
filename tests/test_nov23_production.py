import copy
from pathlib import Path
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from scripts import produce_nov23_replacements as runner
from scripts.nov23_video_receipts import NamedVideoError


class ReservationTests(unittest.TestCase):
    def setUp(self):
        clock = patch.object(runner, 'datetime', wraps=datetime).start()
        clock.now.return_value = datetime(2026,11,24,2,tzinfo=timezone.utc)
        self.slot = next(iter(runner.SLOTS))
        self.row = {'motion_binding': ['fake-test-binding'], 'shots':[], 'sequence_slot':self.slot}
        self.inputs = patch.object(runner,'inputs',return_value=(self.row,{},{ })).start()
        self.addCleanup(patch.stopall)
        self.records = []
        self.save = patch.object(runner, 'persist',
                                 side_effect=lambda ledger: self.records.append(copy.deepcopy(ledger))).start()

    def test_only_submit_after_durable_claim_and_preserve_prior_attempt(self):
        ledger = {}
        def accept(slot, row, source, photo, scratch, receipt):
            self.assertEqual(self.records[0][slot]['status'], 'reserved')
            self.assertEqual(self.records[0][slot]['prior_revision'], runner.SLOTS[slot][0])
            receipt({'remote_id':'gen-vid-1795482000-0123456789abcdefghij', 'video_post_http_status':202})
            return ledger[slot]['remote_id']
        with patch('scripts.nov23_video_receipts.submit_named',side_effect=accept) as post:
            runner.submit(self.slot,ledger,'111')
            runner.submit(self.slot,ledger,'222')
        self.assertEqual(post.call_count,1)
        self.assertEqual(ledger[self.slot]['status'],'accepted')
        self.assertEqual(ledger[self.slot]['attempt'],2)

    def test_stale_claim_never_calls_provider(self):
        self.save.side_effect = runner.CheckpointError
        with patch('scripts.nov23_video_receipts.submit_named') as post:
            with self.assertRaises(runner.CheckpointError):
                runner.submit(self.slot,{},'111')
        post.assert_not_called()

    def test_ambiguous_response_remains_reserved_after_rerun(self):
        ledger={}
        with patch('scripts.nov23_video_receipts.submit_named',side_effect=NamedVideoError('submission_uncertain')) as post:
            runner.submit(self.slot,ledger,'111')
            runner.submit(self.slot,ledger,'222')
        self.assertEqual(post.call_count,1)
        self.assertEqual(ledger[self.slot]['error_code'],'submission_uncertain')
        self.assertFalse(runner.collect(self.slot,ledger))

    def test_resume_only_downloads_and_creates_artifact_outside_gallery(self):
        ledger={self.slot:{'remote_id':'gen-vid-1795482000-0123456789abcdefghij','status':'accepted'}}
        with tempfile.TemporaryDirectory() as temp, patch.object(runner,'OUT',Path(temp)/'files'), \
                patch('scripts.nov23_video_receipts.submit_named') as post, \
                patch('scripts.nov23_video_receipts.download_named',return_value=Path(temp)/'raw.mp4') as download, \
                patch.object(runner,'finish_cut',side_effect=lambda raw,shots,out:out.write_bytes(b'fixture')):
            self.assertTrue(runner.collect(self.slot,ledger))
            self.assertEqual(runner.read(runner.OUT/self.slot/'metadata.json')['sha256'],ledger[self.slot]['sha256'])
        post.assert_not_called()
        download.assert_called_once()
