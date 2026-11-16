#!/usr/bin/env python3
"""Resolve the selected public TikTok link in memory, then generate once on GitHub."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.resolve_tiktok_reference import resolve_tiktok_reference
from scripts import run_kai_motion_cloud

REFERENCE = 'https://www.tiktok.com/@bizzyboom/video/7706633304702848276'

def main():
    output = Path(os.environ.get('VIDEO_OUTPUT_DIR', 'generation-output'))
    summary = {'schema_version': 1, 'status': 'stopped', 'resolver': 'TikWM', 'resolved': False,
               'core_runner_invoked': False}
    try:
        if (os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'
                or os.environ.get('KAI_ALLOW_PAID') != 'true' or not os.environ.get('OPENROUTER_API_KEY', '').strip()):
            raise ValueError('authorization_required')
        resolved = resolve_tiktok_reference(REFERENCE)
        summary.update(resolved=True, reference=resolved.safe_summary())
        # The core independently validates and fully downloads the source MP4
        # before a single journaled paid submission; URLs never become artifacts.
        summary['core_runner_invoked'] = True
        status = run_kai_motion_cloud.main(resolved_reference=(resolved.url, resolved.duration))
        summary['status'] = 'completed' if status == 0 else 'not_completed'
        return status
    except Exception:
        summary['reason'] = 'reference_resolution_or_authorization_failed'
        print('KAI TikTok-link task stopped before generating.', flush=True)
        return 1
    finally:
        output.mkdir(parents=True, exist_ok=True)
        target = output / 'tiktok-resolution-summary.json'
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(target)

if __name__ == '__main__':
    raise SystemExit(main())
