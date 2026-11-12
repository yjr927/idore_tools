import tempfile
import unittest
from unittest.mock import patch
from discovery import canonical_video,DiscoveryError,ArticleLinks,discover_public,normalize
from discovery_store import DiscoveryLibrary
from server import Store

URL='https://www.tiktok.com/@example/video/7699497385185119502'
PAGE='<h3>Simple shoulder dance</h3><p>Single performer dance tutorial.</p><blockquote cite="'+URL+'"><a href="'+URL+'">watch</a></blockquote>'
class DiscoveryTests(unittest.TestCase):
    def test_only_exact_public_video_urls(self):
        self.assertEqual(canonical_video(URL+'?tracking=1')[0],URL)
        for url in ['http://127.0.0.1/video/123','https://www.tiktok.com.evil.test/@x/video/12345678901','https://www.tiktok.com/tag/dance','https://x@www.tiktok.com/@x/video/12345678901']:
            with self.assertRaises(DiscoveryError):canonical_video(url)
    def test_public_parser_deduplicates_and_filters(self):
        with patch('discovery.fetch_article',return_value=PAGE):
            rows,reports=discover_public('hands','',12)
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_heading'],'Simple shoulder dance')
        self.assertFalse(rows[0]['motion_verified']);self.assertFalse(rows[0]['trend_verified'])
        self.assertIsNone(rows[0]['metrics']['views']);self.assertIsNone(rows[0]['published_at'])
        with patch('discovery.fetch_article',return_value=PAGE):
            rows,_=discover_public('hands','unmatched keyword',12)
        self.assertEqual(rows,[])
    def test_photo_carousels_are_not_video_candidates(self):
        with patch('discovery.fetch_article',return_value=PAGE.replace('Simple shoulder dance','Outfit photo carousel')):
            rows,_=discover_public('all','',12)
        self.assertEqual(rows,[])
    def test_source_failure_does_not_fabricate_results(self):
        with patch('discovery.fetch_article',side_effect=DiscoveryError('blocked')):
            rows,reports=discover_public('all','',12)
        self.assertEqual(rows,[]);self.assertTrue(all(r['status']=='blocked' for r in reports))
    def test_merge_preserves_favorites_and_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=Store(tmp);library=DiscoveryLibrary(store)
            first=normalize({'url':URL},'hands','2026-11-01T00:00:00+00:00')
            self.assertEqual(library.merge([first]),1);first['favorite']=True
            second=normalize({'url':URL},'hands','2026-11-11T00:00:00+00:00')
            self.assertEqual(library.merge([second]),0)
            row=library.public()['videos'][0]
            self.assertTrue(row['favorite']);self.assertEqual(row['first_seen_at'],'2026-11-01T00:00:00+00:00')
            self.assertEqual(row['last_seen_at'],'2026-11-11T00:00:00+00:00')
