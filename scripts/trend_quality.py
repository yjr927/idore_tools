"""Check that a single observed TikTok video clears both audience thresholds."""
import re
from decimal import Decimal
from urllib.parse import urlsplit

MIN_VIEWS = 1_000_000
MIN_LIKES = 100_000
COUNT = re.compile(r'(?P<number>(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)\s*(?P<unit>[KMB万亿]?)(?P<plus>\+?)', re.I)
MULTIPLIERS = {'': 1, 'K': 1000, 'M': 1000000, 'B': 1000000000, '万': 10000, '亿': 100000000}


class PublicPageError(Exception):
    """Expected invalid public-page input."""


def canonical_video(value):
    if not isinstance(value, str):
        raise PublicPageError("Expected a complete TikTok video URL.")
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        raise PublicPageError("Invalid video URL.") from None
    if (parsed.scheme != "https" or parsed.hostname not in ("www.tiktok.com", "tiktok.com")
            or parsed.username or parsed.password or port not in (None, 443)):
        raise PublicPageError("Expected an HTTPS TikTok video URL.")
    match = re.fullmatch(r"/@([A-Za-z0-9_.-]{1,100})/video/([0-9]{10,25})/?", parsed.path)
    if not match:
        raise PublicPageError("Only individual TikTok video links are accepted.")
    author, ident = match.groups()
    return f"https://www.tiktok.com/@{author}/video/{ident}", author, ident


def observed_count_reaches(label, count, minimum):
    if type(count) not in (int, float) or not minimum <= count <= 10**15:
        return False
    if not isinstance(label, str) or len(label) > 40:
        return False
    match = COUNT.fullmatch(label.strip())
    if not match:
        return False
    number, unit, plus = match.groups()
    if not unit and '.' in number:
        return False
    scale = MULTIPLIERS[unit.upper()]
    shown = Decimal(number.replace(',', '')) * scale
    if shown != count:
        return False
    if not unit or plus:  # Precise integer, or the page explicitly shows a floor.
        return True
    decimals = len(number.partition('.')[2])
    rounding_margin = Decimal(scale).scaleb(-decimals) / 2
    return shown - rounding_margin >= minimum


def verified_peers(row):
    if not isinstance(row, dict) or not isinstance(row.get('related_videos'), list):
        return []
    try:
        _, author, original = canonical_video(row.get('url'))
    except PublicPageError:
        return []
    authors, ids, peers = {author.lower()}, {original}, []
    for observation in row['related_videos']:
        if not isinstance(observation, dict) or observation.get('original_watched') is not True or observation.get('same_pattern') is not True:
            continue
        note = observation.get('note')
        if not isinstance(note, str) or not note.strip():
            continue
        try:
            url, other, ident = canonical_video(observation.get('url'))
        except PublicPageError:
            continue
        if other.lower() in authors or ident in ids:
            continue
        authors.add(other.lower())
        ids.add(ident)
        peers.append({'url': url, 'creator': other, 'note': note.strip()})
    return peers


def eligible_trend(row):
    if not isinstance(row, dict) or row.get('original_watched') is not True or not str(row.get('motion_summary') or '').strip():
        return False
    metrics, labels = row.get('metrics'), row.get('metric_display')
    return (isinstance(metrics, dict) and isinstance(labels, dict)
            and observed_count_reaches(labels.get('views'), metrics.get('views'), MIN_VIEWS)
            and observed_count_reaches(labels.get('likes'), metrics.get('likes'), MIN_LIKES)
            and len(verified_peers(row)) >= 2)
