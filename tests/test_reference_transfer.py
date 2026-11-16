import base64
from contextlib import redirect_stdout, redirect_stderr
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import prepare_reference_transfer_key as prep
from scripts import run_kai_encrypted_reference as bridge
try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO = True
except ImportError:
    CRYPTO = False

SECRET = 'test-only-openrouter-password-not-a-real-api-key'
RUN = '1122334455'
# Nonexistent fixture path and marker, not a signed URL obtained from TikTok.
URL = ('https://v16-webapp-prime.us.tiktok.com/video/tos/test/nonexistent-fixture/'
       '?mime_type=video_mp4&fixture=DO_NOT_WRITE_REFERENCE')


@unittest.skipUnless(CRYPTO, 'Optional encrypted transfer requires cryptography')
class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master = tempfile.TemporaryDirectory()
        cls.path = Path(cls.master.name) / 'key'
        with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': RUN,
                'GITHUB_RUN_ATTEMPT': '1', 'OPENROUTER_API_KEY': '  ' + SECRET + '  '}, clear=True):
            cls.meta = prep.prepare(cls.path)
        cls.public = serialization.load_pem_public_key((cls.path / 'public_key.pem').read_bytes())

    @classmethod
    def tearDownClass(cls):
        cls.master.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.keys = self.dir / 'key'
        shutil.copytree(self.path, self.keys)
        self.payload = self.dir / 'encrypted.json'
        self.output = self.dir / 'output'
        self.envelope = self.encrypt()
        self.save()

    def tearDown(self):
        self.tmp.cleanup()

    def encrypt(self, clear=None, aad=None, label=None):
        key, iv = AESGCM.generate_key(bit_length=256), os.urandom(12)
        clear = {'url': URL, 'duration': 6.22} if clear is None else clear
        aad = aad if aad is not None else ('idore-kai-motion-v1:kai:' + prep.REFERENCE_ID + ':' + RUN).encode()
        encrypted = AESGCM(key).encrypt(iv, json.dumps(clear).encode(), aad)
        wrapped = self.public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
            algorithm=hashes.SHA256(), label=bridge.OAEP_LABEL if label is None else label))
        b64 = lambda value: base64.b64encode(value).decode()
        return dict(self.meta, encrypted_key=b64(wrapped), iv=b64(iv), ciphertext=b64(encrypted[:-16]), tag=b64(encrypted[-16:]))

    def save(self, envelope=None):
        self.payload.write_text(json.dumps(self.envelope if envelope is None else envelope))

    def decrypt(self, password=None, expected=RUN):
        return bridge.decrypt_reference(self.payload, self.keys, SECRET.encode() if password is None else password, expected)

    def execute(self, env=None, runner=None):
        environment = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': '99887766', 'GITHUB_RUN_ATTEMPT': '1',
            'OPENROUTER_API_KEY': '  ' + SECRET + '  ', 'KAI_ALLOW_PAID': 'true', 'REFERENCE_KEY_DIR': str(self.keys),
            'REFERENCE_ENCRYPTED_FILE': str(self.payload), 'REFERENCE_KEY_RUN_ID': RUN, 'VIDEO_OUTPUT_DIR': str(self.output)}
        environment.update(env or {})
        runner = runner or Mock(return_value=0)
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, environment, clear=True), patch.object(bridge.run_kai_motion_cloud, 'main', runner), redirect_stdout(out), redirect_stderr(err):
            status = bridge.main()
        summary = json.loads((self.output / 'encrypted-reference-summary.json').read_text())
        for private in (URL, SECRET, 'DO_NOT_WRITE_REFERENCE'):
            self.assertNotIn(private, out.getvalue() + err.getvalue() + json.dumps(summary))
        return status, summary, runner

    def test_keyprep_only_encrypted_pkcs8_and_spki_hash_metadata(self):
        self.assertEqual({p.name for p in self.keys.iterdir()}, {'public_key.pem', 'encrypted_private_key.pem', 'key-metadata.json'})
        encrypted = (self.keys / 'encrypted_private_key.pem').read_bytes()
        self.assertTrue(encrypted.startswith(b'-----BEGIN ENCRYPTED PRIVATE KEY-----'))
        self.assertNotIn(SECRET.encode(), encrypted)
        self.assertNotIn(b'-----BEGIN PRIVATE KEY-----', encrypted)
        with self.assertRaises((ValueError, TypeError)):
            serialization.load_pem_private_key(encrypted, password=None)
        private = serialization.load_pem_private_key(encrypted, password=SECRET.encode())
        self.assertEqual(private.key_size, 4096)
        der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(hashlib.sha256(der).hexdigest(), self.meta['public_key_sha256'])
        self.assertEqual(self.meta['key_run_id'], RUN)
        self.assertEqual(self.meta['reference_id'], prep.REFERENCE_ID)
        self.assertEqual((self.keys / 'encrypted_private_key.pem').stat().st_mode & 0o777, 0o600)

    def test_roundtrip_and_plaintext_never_written(self):
        self.assertEqual(self.decrypt(), (URL, 6.22))
        for path in self.dir.rglob('*'):
            if path.is_file():
                self.assertNotIn(URL.encode(), path.read_bytes())
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    @unittest.skipUnless(shutil.which('node'), 'Node required for native crypto interoperability')
    def test_native_node_encryption_decrypts_with_pyca(self):
        source = r'''
const crypto=require('node:crypto');let input='';process.stdin.setEncoding('utf8');
process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>{
const p=JSON.parse(input),key=crypto.randomBytes(32),iv=crypto.randomBytes(12);
const label=Buffer.from('idore-kai-motion-v1');
const aad=Buffer.from('idore-kai-motion-v1:kai:tiktok-7706633304702848276:'+p.metadata.key_run_id);
const cipher=crypto.createCipheriv('aes-256-gcm',key,iv);cipher.setAAD(aad);
const ciphertext=Buffer.concat([cipher.update(JSON.stringify(p.reference),'utf8'),cipher.final()]);
const wrapped=crypto.publicEncrypt({key:p.public_pem,oaepHash:'sha256',oaepLabel:label,padding:crypto.constants.RSA_PKCS1_OAEP_PADDING},key);
process.stdout.write(JSON.stringify({...p.metadata,encrypted_key:wrapped.toString('base64'),iv:iv.toString('base64'),ciphertext:ciphertext.toString('base64'),tag:cipher.getAuthTag().toString('base64')}));});
'''
        data = dict(metadata=self.meta, public_pem=(self.keys / 'public_key.pem').read_text(),
                    reference={'url': URL, 'duration': 6.22})
        result = subprocess.run(['node', '-e', source], input=json.dumps(data), text=True, capture_output=True, check=True)
        self.assertNotIn(URL, result.stdout + result.stderr)
        self.payload.write_text(result.stdout)
        self.assertEqual(self.decrypt(), (URL, 6.22))

    def test_wrong_secret_stops_before_generation(self):
        with self.assertRaises(prep.TransferStopped): self.decrypt(password=b'wrong-password')
        status, summary, runner = self.execute({'OPENROUTER_API_KEY': 'wrong-password'})
        self.assertEqual(status, 1)
        self.assertFalse(summary['reference_decrypted'])
        runner.assert_not_called()

    def test_ciphertext_tag_nonce_and_wrapped_key_tampering(self):
        for field in ('ciphertext', 'tag', 'iv', 'encrypted_key'):
            changed = copy.deepcopy(self.envelope)
            raw = bytearray(base64.b64decode(changed[field])); raw[0] ^= 1
            changed[field] = base64.b64encode(raw).decode(); self.save(changed)
            with self.subTest(field=field), self.assertRaises(prep.TransferStopped): self.decrypt()
        for changes in ({'label': b'wrong-label'}, {'aad': b'wrong-context'}):
            self.save(self.encrypt(**changes))
            with self.assertRaises(prep.TransferStopped): self.decrypt()

    def test_algorithm_character_video_run_and_fingerprint_binding(self):
        for field, value in (('algorithm', 'other'), ('character_id', 'other'), ('reference_id', 'tiktok-7700000000000000000'),
                             ('key_run_id', '999'), ('public_key_sha256', '0' * 64), ('schema_version', True)):
            self.save(dict(self.envelope, **{field: value}))
            with self.subTest(field=field), self.assertRaises(prep.TransferStopped): self.decrypt()
        self.save()
        with self.assertRaises(prep.TransferStopped): self.decrypt(expected='777')
        (self.keys / 'key-metadata.json').write_text(json.dumps(dict(self.meta, key_run_id='888')))
        self.save(dict(self.envelope, key_run_id='888'))
        with self.assertRaises(prep.TransferStopped): self.decrypt(expected='888')

    def test_public_and_private_fingerprints_must_match(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=4096).public_key()
        der = other.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        digest = hashlib.sha256(der).hexdigest()
        (self.keys / 'public_key.pem').write_bytes(other.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        (self.keys / 'key-metadata.json').write_text(json.dumps(dict(self.meta, public_key_sha256=digest)))
        self.save(dict(self.envelope, public_key_sha256=digest))
        with self.assertRaises(prep.TransferStopped): self.decrypt()

    def test_plaintext_format_duration_url_scope_and_envelope_limits(self):
        for clear in ({'url': URL, 'duration': True}, {'url': URL, 'duration': 16}, {'url': URL, 'duration': 6.22, 'extra': SECRET},
                      {'url': 'https://media.example/clip.mp4', 'duration': 6.22},
                      {'url': 'https://www.tiktok.com/@bizzyboom/video/7706633304702848276', 'duration': 6.22},
                      {'url': URL + 'x' * 4096, 'duration': 6.22}):
            self.save(self.encrypt(clear=clear))
            with self.assertRaises(prep.TransferStopped): self.decrypt()
        for field, value in (('iv', 'c2hvcnQ='), ('tag', 'c2hvcnQ='), ('encrypted_key', '@invalid')):
            self.save(dict(self.envelope, **{field: value}))
            with self.assertRaises(prep.TransferStopped): self.decrypt()
        self.payload.write_bytes(b' ' * (bridge.MAX_ENVELOPE + 1))
        with self.assertRaises(prep.TransferStopped): self.decrypt()

    def test_main_one_inprocess_call_no_url_in_environment_or_logs(self):
        def run(**kwargs):
            self.assertEqual(kwargs, {'resolved_reference': (URL, 6.22)})
            self.assertNotIn(URL, json.dumps(dict(os.environ)))
            print(URL); print(SECRET, file=__import__('sys').stderr)
            return 0
        status, summary, runner = self.execute(runner=Mock(side_effect=run))
        self.assertEqual(status, 0)
        self.assertTrue(summary['generation_runner_invoked'])
        runner.assert_called_once()
        self.assertEqual({p.name for p in self.output.iterdir()}, {'encrypted-reference-summary.json'})

    def test_authorization_rerun_github_and_secret_guards(self):
        for env in ({'GITHUB_ACTIONS': 'false'}, {'GITHUB_RUN_ATTEMPT': '2'}, {'KAI_ALLOW_PAID': 'false'}, {'OPENROUTER_API_KEY': ''}):
            status, summary, runner = self.execute(env)
            self.assertEqual(status, 1)
            self.assertFalse(summary['generation_runner_invoked'])
            runner.assert_not_called()

    def test_unexpected_core_exception_is_sanitized(self):
        def run(**kwargs):
            print(URL); print(SECRET, file=__import__('sys').stderr)
            raise RuntimeError(URL + SECRET)
        status, summary, runner = self.execute(runner=Mock(side_effect=run))
        self.assertEqual(status, 1)
        self.assertEqual(summary['reason'], 'encrypted_reference_run_stopped')
        runner.assert_called_once()

    def test_keyprep_guards_and_existing_key_never_overwritten(self):
        before = (self.keys / 'encrypted_private_key.pem').read_bytes()
        base = {'GITHUB_ACTIONS': 'true', 'GITHUB_RUN_ID': RUN, 'GITHUB_RUN_ATTEMPT': '1',
            'OPENROUTER_API_KEY': SECRET, 'REFERENCE_KEY_OUTPUT_DIR': str(self.keys)}
        for changes in ({}, {'GITHUB_ACTIONS': 'false'}, {'GITHUB_RUN_ATTEMPT': '2'}, {'OPENROUTER_API_KEY': ''}):
            out = io.StringIO()
            with patch.dict(os.environ, dict(base, **changes), clear=True), redirect_stdout(out):
                self.assertEqual(prep.main(), 1)
            self.assertNotIn(SECRET, out.getvalue())
            self.assertEqual((self.keys / 'encrypted_private_key.pem').read_bytes(), before)

    def test_symlinks_and_duplicate_json_keys_rejected(self):
        public = self.keys / 'public_key.pem'; other = self.dir / 'other.pem'; other.write_bytes(public.read_bytes())
        public.unlink(); public.symlink_to(other)
        with self.assertRaises(prep.TransferStopped): self.decrypt()
        public.unlink(); public.write_bytes(other.read_bytes())
        raw = self.payload.read_text(); self.payload.write_text('{"schema_version":1,' + raw[1:])
        with self.assertRaises(prep.TransferStopped): self.decrypt()
