"""Six-second blocking for two explicitly selected, previously watched solo dances."""
import json
import re
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from scripts.prepare_motion_source import NoRedirect, PreflightStopped, download_reference
from scripts.pipeline_state import PipelineError
from scripts.trend_quality import canonical_video
from video_service import VideoError, _media_url


# Each beat corresponds to the source's public, watched motion summary. These
# time ranges direct our new cut; the captures sampled longer originals.
BEATS = {
    'tiktok-7703265627972128021': (
        ('拳放脸侧', '双掌在侧边', '抬起一膝', '一臂横放胸前'),
        'Use a stable full-body composition that keeps both feet and hands visible. '
        '0–2s: bend the elbows and bring the fists beside the face. '
        '2–4s: open the palms; cross them at an angle toward one side while lifting one knee. '
        '4–6s: lower the foot and bring one forearm horizontally across the chest, the other fist beside the face.'),
    'tiktok-7709762212679683342': (
        ('双手平放在脸侧', '双臂在胸前交叉转动', '放下手臂'),
        'Use a steady waist-up composition with space around the hands. '
        '0–2s: turn the torso to the side and hold both flat hands beside the face. '
        '2–4s: return to face the camera and cross and rotate both forearms in front of the chest. '
        '4–6s: release the crossed arms, lower them and finish with a facial expression.'),
}


def gesture_prompt(source):
    instructions = BEATS.get(source.get('id'))
    if not instructions or any(phrase not in source['motion_summary'] for phrase in instructions[0]):
        raise PipelineError('motion_observation_missing', '原片记录需要包含这段舞蹈的三个关键动作。')
    return ('Animate the one fictional character in the supplied first frame into a six-second 9:16 dance. '
            'Keep their face, hair, outfit and accessories consistent. Use the first frame as the identity and look. '
            + instructions[1] + ' Link the poses with fluid shoulder, elbow and wrist motion. '
            'Maintain plausible anatomy, five fingers per hand, a stable camera, consistent lighting and one performer. '
            'No outfit changes, cuts, lettering, logos, subtitles or audio.')


class _PublicPlayer(HTMLParser):
    """Read playback media only from the named clip on TikTok's ordinary public page."""
    def __init__(self):
        super().__init__()
        self.capture = False
        self.player_json = []
        self.access_required = False

    def handle_starttag(self, tag, attrs):
        if dict(attrs).get('id') in ('captcha-verify-container', 'captcha_container'):
            self.access_required = True
        if tag == 'script':
            self.capture = dict(attrs).get('id') == '__UNIVERSAL_DATA_FOR_REHYDRATION__'

    def handle_endtag(self, tag):
        if tag == 'script':
            self.capture = False

    def handle_data(self, data):
        if self.capture:
            self.player_json.append(data)
        elif data.strip().casefold() in ('verify to continue', 'security verification', 'verify you are human'):
            self.access_required = True


def official_original(source, destination):
    """Check the permalink once. No redirects, session data or third-party resolvers."""
    url, handle, video_id = canonical_video(source['url'])
    if source['id'] != 'tiktok-' + video_id or source['id'] not in BEATS:
        raise PipelineError('source_mismatch', '指定舞蹈与原片地址不一致。')
    req = Request(url, headers={'User-Agent': 'idore-motion/1.0', 'Accept': 'text/html'})
    try:
        with build_opener(NoRedirect).open(req, timeout=15) as response:
            if response.geturl() != url or response.headers.get_content_type() != 'text/html':
                raise PreflightStopped('public_page_unavailable')
            data = response.read(3 * 1024 * 1024 + 1)
    except HTTPError as exc:
        exc.close()
        raise PreflightStopped('public_page_access_required' if exc.code in (401,403,429) else 'public_page_unavailable') from None
    except (TimeoutError, URLError, OSError):
        raise PreflightStopped('public_page_unavailable') from None
    if len(data) > 3 * 1024 * 1024:
        raise PreflightStopped('public_page_unavailable')
    parser = _PublicPlayer()
    try:
        parser.feed(data.decode('utf-8'))
        if parser.access_required:
            raise PreflightStopped('public_page_access_required')
        detail = json.loads(''.join(parser.player_json))['__DEFAULT_SCOPE__']['webapp.video-detail']
        if detail.get('statusCode', 0) != 0:
            raise PreflightStopped('public_page_access_required')
        clip = detail['itemInfo']['itemStruct']
        if str(clip['id']) != video_id or clip['author']['uniqueId'].casefold() != handle.casefold():
            raise PipelineError('source_mismatch', 'TikTok 返回的原片与指定作品不一致。')
        address = _media_url(clip['video']['playAddr'], 'video_url')
        if not re.fullmatch(r'v[0-9]+(?:-[a-z0-9]+)*\.(?:[a-z]{2}\.)?tiktok\.com|v[0-9]+[a-z]?\.tiktokcdn-us\.com',
                            urlsplit(address).hostname or ''):
            raise PreflightStopped('official_player_unavailable')
    except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, RecursionError, VideoError):
        raise PreflightStopped('official_player_unavailable') from None
    download_reference(address, destination)
