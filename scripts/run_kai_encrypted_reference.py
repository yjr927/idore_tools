#!/usr/bin/env python3
"""Decrypt the one approved KAI reference in GitHub memory, then invoke once.

The repository contains only a public-key-encrypted URL envelope. The private key
artifact is itself encrypted with the existing GitHub OpenRouter secret. No API
key, plaintext private key, AES key or resolved URL is written or printed here.
"""
from __future__ import annotations
import base64
import binascii
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import hmac
import io
import json
import math
import os
from pathlib import Path
import re
import sys
import uuid
from urllib.parse import parse_qs, unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.prepare_reference_transfer_key import (ALGORITHM, CHARACTER_ID, REFERENCE_ID,
                                                   TransferStopped, github_context)
from scripts import run_kai_motion_cloud
from video_service import _media_url

OAEP_LABEL = b'idore-kai-motion-v1'
MAX_ENVELOPE = 16 * 1024
MAX_PLAINTEXT = 8 * 1024
METADATA_KEYS = {'schema_version', 'key_run_id', 'character_id', 'reference_id', 'public_key_sha256', 'algorithm'}
ENVELOPE_KEYS = METADATA_KEYS | {'encrypted_key', 'iv', 'ciphertext', 'tag'}


def _read(path, maximum):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= maximum:
        raise TransferStopped('encrypted_reference_input_invalid')
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise TransferStopped('encrypted_reference_input_invalid')
    return raw


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique_pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))


def _base64(value, minimum, maximum):
    if not isinstance(value, str) or len(value) > ((maximum + 2) // 3) * 4:
        raise ValueError()
    raw = base64.b64decode(value, validate=True)
    if not minimum <= len(raw) <= maximum:
        raise ValueError()
    return raw


def _context(document, keys):
    if (not isinstance(document, dict) or set(document) != keys
            or type(document['schema_version']) is not int or document['schema_version'] != 1
            or document['algorithm'] != ALGORITHM or document['character_id'] != CHARACTER_ID
            or document['reference_id'] != REFERENCE_ID
            or not isinstance(document['key_run_id'], str) or not re.fullmatch(r'[0-9]{1,30}', document['key_run_id'])
            or not isinstance(document['public_key_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', document['public_key_sha256'])):
        raise ValueError()


def decrypt_reference(envelope_file, key_dir, password, expected_key_run_id=None):
    """Return only (URL, duration); callers must never serialize this result."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        key_dir = Path(key_dir)
        if key_dir.is_symlink() or not isinstance(password, bytes) or not password:
            raise ValueError()
        envelope = _json(_read(envelope_file, MAX_ENVELOPE))
        metadata = _json(_read(key_dir / 'key-metadata.json', 8192))
        _context(envelope, ENVELOPE_KEYS)
        _context(metadata, METADATA_KEYS)
        if (envelope['key_run_id'] != metadata['key_run_id'] or
                (expected_key_run_id is not None and metadata['key_run_id'] != expected_key_run_id)):
            raise ValueError()
        public = serialization.load_pem_public_key(_read(key_dir / 'public_key.pem', 65536))
        if not isinstance(public, rsa.RSAPublicKey) or public.key_size != 4096 or public.public_numbers().e != 65537:
            raise ValueError()
        der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        fingerprint = hashlib.sha256(der).hexdigest()
        if not (hmac.compare_digest(fingerprint, metadata['public_key_sha256']) and
                hmac.compare_digest(fingerprint, envelope['public_key_sha256'])):
            raise ValueError()
        encrypted_private = _read(key_dir / 'encrypted_private_key.pem', 65536)
        if not encrypted_private.startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'):
            raise ValueError()
        private = serialization.load_pem_private_key(encrypted_private, password=password)
        if not isinstance(private, rsa.RSAPrivateKey) or private.key_size != 4096:
            raise ValueError()
        private_der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if not hmac.compare_digest(hashlib.sha256(private_der).hexdigest(), fingerprint):
            raise ValueError()
        wrapped = _base64(envelope['encrypted_key'], 512, 512)
        aes_key = private.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                                      algorithm=hashes.SHA256(), label=OAEP_LABEL))
        if len(aes_key) != 32:
            raise ValueError()
        iv = _base64(envelope['iv'], 12, 12)
        ciphertext = _base64(envelope['ciphertext'], 2, MAX_PLAINTEXT)
        tag = _base64(envelope['tag'], 16, 16)
        aad = (OAEP_LABEL.decode('ascii') + ':' + CHARACTER_ID + ':' + REFERENCE_ID + ':' + metadata['key_run_id']).encode('ascii')
        clear = AESGCM(aes_key).decrypt(iv, ciphertext + tag, aad)
        if len(clear) > MAX_PLAINTEXT:
            raise ValueError()
        reference = _json(clear)
        if not isinstance(reference, dict) or set(reference) != {'url', 'duration'}:
            raise ValueError()
        url, duration = reference['url'], reference['duration']
        if (not isinstance(url, str) or not 1 <= len(url) <= 4096 or type(duration) not in (int, float)
                or not math.isfinite(duration) or not 2 <= duration <= 15.05):
            raise ValueError()
        _media_url(url, 'video_url')
        parsed = urlsplit(url)
        if (not re.fullmatch(r'v[0-9]+(?:-[a-z0-9]+)*\.(?:[a-z]{2}\.)?tiktok\.com', parsed.hostname or '')
                or not re.fullmatch(r'/video/tos/[A-Za-z0-9/_-]+/?', unquote(parsed.path))
                or parse_qs(parsed.query).get('mime_type') != ['video_mp4']):
            raise ValueError()
        return url, duration
    except Exception:
        raise TransferStopped('encrypted_reference_verification_failed') from None


def write_summary(output, summary):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'encrypted-reference-summary.json'
    temporary = output / ('.encrypted-reference-summary-' + uuid.uuid4().hex + '.tmp')
    try:
        descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as target:
            json.dump(summary, target, sort_keys=True)
            target.write('\n')
        temporary.replace(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def main():
    output = Path(os.environ.get('VIDEO_OUTPUT_DIR', 'generation-output'))
    summary = dict(schema_version=1, status='stopped', reference_decrypted=False, generation_runner_invoked=False,
                   character_id=CHARACTER_ID, reference_id=REFERENCE_ID)
    try:
        if os.environ.get('KAI_ALLOW_PAID') != 'true':
            raise TransferStopped('explicit_paid_authorization_required')
        _, password = github_context()
        expected = os.environ.get('REFERENCE_KEY_RUN_ID')
        if expected is not None and not re.fullmatch(r'[0-9]{1,30}', expected):
            raise TransferStopped('transfer_key_context_invalid')
        reference = decrypt_reference(
            os.environ.get('REFERENCE_ENCRYPTED_FILE', 'cloud-requests/kai-reference.encrypted.json'),
            os.environ.get('REFERENCE_KEY_DIR', 'reference-transfer-key'), password, expected)
        summary['reference_decrypted'] = True
        summary['generation_runner_invoked'] = True
        # The core performs its own guards, complete MP4 verification and single
        # provider submission. Never expose unexpected diagnostic strings here.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = run_kai_motion_cloud.main(resolved_reference=reference)
        summary['status'] = 'completed' if result == 0 else 'not_completed'
        write_summary(output, summary)
        print('Encrypted KAI reference run: ' + summary['status'], flush=True)
        return 0 if result == 0 else 1
    except Exception as exc:
        summary['reason'] = exc.reason if isinstance(exc, TransferStopped) else 'encrypted_reference_run_stopped'
        try:
            write_summary(output, summary)
        except OSError:
            pass
        print('Encrypted KAI reference run stopped: ' + summary['reason'], flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
