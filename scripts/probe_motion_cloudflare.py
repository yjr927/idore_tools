"""Read-only comparison of the two November 23 Cloudflare planner routes from Actions."""
import json
import os
from pathlib import Path
import re
import sys
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from creative_service import _NoRedirect
from scripts.agent_film_planner import (
    MAX_RESULT, WORKER, FilmPlanError, _inference, _slate, _table, _validate,
)
from scripts.named_motion import NAMED, named_source
from scripts.pipeline_copy import DEFAULT_ROUTER_MODEL
from scripts.run_agent_motion import approved_photos

SLOTS = ('2026-11-23-momoko', '2026-11-23-bay')
CLIENTS = {'legacy': 'Python-urllib/3.11', 'production': 'idore-film-planner/1.0'}
WORKER_ERRORS = {'invalid_slate', 'invalid_json', 'invalid_output', 'missing_model', 'method_not_allowed',
                 'invalid_content_type', 'https_required', 'request_too_large', 'not_found'}
FIXED_WORKER = 'https://idore-tools.yjr927.workers.dev/agent/plan/'


def build_slates(site):
    def data(name):
        return json.loads((site / 'data' / (name + '.json')).read_text(encoding='utf-8'))

    queue = {row.get('sequence_slot'): row for row in data('pipeline')['items']}
    discovery = {row.get('id'): row for row in data('discovery')['videos']}
    photos = approved_photos(data('characters')['items'], site)
    gallery = data('creations')['items']
    model = os.environ.get('OPENROUTER_TEXT_MODEL') or DEFAULT_ROUTER_MODEL
    for slot in SLOTS:
        reference, row = NAMED[slot], queue[slot]
        if row.get('trend_id') != reference['source_id'] or row.get('source_url') != reference['url']:
            raise ValueError('Named queue and schedule disagree.')
        source = named_source(slot, discovery[reference['source_id']])
        if source is None:
            raise ValueError('Missing named observation.')
        # Exclude this slot's own output if the archived film is present at probe time.
        prior = [film for film in gallery if film.get('sequence_slot') != slot]
        trends, shortlist = _table([source], photos, prior, named_slot=slot)
        slate = _slate(model, slot, trends, shortlist)
        if set(trends) != {reference['source_id']} or not slate['photos']:
            raise ValueError('Unexpected named slate.')
        photo = slate['photos'][0]
        # This exercises the complete accept validator. No model produced this fixture.
        decision = {'decision': 'render', 'trend_id': reference['source_id'],
                    'shots': [{'photo_id': photo['photo_id'], 'framing': framing, 'overlay': overlay}
                              for framing, overlay in [('wide','Find the first beat'), ('medium','Hold that pose'),
                                                       ('close','Catch the last count')]],
                    'caption': 'A rehearsal cut in three counts.', 'hashtags': ['#RehearsalCut','#BeatCheck']}
        _validate(decision, trends, shortlist, model)
        yield slot, slate, trends, shortlist, decision


def post(stage, payload, user_agent, validate):
    if WORKER != FIXED_WORKER or stage not in ('request', 'accept') or user_agent not in CLIENTS.values():
        raise ValueError('Forbidden probe destination or client.')
    body = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    req = Request(WORKER + stage, data=body, method='POST', headers={
        'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': user_agent})
    started = perf_counter()
    http, issue, ray = None, 'transport_error', None
    try:
        try:
            response = build_opener(_NoRedirect).open(req, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            http = response.status
            raw = response.read(MAX_RESULT + 1 if http == 200 else 4096)
            ray_header = response.headers.get('CF-Ray', '')
            if re.fullmatch(r'[A-Fa-f0-9]{12,32}(?:-[A-Z]{3})?', ray_header):
                ray = ray_header
            if http != 200:
                edge = re.search(rb'\berror\s+code\s*:\s*(1\d{3})\b', raw, re.I)
                issue = 'cf_' + edge.group(1).decode('ascii') if edge else 'http_error'
                if not edge:
                    try:
                        document = json.loads(raw)
                        name = document.get('error')
                        candidates = [document.get('code'), name]
                        if isinstance(name, dict):
                            candidates.append(name.get('code'))
                        errors = document.get('errors')
                        if isinstance(errors, list):
                            candidates.extend(item.get('code') for item in errors if isinstance(item, dict))
                        if any(str(value) == '1010' for value in candidates):
                            issue = 'cf_1010'
                        elif isinstance(name, str) and name in WORKER_ERRORS:
                            issue = name
                    except (ValueError, AttributeError, UnicodeError):
                        pass
            elif response.geturl() != req.full_url or len(raw) > MAX_RESULT:
                issue = 'invalid_response'
            else:
                try:
                    validate(json.loads(raw))
                    issue = 'validated'
                except (ValueError, UnicodeError, TypeError, KeyError, FilmPlanError):
                    issue = 'invalid_response'
    except (TimeoutError, URLError, OSError):
        pass
    return {'http': http, 'outcome': issue, 'cf_ray': ray, 'latency_ms': round((perf_counter()-started)*1000)}


def main():
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('GITHUB_REPOSITORY') != 'yjr927/idore_tools':
        raise SystemExit('This transport probe must run in the repository Actions environment.')
    slates = list(build_slates(ROOT / 'site'))
    passed = True
    for round_number in (1, 2):
        for slot, slate, trends, shortlist, decision in slates:
            accept_input = {'slate': slate, 'response': json.dumps(decision, separators=(',', ':'))}
            for client, user_agent in CLIENTS.items():
                for stage in ('request', 'accept'):
                    if stage == 'request':
                        result = post(stage, slate, user_agent, lambda v: _inference(v, slate))
                    else:
                        result = post(stage, accept_input, user_agent,
                                      lambda v: _validate(v, trends, shortlist, slate['model']))
                    print(json.dumps({'round': round_number, 'slot':slot, 'client':client,
                                      'stage':stage, **result}, separators=(',', ':')), flush=True)
                    if result['http'] == 429:
                        raise SystemExit('Worker requested rate limiting. Probe stopped.')
                    if client == 'production' and result['outcome'] != 'validated':
                        passed = False
    if not passed:
        raise SystemExit('At least one production request or validation failed.')


if __name__ == '__main__':
    main()
