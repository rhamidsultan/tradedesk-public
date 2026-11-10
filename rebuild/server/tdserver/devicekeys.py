"""Device-key sign-in: each iPhone/Mac holds a P-256 key in its Secure Enclave, unlocked by Face ID.

The server keeps only the public key. Sign-in is challenge-response: the server sends a random nonce,
the device signs `TD2 login v1|<challenge id>|<nonce>` (ECDSA P-256, SHA-256, DER signature, the format
CryptoKit's `signature.derRepresentation` produces), and the server verifies with the stored key.
Enrolment proves key possession the same way over `TD2 enroll v1|<invite code>`.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

LOGIN_PREFIX = b"TD2 login v1|"
ENROLL_PREFIX = b"TD2 enroll v1|"
ARM_PREFIX = b"TD2 arm v1|"


class BadKey(ValueError):
    pass


def load_public_key(x963: bytes) -> ec.EllipticCurvePublicKey:
    if len(x963) != 65 or x963[0] != 4:
        raise BadKey("public key must be an uncompressed P-256 point (65 bytes, X9.63)")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), x963)
    except ValueError as e:
        raise BadKey("not a valid P-256 point") from e


def key_id(x963: bytes) -> str:
    return hashlib.sha256(x963).hexdigest()


def login_message(challenge_id: str, nonce: bytes) -> bytes:
    return LOGIN_PREFIX + challenge_id.encode() + b"|" + base64.b64encode(nonce)


def arm_message(challenge_id: str, nonce: bytes, session_id: int, day: str) -> bytes:
    """Arming a live session for one day: signed with Face ID on the device that asks."""
    return ARM_PREFIX + f"{challenge_id}|".encode() + base64.b64encode(nonce) + f"|session {session_id}|{day}|LIVE".encode()


def enroll_message(invite_code: str) -> bytes:
    return ENROLL_PREFIX + invite_code.encode()


def verify(x963: bytes, message: bytes, der_signature: bytes) -> bool:
    try:
        load_public_key(x963).verify(der_signature, message, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, BadKey, ValueError):
        return False


def b64d(s: str) -> bytes:
    s = s.strip()
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4), altchars=b"-_" if ("-" in s or "_" in s) else None,
                                validate=False)
    except Exception as e:  # noqa: BLE001
        raise BadKey("bad base64") from e
