import unittest

from script_agent.segmenter import numbered_scene_text, split_scenes


class SegmenterTests(unittest.TestCase):
    def test_splits_chinese_and_english_headings(self):
        text = """人物表

第一场 客厅 日
甲：你来了。

INT. HALLWAY - NIGHT
乙 walks away.
"""
        scenes = split_scenes(text)
        self.assertEqual(3, len(scenes))
        self.assertEqual("开场资料", scenes[0].heading)
        self.assertTrue(scenes[1].heading.startswith("第一场"))
        self.assertTrue(scenes[2].heading.startswith("INT."))
        self.assertIn("L000004", numbered_scene_text(scenes[1]))

    def test_fallback_chunks_without_headings(self):
        text = "段落一。\n\n段落二。\n\n段落三。"
        scenes = split_scenes(text, max_chars=10)
        self.assertGreaterEqual(len(scenes), 2)
        self.assertEqual(1, scenes[0].start_line)


if __name__ == "__main__":
    unittest.main()

