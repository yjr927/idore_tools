"""Finish copy for two locked, Work-authored storyboards in the idore-production job."""
import copy
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from creative_service import CreativeService, TEXT_MODEL
from scripts.agent_film_planner import OPENROUTER, WORKER, FilmPlanError, _post, _slate, _table, _validate
from scripts.named_motion import NAMED, NAMED_METHOD, SELECTION, named_source
from scripts.observed_dance import BEATS
from scripts.pipeline_copy import DEFAULT_ROUTER_MODEL
from scripts.pipeline_state import now, touch
from scripts.run_agent_motion import approved_photos, attach_plan
from scripts.run_pipeline import REPOSITORY, SITE, STATE, checkpoint, read

ORIGINAL = {
    '2026-11-23-momoko': ('tiktok-7703265627972128021', 'role-3abb96e025889f47', '2026-11-23T14:31:59.539599Z'),
    '2026-11-23-bay': ('tiktok-7709762212679683342', 'role-323c636618027cbd', '2026-11-23T14:32:03.125827Z'),
}
BOARDS = ROOT / 'scripts/data/named-storyboards-20261123.json'
METHOD = 'work_locked_board_openrouter_copy'
VIDEO_FIELDS = ('submission_reserved', 'remote_id', 'video_path', 'generated_at', 'sha256')


def require(test, message):
    if not test:
        raise ValueError(message)


def copy_request(model, contexts):
    string = {'type': 'string'}
    fields = {'overlays': {'type': 'array', 'items': string}, 'caption': string,
              'hashtags': {'type': 'array', 'items': string}}
    copy_schema = {'type': 'object', 'additionalProperties': False, 'properties': fields, 'required': list(fields)}
    schema = {'type': 'object', 'additionalProperties': False, 'required': list(ORIGINAL),
              'properties': {slot: copy_schema for slot in ORIGINAL}}
    return {
        'model': model, 'max_tokens': 1500, 'provider': {'require_parameters': True, 'allow_fallbacks': False},
        'response_format': {'type': 'json_schema', 'json_schema': {'name': 'final_dance_copy', 'strict': True, 'schema': schema}},
        'messages': [
            {'role': 'system', 'content': (
                'Act as the English copy editor for two fixed six-second fictional-character dance cuts. '
                'The cast, identities, movements and framing are already locked. Deliver final English text for both. '
                'Use only the provided observations, supplied as reference data. Write three distinct chronological '
                'overlays each of at most nine words and 68 characters. Use the visual setup, turn and payoff. '
                'Write a conversational caption up to 150 characters, different from the first overlay; provide two '
                'to four distinct specific English hashtags with letters/numbers/underscores. No generic #fyp, '
                '#foryou, #trending, #viral, engagement bait, slogans, copied lyrics, URLs, mentions or invented '
                'popularity. No hashtags in the overlays or caption. Keep choices and shot order exactly fixed.')},
            {'role': 'user', 'content': json.dumps(contexts, ensure_ascii=False)},
        ]}


def final_decisions(reply, boards, materials, model, key):
    try:
        choice = reply['choices'][0]
        content = choice['message']['content']
        require(choice['finish_reason'] == 'stop' and isinstance(content, str) and len(content) <= 10000
                and key not in content, 'Unusable editorial response.')
        results = json.loads(content)
        require(isinstance(results, dict) and set(results) == set(ORIGINAL), 'Incomplete editorial batch.')
        outputs = {}
        for slot, row in results.items():
            require(isinstance(row, dict) and set(row) == {'overlays', 'caption', 'hashtags'} and
                    isinstance(row['overlays'], list) and len(row['overlays']) == 3, 'Incomplete editorial copy.')
            board = copy.deepcopy(boards[slot])
            for shot, new_text in zip(board['shots'], row['overlays']):
                shot['overlay'] = new_text
            board.update(caption=row['caption'], hashtags=row['hashtags'])
            _, _, candidates, photos = materials[slot]
            valid = _validate(board, candidates, photos, model)
            outputs[slot] = {'decision': 'render', 'trend_id': valid['source_id'], 'shots': valid['shots'],
                             'caption': valid['caption'], 'hashtags': valid['hashtags']}
        return outputs
    except (TypeError, ValueError, IndexError, KeyError, FilmPlanError):
        raise ValueError('Editorial reply failed validation; the paid request will not be repeated.') from None


def recover(document, boards, catalog, sources, films, site, run_id, model, key, save=checkpoint, send=_post):
    # Daily production can outlive this one-time commission or prune its historical rows.
    originals = [r for r in document.get('items', []) if r.get('sequence_slot') in ORIGINAL]
    unresolved = [r for r in originals
                  if (r.get('status'), r.get('stage'), r.get('error_code')) ==
                     ('needs_attention', 'planning', 'planner_unavailable')
                  and not r.get('agent_plan') and not any(r.get(k) for k in VIDEO_FIELDS)
                  and not any(f.get('sequence_slot') == r['sequence_slot'] or
                              f.get('trend_id') == ORIGINAL[r['sequence_slot']][0] or
                              f.get('source_url') == NAMED[r['sequence_slot']]['url'] for f in films)]
    if not unresolved:
        return 0
    require(set(boards) == set(ORIGINAL), 'Unexpected fixed storyboard batch.')
    require(isinstance(model, str) and model.startswith('openai/') and TEXT_MODEL.fullmatch(model[7:]),
            'Invalid configured editorial model.')
    materials, pending, contexts = {}, {}, {}
    approved = {photo['id']: photo for photo in approved_photos(catalog, site)}
    for slot, (tid, photo_id, original_time) in ORIGINAL.items():
        if not any(r['sequence_slot'] == slot for r in unresolved):
            continue
        rows = [r for r in originals if r['sequence_slot'] == slot]
        require(len(rows) == 1 and rows[0].get('id') == 'agent-' + slot, 'Ambiguous original queue row.')
        row = rows[0]
        url = NAMED[slot]['url']
        require(row.get('generation_method') == NAMED_METHOD and row.get('selection_basis') == SELECTION
                and row.get('trend_verified') is False and row.get('trend_id') == tid and row.get('source_url') == url
                and row.get('plan_reserved_at') == original_time, 'Original source or reservation changed.')
        duplicates = [r for r in document['items'] if r is not row and
                      (tid in (r.get('trend_id'), r.get('source_id')) or r.get('source_url') == url)]
        require(not any(r.get('agent_plan') or any(r.get(k) for k in VIDEO_FIELDS) for r in duplicates),
                'Another queue row already has a plan or video for this source.')
        source = named_source(slot, next((s for s in sources if s.get('id') == tid), None))
        photo = approved.get(photo_id)
        require(source and photo, 'Locked source or approved role missing.')
        candidates, photos = _table([source], [photo], [], named_slot=slot)
        work = _validate(boards[slot], candidates, photos, model)
        require(work['photo_ids'] == [photo_id] and work['source_id'] == tid, 'Fixed storyboard binding changed.')
        materials[slot] = source, photo, candidates, photos
        contexts[slot] = {'character': photo['name'], 'movement': BEATS[tid][1],
                          'locked_shots': work['shots'], 'draft_caption': work['caption'], 'draft_tags': work['hashtags']}
        pending[slot] = row
    if not pending:
        return 0

    batch_hash = hashlib.sha256(json.dumps(boards, sort_keys=True).encode()).hexdigest()
    markers = [row.get('editorial_recovery') for row in pending.values()]
    if all(marker is None for marker in markers):
        require(set(pending) == set(ORIGINAL), 'Paid batch requires both original unplanned slots.')
        require(CreativeService._valid_key(key), 'Missing project credential.')
        payload = copy_request(model, contexts)
        stamp = now()
        for row in pending.values():
            row['editorial_recovery'] = {
                'method': METHOD, 'reserved_at': stamp, 'run_id': run_id, 'model': model, 'batch_sha256': batch_hash,
                'original_planning_row': dict(row),
            }
            touch(row)
        save(document)  # This must publish successfully before the only billable request.
        reply = send(OPENROUTER, payload, key)
        receipt = reply.get('id') if isinstance(reply, dict) else None
        has_receipt = (isinstance(receipt, str) and key not in receipt and
                       re.fullmatch(r'gen-[0-9]{8,16}-[A-Za-z0-9_-]{6,120}', receipt))
        if has_receipt:
            for row in pending.values():
                row['editorial_recovery']['generation_id'] = receipt
        try:
            outputs = final_decisions(reply, boards, materials, model, key)
        except ValueError:
            if has_receipt:
                for row in pending.values():
                    touch(row)
                save(document)
            raise
        for slot, row in pending.items():
            row['editorial_recovery']['approved_copy'] = outputs[slot]
            touch(row)
        save(document)  # Both decisions are recoverable before any Worker request.
    else:
        for marker in markers:
            require(isinstance(marker, dict) and marker.get('method') == METHOD and
                    marker.get('batch_sha256') == batch_hash and marker.get('model') == model and
                    isinstance(marker.get('approved_copy'), dict),
                    'Existing editorial submission has no compatible recoverable response; do not resubmit.')

    for slot, row in pending.items():
        source, photo, candidates, photos = materials[slot]
        board = row['editorial_recovery']['approved_copy']
        # On resume, re-validate persisted copy and the complete original shot binding.
        require(board['trend_id'] == boards[slot]['trend_id'] and
                [(s['photo_id'], s['framing']) for s in board['shots']] ==
                [(s['photo_id'], s['framing']) for s in boards[slot]['shots']], 'Persisted binding changed.')
        _validate(board, candidates, photos, model)
        accepted = send(WORKER + 'accept', {'slate': _slate(model, slot, candidates, photos),
                                            'response': json.dumps(board, ensure_ascii=False)})
        require(accepted == board, 'Cloudflare validation did not confirm the exact saved decision.')
        plan = _validate(accepted, candidates, photos, model)
        attribution = {'storyboard_author': 'work', 'copy_provider': 'openrouter', 'validation_host': 'cloudflare',
                       'recovery_method': METHOD}
        plan.update(selection_basis=SELECTION, **attribution)
        attach_plan(row, plan, [source], [photo])
        row.update(agent_plan=plan, status='plan_ready', stage='planning', error_code='', **attribution,
                   message='Work 已固定角色与分镜，OpenRouter 已完成英文文案，Cloudflare 已校验。')
        touch(row)
        save(document)
    return len(pending)


def main():
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('GITHUB_REPOSITORY') == REPOSITORY and
            os.environ.get('GITHUB_REF') == 'refs/heads/main' and os.environ.get('PIPELINE_PERSIST') == 'true' and
            os.environ.get('PIPELINE_BRANCH', 'main') == 'main' and re.fullmatch(r'[0-9]+', run_id),
            'Recovery requires a trusted, persistent main workflow.')
    data = SITE / 'data'
    count = recover(read(STATE), read(BOARDS), read(data/'characters.json')['items'], read(data/'discovery.json')['videos'],
                    read(data/'creations.json')['items'], SITE, run_id,
                    os.environ.get('OPENROUTER_TEXT_MODEL') or DEFAULT_ROUTER_MODEL, os.environ.get('OPENROUTER_API_KEY', ''))
    print(f'Validated {count} new recovery plans.')


if __name__ == '__main__':
    main()
