#!/usr/bin/env python3
"""One explicit diagnostic: does Seedance fetch this public TikTok share page?"""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import video_service
from scripts import run_video_generation
from scripts.run_kai_motion_cloud import IMAGE_URL, MODEL, PROMPT

REFERENCE = 'https://www.tiktok.com/@bizzyboom/video/7706633304702848276'

def main():
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'
            or os.environ.get('KAI_ALLOW_PAID') != 'true' or not os.environ.get('OPENROUTER_API_KEY', '').strip()):
        print('Direct-share test not authorized in this execution.'); return 1
    original_validator = video_service._media_url
    def diagnostic_url(value, kind):
        # Fixed user-selected public share URL only; production validation is unchanged.
        return REFERENCE if kind == 'video_url' and value == REFERENCE else original_validator(value, kind)
    previous_inputs = os.environ.get('GENERATION_INPUTS')
    video_service._media_url = diagnostic_url
    os.environ['GENERATION_INPUTS'] = json.dumps({'model': MODEL, 'mode': 'references', 'prompt': PROMPT,
        'duration': 6, 'resolution': '720p', 'aspect_ratio': '9:16', 'generate_audio': False,
        'allow_paid': True, 'first_frame_url': IMAGE_URL, 'reference_video_url': REFERENCE})
    try:
        # The standard runner durably journals before one POST, never retries it,
        # records the remote job, polls and downloads only completed output.
        with contextlib.redirect_stderr(io.StringIO()):
            return run_video_generation.main()
    finally:
        video_service._media_url = original_validator
        if previous_inputs is None: os.environ.pop('GENERATION_INPUTS', None)
        else: os.environ['GENERATION_INPUTS'] = previous_inputs

if __name__ == '__main__':
    raise SystemExit(main())
