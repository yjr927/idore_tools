"""The two specific motion references selected for the November 23 New York run."""
from datetime import date, timedelta
import json
from pathlib import Path

from replication_service import _effective_reference

SCHEDULE = json.loads((Path(__file__).resolve().parents[1] / 'site/data/named-motion.json').read_text('utf-8'))
SCHEDULE_DATE = date.fromisoformat(SCHEDULE['date'])
DEFERRED_DATE = SCHEDULE_DATE - timedelta(days=1)
NAMED = {entry['slot']: entry for entry in SCHEDULE['references']}
NAMED_METHOD = 'named_reference_motion'
SELECTION = 'user_selected_reference'


def matches_named_film(film):
    entry = NAMED.get(film.get('sequence_slot'))
    return bool(entry and film.get('generation_method') == NAMED_METHOD and film.get('selection_basis') == SELECTION
                and film.get('trend_id') == entry['source_id'] and film.get('source_url') == entry['url'])


def named_source(slot, source):
    """Bind a single reference to its exact slot. Keep observed facts apart from casting choices."""
    entry = NAMED.get(slot)
    if (not entry or not isinstance(source, dict) or source.get('id') != entry['source_id']
            or source.get('url') != entry['url'] or source.get('kind') != 'hands'
            or source.get('checks', {}).get('fixed_camera') is not True or not source.get('motion_summary')):
        return None
    observed = dict(source)
    if source.get('selection_basis') == SELECTION:
        observed.update(material_group=source.get('source_material_group'), transformation=source.get('source_transformation'))
    group, cast, watched, _ = _effective_reference(observed)
    if not watched or cast != 'solo' or group not in ('male', 'female', 'unknown'):
        return None
    if group == 'unknown' and (entry['slot'] != '2026-11-23-bay' or '不限定男女角色用途' not in source['motion_summary']):
        return None
    if observed.get('transformation') not in ('none', 'unknown'):
        return None
    casting = group if group != 'unknown' else entry['casting_group']
    return dict(observed, source_material_group=group, source_transformation=observed['transformation'],
                material_group=casting, cast_type='solo', transformation='none', selection_basis=SELECTION,
                casting_basis='neutral_choreography_editorial_cast' if group == 'unknown' else 'observed_material_group',
                direction='Recreate the observed arm gesture in a single consistent costume.')
