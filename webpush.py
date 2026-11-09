"""Web Push message encryption (RFC 8291, aes128gcm content coding RFC 8188).

Used for Android registrations whose UnifiedPush connector handed over
``webpush`` keys (``p256dh`` and ``auth``): the body POSTed to the endpoint
is then encrypted to the phone, so the push server sees nothing but its
size. Some push servers (Mozilla autopush, and so Sunup) refuse bodies
without it. No VAPID: the phone registers without a server key.

One record, record size 4096, no padding beyond the delimiter: the header
is 86 bytes and the tag 16, so a body of at most 4096 bytes carries at most
3993 bytes of plaintext.

Needs ``cryptography``, which Hermes already pins as a core dependency (the
end-to-end sealing in e2e.py uses it too).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
from typing import Any

RECORD_SIZE = 4096
HEADER_BYTES = 16 + 4 + 1 + 65
TAG_BYTES = 16
# The plaintext that fits one 4096-byte body.
MAX_PLAINTEXT = RECORD_SIZE - HEADER_BYTES - TAG_BYTES - 1


class WebPushError(Exception):
    pass


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def encrypt(plaintext: bytes, p256dh: bytes, auth: bytes, *, salt: bytes | None = None,
            private_key: Any = None) -> bytes:
    """The aes128gcm body for ``plaintext``. ``salt`` and ``private_key``
    (the sender's ephemeral P-256 key) are random unless given (tests)."""
    try:
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    except Exception as error:  # pragma: no cover - cryptography ships with Hermes
        raise WebPushError("the cryptography package is not available") from error
    if len(plaintext) > MAX_PLAINTEXT:
        raise WebPushError("the message does not fit one Web Push record")
    if len(auth) != 16 or len(p256dh) != 65 or p256dh[0] != 0x04:
        raise WebPushError("malformed Web Push keys")
    try:
        ua_public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    except ValueError as error:
        raise WebPushError("p256dh is not a P-256 point") from error
    private_key = private_key if private_key is not None else ec.generate_private_key(ec.SECP256R1())
    as_public = private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    salt = salt if salt is not None else os.urandom(16)
    if len(salt) != 16:
        raise WebPushError("the salt must be 16 bytes")

    ecdh_secret = private_key.exchange(ec.ECDH(), ua_public)
    # RFC 8291 section 3.4: the IKM is bound to both public keys and the
    # phone's auth secret.
    prk_key = _hmac(auth, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + p256dh + as_public + b"\x01")
    # RFC 8188 section 2.2/2.3.
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    # The last (only) record: plaintext, then the 0x02 delimiter.
    ciphertext = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    header = salt + struct.pack("!I", RECORD_SIZE) + bytes([len(as_public)]) + as_public
    return header + ciphertext


def decrypt(body: bytes, ua_private_key: Any, auth: bytes) -> bytes:
    """The receiving side (the phone's connector does this); for tests."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    salt, idlen = body[:16], body[20]
    as_public = body[21:21 + idlen]
    ciphertext = body[21 + idlen:]
    ua_public = ua_private_key.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    ecdh_secret = ua_private_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    prk_key = _hmac(auth, ecdh_secret)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public + as_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    record = AESGCM(cek).decrypt(nonce, ciphertext, None)
    if not record.rstrip(b"\x00").endswith(b"\x02"):
        raise WebPushError("not a last record")
    return record.rstrip(b"\x00")[:-1]
