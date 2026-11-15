#!/usr/bin/env python3
"""Manually requested cloud generation; credentials never enter artifacts or logs."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_service import VideoService, VideoError


def task_summary(job):
    """Keep recovery identifiers, never prompts, media URLs or provider payloads."""
    return {key: job.get(key) for key in ('id', 'remote_id', 'model', 'status', 'created_at', 'cost', 'error')
            if isinstance(job, dict) and key in job}


def main():
    output = Path(os.environ.get('VIDEO_OUTPUT_DIR', 'generation-output'))
    output.mkdir(parents=True, exist_ok=True)
    job = None
    try:
        settings = json.loads(os.environ.get('GENERATION_INPUTS', '{}'))
        if not isinstance(settings, dict):
            raise VideoError('生成参数必须是对象。')
        if not os.environ.get('OPENROUTER_API_KEY'):
            raise VideoError('请先在 GitHub Actions Secrets 保存 OPENROUTER_API_KEY。')
        service = VideoService(output)
        remote_id = str(settings.get('resume_job_id') or '').strip()
        model = settings.get('model', '')
        if remote_id:
            job = service.recover(remote_id, model)
        else:
            if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
                raise VideoError('本轮是重新运行。为避免重复扣费，请新建一次任务并填写原任务 ID 来继续查询。')
            if settings.get('allow_paid') is not True:
                raise VideoError('尚未勾选允许本次使用 OpenRouter 额度，没有提交生成。')
            mode = settings.get('mode', 'text')
            body = dict(idempotency_key='github-' + os.environ.get('GITHUB_RUN_ID', 'manual'),
                model=model, mode=mode, prompt=settings.get('prompt', ''),
                duration=int(settings.get('duration', 5)), resolution=settings.get('resolution', '720p'),
                aspect_ratio='9:16', generate_audio=False, consent=True)
            first = str(settings.get('first_frame_url') or '').strip()
            last = str(settings.get('last_frame_url') or '').strip()
            reference = str(settings.get('reference_video_url') or '').strip()
            if mode == 'frames':
                body['first_frame'] = first
                if last: body['last_frame'] = last
                if reference: raise VideoError('首尾帧模式不同时使用动作参考视频。')
            elif mode == 'references':
                body['references'] = []
                if first: body['references'].append({'type': 'image_url', 'url': first})
                if reference: body['references'].append({'type': 'video_url', 'url': reference})
                if last: raise VideoError('参考素材模式不同时指定尾帧。')
            elif first or last or reference:
                raise VideoError('纯文字模式不能附带图片或参考视频。')
            job = service.submit(body)
        deadline = time.monotonic() + 25 * 60
        while job['status'] in ('pending', 'in_progress', 'submitting') and time.monotonic() < deadline:
            time.sleep(30)
            job = service.refresh(job['id'])
            print('Video task status: ' + job['status'], flush=True)
        if job['status'] == 'completed':
            job = service.download(job['id'])
        summary = task_summary(job)
        (output / 'generation-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        # Retain local job IDs for recovery, but never publish user prompts/media URLs.
        print('Video task status: ' + str(job.get('status')), flush=True)
        return 0 if job.get('status') == 'completed' and job.get('result_url') else 1
    except (VideoError, ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, VideoError) else '生成参数或本机结果无法读取。'
        summary = task_summary(job)
        if 'status' in summary:
            summary['last_known_status'] = summary['status']
        summary.update(status='not_completed', message=message)
        (output / 'generation-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        print(message, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
