"""Stable request identities; never persist raw prompts or client request keys."""
import hashlib
import json
import re


def request_identity(key, operation, payload, source=None):
    if key is None:
        return None
    if not re.fullmatch(r'[A-Za-z0-9._:-]{8,128}', key):
        raise ValueError('Idempotency-Key must be 8-128 ASCII letters, digits, dots, colons, underscores or hyphens.')
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    if source is not None:
        with source.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                fingerprint.update(chunk)
    return {'request_key': hashlib.sha256((operation + '\0' + key).encode('utf-8')).hexdigest(),
            'request_fingerprint': fingerprint.hexdigest()}
