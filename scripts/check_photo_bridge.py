"""Exercise the deployed private bridge with a public one-pixel fixture and no inference."""
import base64
import hashlib
import json
import os
import sys
import time

from run_photo_generation import PrivateJob, WORKER, TransportError, dump, request


def check():
    if (os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'
            or os.environ.get('GITHUB_REPOSITORY') != 'yjr927/idore_tools'):
        raise RuntimeError('The remote connection check requires a trusted main push.')
    run_id = os.environ['GITHUB_RUN_ID']
    if not run_id.isdigit():
        raise ValueError('Invalid run ID')
    secret = hashlib.sha256(('idore-photo-smoke:' + run_id).encode()).hexdigest()
    job_id = hashlib.sha256(secret.encode()).hexdigest()
    job = PrivateJob(job_id)
    url = WORKER + job_id + '/'
    # The Cloudflare deployment and Actions push build start independently.
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        try:
            init = job.call('init')
            if not init.lstrip().startswith(b'<'):
                break
        except TransportError as error:
            if error.status not in (404, 405, 502, 503, 504, None):
                raise
        time.sleep(8)
    else:
        raise RuntimeError('Updated Worker was unavailable during the deployment window.')
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGOwLZgJAAI0AUco0HPZAAAAAElFTkSuQmCC')
    payload = {'prompt': 'Private bridge connection test', 'images': [
        {'mime': 'image/png', 'data': base64.b64encode(png).decode()}]}
    if json.loads(request(url + 'status', secret)).get('status') != 'waiting':
        raise RuntimeError('Unexpected initial state.')
    request(url + 'input', secret, 'POST', dump(payload), 'application/json')
    if json.loads(job.call('input', 'GET', None)) != payload:
        raise RuntimeError('The input round trip failed.')
    job.call('claim')
    try:
        job.call('claim')
    except TransportError as error:
        if error.status != 409:
            raise
    else:
        raise RuntimeError('A second generation claim was accepted.')
    job.call('result', 'POST', png, 'image/png')
    if json.loads(request(url + 'status', secret)).get('status') != 'ready':
        raise RuntimeError('The test result was not marked ready.')
    if request(url + 'result', secret) != png:
        raise RuntimeError('The result round trip failed.')
    print('The deployed photo bridge returned the fixture and rejected duplicate generation.')


if __name__ == '__main__':
    try:
        check()
    except TransportError as failure:
        sys.exit('Photo bridge HTTP request failed: ' + str(failure.status or 'unavailable'))
    except Exception as failure:
        sys.exit('Photo bridge verification failed: ' + type(failure).__name__)
