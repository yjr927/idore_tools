"""Produce and stage one newly authorized attempt for each of two fixed commissions."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.named_motion import NAMED, named_source
from scripts.run_agent_motion import attach_plan, source_file, finish_cut

LEDGER = Path('work-results/nov23-video-replacements.json')
SITE = Path('site')
OUT = Path('nov23-replacement-films')
MODEL = 'bytedance/seedance-2.5'
# Fingerprints refer to the immutable failed attempt, whose row remains untouched.
SLOTS = {
    '2026-11-23-momoko': ('cc1f57cca9c625987b08ffcef87247698881409110fad2e1f5eb8ab4b58fd2f7',
        '299488d51a70b3f7e7fa053a1753883a00a6043a', 'db2b79d2122dc8d2553da134c2adbb49097d01c1',
        'role-3abb96e025889f47', '3abb96e025889f477fd976ab634e8fd79dee3c7bf70a92157d617bcb614cbf85'),
    '2026-11-23-bay': ('5bbd34af24f13ccbd67335c1ffaefaaa0fc0819e63076d6c8bc6969711848f00',
        '4e048a1b8cf42523bc7e0fbc4599c95fb3ec0e9a', 'edf5eeb28555fb9af12fa4acec359eb3b6a8b83f',
        'role-323c636618027cbd', '323c636618027cbd6d200dcb82eb823ba8d2c83a66a89a2d2f4b7b3eecd0318c'),
}
METADATA = ('title','character','character_id','material_group','cast_type','generation_method',
            'source_photo_ids','source_photo_sha256','source_roles','sequence_type','sequence_slot',
            'planner','planner_model','planner_provider','execution_host','trend_id','trend_verified',
            'source_title','source_url','source_observed_at','source_metrics','source_metric_display',
            'source_peers','peer_count','hook','caption','hashtags','shots','storyboard_author','copy_provider',
            'validation_host','recovery_method','selection_basis','source_material_group','source_transformation',
            'casting_basis','motion_reference_method')


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00','Z')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def git(*args):
    subprocess.run(['git', *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CheckpointError(Exception):
    pass


def persist(ledger):
    # A racing change to main makes the push fail closed. Never POST after a failed claim.
    write(LEDGER, ledger)
    try:
        git('add', '--', str(LEDGER))
        git('commit', '--only', '-m', 'Record a November 23 replacement checkpoint', '--', str(LEDGER))
        try:
            git('push', 'origin', 'HEAD:refs/heads/main')
        except subprocess.CalledProcessError:
            git('fetch','origin','refs/heads/main:refs/remotes/origin/main')
            git('diff','--quiet','HEAD^','origin/main','--',str(LEDGER),
                'site/data/pipeline.json','site/data/creations.json')
            git('rebase','origin/main')
            git('push','origin','HEAD:refs/heads/main')
    except subprocess.CalledProcessError:
        raise CheckpointError from None


def inputs(slot):
    revision, _, _, role, digest = SLOTS[slot]
    ref = NAMED[slot]
    target = ref['source_id']
    rows = read(SITE/'data/pipeline.json')['items']
    relevant = [r for r in rows if r.get('sequence_slot') == slot or r.get('trend_id') == target]
    if len(relevant) != 1:
        raise ValueError('source_already_claimed')
    row = relevant[0]
    if any(f.get('trend_id') == target or f.get('sequence_slot') == slot
           for f in read(SITE/'data/creations.json')['items']):
        raise ValueError('source_already_archived')
    if (row.get('id') != 'agent-'+slot or row.get('sequence_slot') != slot or row.get('trend_id') != target
            or row.get('revision') != revision or row.get('error_code') != 'submission_uncertain'
            or row.get('motion_binding') != [target, ref['url'],role,digest]
            or row.get('source_photo_ids') != [role] or row.get('model') != MODEL
            or row.get('submission_reserved') is not True or row.get('status') != 'needs_attention'
            or row.get('remote_id') or row.get('video_path') or row.get('sha256') or row.get('draft_publish_id')
            or row.get('motion_reference_method') != 'observed_description'):
        raise ValueError('prior_attempt_changed')
    discovery = read(SITE/'data/discovery.json')['videos']
    source = named_source(slot, next(s for s in discovery if s['id'] == target))
    photo, = [p for p in read(SITE/'data/characters.json')['items'] if p['id'] == role]
    if (not source or photo.get('sha256') != digest or not source_file(SITE, photo) or not photo.get('enabled')
            or photo.get('auto_match_eligible') is not True or photo.get('auto_match_blockers')
            or photo.get('cast_type') != 'solo' or photo.get('asset_kind') not in ('portrait','single_character')):
        raise ValueError('source_or_role_changed')
    # Working copy here. The old primary pipeline row is never rewritten by this workflow.
    row = dict(row)
    attach_plan(row, row['agent_plan'], [source], [photo])
    return row, source, photo


def reserve(ledger, slot, row, run_id):
    if slot in ledger:
        return False
    if datetime.now(timezone.utc) >= datetime(2026,11,24,5,tzinfo=timezone.utc):
        raise ValueError('new_submission_window_closed')
    revision, reservation, outcome, _, _ = SLOTS[slot]
    ledger[slot] = dict(attempt=2, status='reserved', model=MODEL, run_id=run_id,
                        reserved_at=now(), prior_reservation_commit=reservation, prior_outcome_commit=outcome,
                        prior_revision=revision, prior_status='submission_uncertain', binding=row['motion_binding'])
    persist(ledger)
    return True


def save_receipt(slot, ledger, fields):
    # The receipt module emits only bounded HTTP codes, fixed labels, official job identifiers and counts.
    item = ledger[slot]
    if item.get('remote_id') and fields.get('remote_id', item['remote_id']) != item['remote_id']:
        raise ValueError('conflicting_remote_id')
    if any(item.get(k) != v for k,v in fields.items()):
        ident = fields.get('remote_id')
        if isinstance(ident, str) and re.fullmatch(r'gen-vid-[0-9]{10}-[0-9A-Za-z]{20}', ident):
            print('accepted_video', slot, ident, flush=True)
        item.update(fields, checked_at=now())
        persist(ledger)


def submit(slot, ledger, run_id):
    from scripts.nov23_video_receipts import submit_named, NamedVideoError
    row, source, photo = inputs(slot)
    if not reserve(ledger, slot, row, run_id):
        return
    try:
        with tempfile.TemporaryDirectory() as scratch:
            remote = submit_named(slot,row,source,photo,Path(scratch),lambda fields: save_receipt(slot,ledger,fields))
    except NamedVideoError as exc:
        save_receipt(slot,ledger,{'status':'needs_attention','error_code':exc.code})
        return
    save_receipt(slot,ledger,{'remote_id':remote,'status':'accepted'})


def collect(slot, ledger):
    from scripts.nov23_video_receipts import download_named, NamedVideoError
    row, _, _ = inputs(slot)
    receipt = ledger.get(slot, {})
    remote = receipt.get('remote_id')
    if not remote:
        return False
    try:
        with tempfile.TemporaryDirectory() as scratch:
            work = Path(scratch)
            raw = download_named(slot, remote, work)
            cut = work/'final.mp4'
            finish_cut(raw, row['shots'], cut)
            data = cut.read_bytes()
    except NamedVideoError as exc:
        save_receipt(slot,ledger,{'status':'needs_attention','error_code':exc.code})
        return False
    sha = hashlib.sha256(data).hexdigest()
    folder = OUT/slot
    metadata = {field: row[field] for field in METADATA if field in row}
    stamp = receipt.get('generated_at') or now()
    metadata.update(model=MODEL, remote_id=remote, sha256=sha, bytes=len(data), generated_at=stamp,
                    duration=6, resolution='720p', aspect_ratio='9:16', attempt=2)
    folder.mkdir(parents=True, exist_ok=True)
    (folder/'video.mp4').write_bytes(data)
    write(folder/'metadata.json', metadata)
    save_receipt(slot,ledger,{'status':'cut_verified','sha256':sha,'bytes':len(data),'generated_at':stamp})
    return True


def main():
    if (os.environ.get('GITHUB_REPOSITORY') != 'yjr927/idore_tools' or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_ACTIONS') != 'true' or not re.fullmatch(r'[0-9]+',os.environ.get('GITHUB_RUN_ID',''))
            or sys.argv[1:] not in (['submit'], ['collect'])):
        raise SystemExit('A trusted named production run is required.')
    git('config','user.name','idore production bot')
    git('config','user.email','41898282+github-actions[bot]@users.noreply.github.com')
    ledger = read(LEDGER) if LEDGER.exists() else {}
    if not isinstance(ledger, dict) or set(ledger) - SLOTS.keys():
        raise SystemExit('Unexpected replacement ledger')
    success = 0
    for slot in SLOTS:
        try:
            if sys.argv[1] == 'submit':
                submit(slot,ledger,os.environ['GITHUB_RUN_ID'])
                success += bool(ledger.get(slot,{}).get('remote_id'))
            else:
                success += bool(collect(slot,ledger))
        except CheckpointError:
            raise SystemExit('A required durable checkpoint failed.')
        except (KeyError, StopIteration, ValueError, OSError, subprocess.CalledProcessError):
            print(slot, 'could_not_verify_inputs_or_media')
        print(slot, ledger.get(slot,{}).get('status','not_started'))
    if sys.argv[1] == 'submit':
        if not success:
            raise SystemExit('Neither commission has a recoverable video ID.')
    elif success != len(SLOTS):
        raise SystemExit('One or both commissions have no verified final cut.')


if __name__ == '__main__':
    main()
