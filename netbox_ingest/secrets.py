"""
Source secrets (passwords, tokens, API keys, client secrets).

Stored encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from the
"cryptography" package NetBox already installs). The key is derived from
NetBox's SECRET_KEY, so a database dump alone does not reveal secrets. If
SECRET_KEY changes, stored secrets can no longer be read and must be entered
again; runs say so. A secret can instead come from an environment variable
of the NetBox worker, and then nothing is stored at all.
"""
import base64
import hashlib
import os

from django.conf import settings


class SecretError(Exception):
    pass


def _fernet():
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(b'netbox-ingest:' + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext):
    if not plaintext:
        return ''
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext):
    from cryptography.fernet import InvalidToken

    if not ciphertext:
        return ''
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise SecretError(
            'The stored secret cannot be read (NetBox\'s SECRET_KEY changed?). Enter it again on the source.'
        ) from exc


def get_secret(source):
    """The source's secret in plain text, '' if it has none."""
    if source.secret_env:
        value = os.environ.get(source.secret_env)
        if value is None:
            raise SecretError(f'The environment variable {source.secret_env} is not set for the NetBox worker.')
        return value
    return decrypt(source.secret_ciphertext)
