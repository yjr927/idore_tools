#!/usr/bin/env python3
"""Run the authorized KAI request once, retaining redacted rejection details."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import video_service
from scripts import run_kai_tiktok_link_cloud

def main():
    original = video_service._transport
    diagnostic = {'schema_version': 1, 'http_rejection_observed': False}
    def transport(request, *, timeout, max_bytes):
        status, headers, raw = original(request, timeout=timeout, max_bytes=max_bytes)
        if request.get_method() == 'POST' and status not in (200, 201, 202):
            diagnostic.update(http_rejection_observed=True, http_status=status)
            try:
                document = json.loads(raw)
                error = document.get('error') if isinstance(document, dict) else None
                message = error.get('message') if isinstance(error, dict) else error if isinstance(error, str) else None
                code = error.get('code') if isinstance(error, dict) else None
                key = os.environ.get('OPENROUTER_API_KEY', '').strip()
                if isinstance(message, str):
                    diagnostic['message'] = ' '.join(video_service._safe_prompt(message, key).split())[:800]
                if type(code) in (str, int):
                    diagnostic['code'] = ' '.join(video_service._safe_prompt(str(code), key).split())[:120]
            except Exception:
                diagnostic['message'] = 'provider_error_body_not_available'
        return status, headers, raw
    video_service._transport = transport
    try:
        return run_kai_tiktok_link_cloud.main()
    finally:
        video_service._transport = original
        output = Path(os.environ.get('VIDEO_OUTPUT_DIR', 'generation-output'))
        output.mkdir(parents=True, exist_ok=True)
        (output / 'provider-rejection-summary.json').write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps(diagnostic, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    raise SystemExit(main())
