"""Envelope encryption, token hashing and signed links.

Every secret gets its own random data key (AES-256-GCM); the data key is stored encrypted by the master
key. A leaked database alone therefore reveals no broker key. The master key provider is an interface so
that a managed key service (AWS KMS) can replace the local key later without touching callers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from typing import Protocol

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_NONCE = 12


class KeyProvider(Protocol):
    def wrap(self, data_key: bytes) -> bytes: ...
    def unwrap(self, wrapped: bytes) -> bytes: ...


class LocalKeyProvider:
    """Master key held in the server's environment (phase 1). Swap for a KMS provider later."""

    def __init__(self, master_key: bytes):
        if len(master_key) != 32:
            raise ValueError("master key must be 32 bytes")
        self._aes = AESGCM(_derive(master_key, b"td2 wrap v1"))

    def wrap(self, data_key: bytes) -> bytes:
        nonce = os.urandom(_NONCE)
        return nonce + self._aes.encrypt(nonce, data_key, b"data-key")

    def unwrap(self, wrapped: bytes) -> bytes:
        return self._aes.decrypt(wrapped[:_NONCE], wrapped[_NONCE:], b"data-key")


def _derive(key: bytes, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(key)


class Vault:
    """Encrypts and decrypts small secrets. `context` binds a ciphertext to its use (e.g. 'kite:api_secret')."""

    def __init__(self, provider: KeyProvider):
        self.provider = provider

    def new_data_key(self) -> bytes:
        return self.provider.wrap(AESGCM.generate_key(bit_length=256))

    def seal(self, data_key_enc: bytes, plaintext: str, context: str) -> bytes:
        aes = AESGCM(self.provider.unwrap(data_key_enc))
        nonce = os.urandom(_NONCE)
        return nonce + aes.encrypt(nonce, plaintext.encode(), context.encode())

    def open(self, data_key_enc: bytes, sealed: bytes, context: str) -> str:
        aes = AESGCM(self.provider.unwrap(data_key_enc))
        return aes.decrypt(sealed[:_NONCE], sealed[_NONCE:], context.encode()).decode()


def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """For high-entropy tokens (32 random bytes): a plain hash is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


class LinkSigner:
    """HMAC-signed short links (acknowledge an alert, open the console). Format: payload.exp.sig (base64url)."""

    def __init__(self, master_key: bytes):
        self._key = _derive(master_key, b"td2 links v1")

    def sign(self, purpose: str, payload: str, exp_epoch: int) -> str:
        body = f"{purpose}|{payload}|{exp_epoch}".encode()
        sig = hmac.new(self._key, body, hashlib.sha256).digest()[:16]
        return ".".join(_b64(x) for x in (payload.encode(), str(exp_epoch).encode(), sig))

    def keyed_hash(self, purpose: str, value: str) -> str:
        """For short codes a person types (invite codes): a leaked database cannot be guessed offline."""
        return hmac.new(self._key, f"{purpose}|{value}".encode(), hashlib.sha256).hexdigest()

    def short_code(self, purpose: str, payload: str, length: int = 20) -> str:
        """Deterministic short code (letters and digits), e.g. for a Telegram /start parameter."""
        d = hmac.new(self._key, f"{purpose}|{payload}".encode(), hashlib.sha256).digest()
        return base64.b32encode(d).decode().rstrip("=")[:length]

    def verify(self, purpose: str, token: str, now_epoch: int) -> str | None:
        try:
            p, e, s = token.split(".")
            payload, exp = _unb64(p).decode(), int(_unb64(e).decode())
            sig = _unb64(s)
        except Exception:
            return None
        body = f"{purpose}|{payload}|{exp}".encode()
        good = hmac.new(self._key, body, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(sig, good) or now_epoch > exp:
            return None
        return payload


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
