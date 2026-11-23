#!/usr/bin/env python3
"""Archive trusted, completed workflow videos; never run generation or copy state."""
import argparse
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from fractions import Fraction
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import zipfile

WORKFLOWS = {
    '.github/workflows/video-generation.yml': 'seedance-result-',
    '.github/workflows/creative-generation.yml': 'openai-creative-',
}
HISTORICAL = {
    '43772946438': {'artifact_id': 14185685311, 'artifact': 'api-test-short-video-43772946438',
                    'title': 'API 视频生成测试', 'character': '测试小鹿',
                    'material_group': 'unknown', 'cast_type': 'unknown', 'kind': 'api_test'},
    '43790641550': {'artifact_id': 14192636090, 'artifact': 'kai-clay-generated-video',
                    'title': 'KAI · Bizzyboom 手势舞', 'character': 'KAI',
                    'material_group': 'male', 'cast_type': 'solo', 'kind': 'character'},
}
MAX_VIDEO = 90 * 1024 * 1024  # Keep below GitHub's 100 MiB per-file limit.
MAX_ZIP = 150 * 1024 * 1024
IDENT = re.compile(r'video-[a-f0-9]{32}')


class ArchiveStopped(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class GitHub:
    def __init__(self, repository, token):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository) or not token:
            raise ArchiveStopped('GitHub configuration missing.')
        self.repository, self.token = repository, token

    def read(self, suffix, archive=False):
        if not re.fullmatch(r'/actions/(?:runs/\d+(?:/artifacts\?per_page=100)?|artifacts/\d+/zip)', suffix):
            raise ArchiveStopped('Unsupported GitHub route.')
        request = Request('https://api.github.com/repos/' + self.repository + suffix,
                          headers={'Authorization': 'Bearer ' + self.token,
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28'})
        try:
            response = build_opener(NoRedirect).open(request, timeout=30)
        except HTTPError as exc:
            if not archive or exc.code != 302:
                raise ArchiveStopped('GitHub artifact request failed.') from None
            location = exc.headers.get('Location', '')
            target = urlsplit(location)
            if (target.scheme != 'https' or target.username or target.password
                    or target.port not in (None, 443) or target.fragment
                    or not (target.hostname or '').endswith(('.blob.core.windows.net', '.actions.githubusercontent.com', '.githubusercontent.com'))):
                raise ArchiveStopped('Unsupported GitHub artifact storage.') from None
            # Never forward the repository credential to signed artifact storage.
            response = build_opener(NoRedirect).open(Request(location), timeout=60)
        with response:
            raw = response.read((MAX_ZIP if archive else 2 * 1024 * 1024) + 1)
        if len(raw) > (MAX_ZIP if archive else 2 * 1024 * 1024):
            raise ArchiveStopped('GitHub artifact exceeds archive bounds.')
        return raw if archive else json.loads(raw)


def trusted_run(run, repository, historical=False):
    path = str(run.get('path', '')).split('@', 1)[0]
    if (historical and repository == 'yjr927/idore_tools' and str(run.get('id')) in HISTORICAL
            and run.get('status') == 'completed' and run.get('conclusion') in ('success', 'failure')
            and run.get('head_repository', {}).get('full_name') == repository):
        return HISTORICAL[str(run['id'])]['artifact']
    if historical:
        raise ArchiveStopped('Historical backfill is restricted to two approved runs.')
    if (run.get('status') != 'completed' or run.get('conclusion') not in ('success', 'failure', 'cancelled')
            or run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'main'
            or run.get('head_repository', {}).get('full_name') != repository
            or path not in WORKFLOWS):
        raise ArchiveStopped('Only completed trusted main generation runs can be archived.')
    return WORKFLOWS[path] + str(run['id'])


def final_videos(raw, historical=False):
    """Inspect ZIP members without extracting any path, image, source, or journal."""
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(m.file_size for m in members) > 250 * 1024 * 1024:
            raise ArchiveStopped('Artifact contents exceed bounds.')
        names = {}
        for member in members:
            path = PurePosixPath(member.filename)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.filename
                    or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1
                    or member.filename in names):
                raise ArchiveStopped('Unsafe artifact member.')
            names[member.filename] = member
        results = []
        summaries = ('generation-summary.json', 'creative-summary.json') + (('clay-generation-summary.json',) if historical else ())
        for name in summaries:
            member = names.get(name)
            if member is None:
                continue
            if member.file_size > 256 * 1024:
                raise ArchiveStopped('Summary exceeds bounds.')
            summary = json.loads(archive.read(member))
            if name == 'creative-summary.json':
                if summary.get('task') != 'full_video':
                    continue
                summary = summary.get('video', {})
            if summary.get('status') != 'completed' or not IDENT.fullmatch(str(summary.get('id', ''))):
                continue
            ident = summary['id']
            if name == 'clay-generation-summary.json' and summary.get('result_file') != ident + '.mp4':
                raise ArchiveStopped('Historical clay result filename mismatch.')
            candidates = [p for p in (ident + '.mp4', 'generated/' + ident + '.mp4') if p in names]
            if len(candidates) != 1:
                raise ArchiveStopped('Completed video has no unique matching media file.')
            video = names[candidates[0]]
            if not 0 < video.file_size <= MAX_VIDEO:
                raise ArchiveStopped('Generated video exceeds GitHub file limits.')
            content = archive.read(video)
            if len(content) < 24 or content[4:8] != b'ftyp':
                raise ArchiveStopped('Generated output is not MP4.')
            model = summary.get('model', '')
            if not isinstance(model, str) or not re.fullmatch(r'bytedance/seedance-[a-z0-9.\-]{1,80}', model):
                raise ArchiveStopped('Generated model metadata is invalid.')
            results.append((content, model))
        return results


def probe(path):
    result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height:format=duration,format_name', '-of', 'json', str(path)],
        check=True, capture_output=True, text=True, timeout=30)
    info = json.loads(result.stdout)
    stream, fmt = info['streams'][0], info['format']
    width, height, duration = stream['width'], stream['height'], float(fmt['duration'])
    if (type(width) is not int or type(height) is not int or not 64 <= width <= 4096
            or not 64 <= height <= 4096 or not math.isfinite(duration) or not 0 < duration <= 120
            or 'mp4' not in fmt['format_name']):
        raise ArchiveStopped('Generated video metadata exceeds archive bounds.')
    ratio = Fraction(width, height)
    return round(duration, 3), str(min(width, height)) + 'p', str(ratio.numerator) + ':' + str(ratio.denominator)


def merge(site, videos, run, repository, historical=False):
    site = Path(site)
    directory, manifest = site / 'generated', site / 'data' / 'creations.json'
    if directory.is_symlink() or manifest.is_symlink():
        raise ArchiveStopped('Archive paths must not be symbolic links.')
    directory.mkdir(parents=True, exist_ok=True)
    manifest.parent.mkdir(parents=True, exist_ok=True)
    document = json.loads(manifest.read_text()) if manifest.exists() else {'schema_version': 1, 'items': []}
    if document.get('schema_version') != 1 or not isinstance(document.get('items'), list) or len(document['items']) > 5000:
        raise ArchiveStopped('Existing creations manifest is invalid.')
    items = document['items']
    if not all(isinstance(item, dict) and re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))) for item in items):
        raise ArchiveStopped('Existing creations metadata is invalid.')
    added = 0
    for content, model in videos:
        digest = hashlib.sha256(content).hexdigest()
        if any(item['sha256'] == digest for item in items):
            continue
        ident = 'video-' + digest[:24]
        video, poster = directory / (ident + '.mp4'), directory / (ident + '.jpg')
        if video.exists() or poster.exists() or video.is_symlink() or poster.is_symlink():
            raise ArchiveStopped('Unindexed archive media already exists.')
        with tempfile.TemporaryDirectory(prefix='creation-archive-') as scratch:
            temporary = Path(scratch) / 'video.mp4'
            temporary.write_bytes(content)
            duration, resolution, ratio = probe(temporary)
            image = Path(scratch) / 'poster.jpg'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-ss', str(min(.5, duration / 2)),
                '-i', str(temporary), '-frames:v', '1', '-vf', 'scale=480:-2', '-q:v', '3', str(image)],
                check=True, capture_output=True, timeout=45)
            if not image.is_file() or image.stat().st_size > 2 * 1024 * 1024:
                raise ArchiveStopped('Generated poster is invalid.')
            shutil.copyfile(temporary, video)
            shutil.copyfile(image, poster)
        item = {'id': ident, 'title': '视频作品', 'character': '未标注角色',
            'material_group': 'unknown', 'cast_type': 'unknown',
            'created_at': run.get('archive_created_at') or run.get('updated_at') or now(), 'model': model, 'duration': duration,
            'resolution': resolution, 'aspect_ratio': ratio, 'video_path': 'generated/' + video.name,
            'poster_path': 'generated/' + poster.name,
            'workflow_url': 'https://github.com/' + repository + '/actions/runs/' + str(run['id']),
            'kind': 'character', 'sha256': digest,
            'timestamp_basis': 'workflow_artifact' if run.get('archive_created_at') else 'workflow_update'}
        if historical:
            metadata = HISTORICAL[str(run['id'])]
            item.update({key: metadata[key] for key in ('title', 'character', 'material_group', 'cast_type', 'kind')})
        items.append(item)
        added += 1
    if added:
        items.sort(key=lambda item: item.get('created_at', ''), reverse=True)
        document['updated_at'] = now()
        temporary = manifest.with_suffix('.tmp')
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(manifest)
    return added


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--site', default='site')
    parser.add_argument('--historical', action='store_true', help='Explicit backfill of the two approved historical runs only.')
    args = parser.parse_args()
    try:
        if not args.run_id.isdigit():
            raise ArchiveStopped('Invalid workflow run ID.')
        github = GitHub(os.environ.get('GITHUB_REPOSITORY', ''), os.environ.get('GH_TOKEN', ''))
        run = github.read('/actions/runs/' + args.run_id)
        if str(run.get('id')) != args.run_id:
            raise ArchiveStopped('Workflow ID mismatch.')
        expected = trusted_run(run, github.repository, args.historical)
        listing = github.read('/actions/runs/' + args.run_id + '/artifacts?per_page=100')
        if listing.get('total_count', 0) > 100:
            raise ArchiveStopped('Too many artifacts.')
        artifacts = [item for item in listing.get('artifacts', []) if item.get('name') == expected and not item.get('expired')]
        if len(artifacts) > 1:
            raise ArchiveStopped('Duplicate result artifact.')
        videos = []
        for artifact in artifacts:
            if args.historical and artifact.get('id') != HISTORICAL[args.run_id]['artifact_id']:
                raise ArchiveStopped('Historical artifact ID mismatch.')
            if not 0 < artifact.get('size_in_bytes', 0) <= MAX_ZIP or type(artifact.get('id')) is not int:
                raise ArchiveStopped('Artifact size or ID invalid.')
            videos.extend(final_videos(github.read('/actions/artifacts/' + str(artifact['id']) + '/zip', archive=True), args.historical))
            run['archive_created_at'] = artifact.get('created_at') or run.get('updated_at')
        if args.historical and (len(artifacts) != 1 or len(videos) != 1):
            raise ArchiveStopped('Historical completed result not found.')
        added = merge(args.site, videos, run, github.repository, args.historical)
        print('Archived completed videos: ' + str(added))
        return 0
    except Exception:
        # Never serialize upstream error bodies, storage redirects, or credentials.
        print('Video archive stopped; no generation was submitted.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
