#!/usr/bin/env python3
"""Prepare a GitHub-only transfer key; never export a plaintext private key."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re

CHARACTER_ID = 'kai'
REFERENCE_ID = 'tiktok-7706633304702848276'
ALGORITHM = 'RSA-OAEP-SHA256+AES-256-GCM'


class TransferStopped(Exception):
    def __init__(self, reason):
        self.reason = reason


def github_context():
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise TransferStopped('github_only')
    if os.environ.get('GITHUB_RUN_ATTEMPT', '1') != '1':
        raise TransferStopped('rerun_refused')
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if not re.fullmatch(r'[0-9]{1,30}', run_id):
        raise TransferStopped('github_run_id_invalid')
    secret = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not 8 <= len(secret) <= 512 or any(not 33 <= ord(char) <= 126 for char in secret):
        raise TransferStopped('openrouter_secret_missing_or_invalid')
    return run_id, secret.encode('ascii')


def prepare(output):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    run_id, password = github_context()
    output = Path(output)
    names = ('public_key.pem', 'encrypted_private_key.pem', 'key-metadata.json')
    if output.is_symlink() or any((output / name).exists() or (output / name).is_symlink() for name in names):
        raise TransferStopped('existing_transfer_key_not_overwritten')
    private = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    public = private.public_key()
    public_pem = public.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    public_der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    # No plaintext private-key serialization is performed, even temporarily.
    encrypted_private = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(password))
    metadata = dict(schema_version=1, key_run_id=run_id, character_id=CHARACTER_ID, reference_id=REFERENCE_ID,
                    public_key_sha256=hashlib.sha256(public_der).hexdigest(), algorithm=ALGORITHM)
    data = (public_pem, encrypted_private, (json.dumps(metadata, sort_keys=True, indent=2) + '\n').encode('utf-8'))
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    written = []
    try:
        for name, content in zip(names, data):
            path = output / name
            descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, 'wb') as target:
                target.write(content)
                target.flush()
                os.fsync(target.fileno())
            written.append(path)
    except Exception:
        for path in written:
            try:
                path.unlink()
            except OSError:
                pass
        raise TransferStopped('transfer_key_could_not_be_saved') from None
    return metadata


def main():
    try:
        prepare(Path(os.environ.get('REFERENCE_KEY_OUTPUT_DIR', 'reference-transfer-key')))
        print('Reference transfer key: prepared', flush=True)
        return 0
    except Exception as exc:
        reason = exc.reason if isinstance(exc, TransferStopped) else 'transfer_key_preparation_failed'
        print('Reference transfer key stopped: ' + reason, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
