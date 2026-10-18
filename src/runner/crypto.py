# -*- coding: utf-8 -*-
"""Encryption for the public repo (SPEC hard rule 5, decisions Q5/Q33/Q34).

Two schemes:
  - Private state (professor emails, Message-IDs, the resume): AES-256-GCM with the 32-byte
    key in the DATA_KEY secret (base64). File format: b"CM1" + 12-byte nonce + ciphertext.
  - Dashboard data: AES-256-GCM with a key derived from DASHBOARD_PASSPHRASE by
    PBKDF2-HMAC-SHA256, so the static page can decrypt it in the browser with WebCrypto.
"""
import base64
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

MAGIC = b"CM1"
PBKDF2_ITERATIONS = 310_000  # must match site/index.html


def load_key(b64: str = None) -> bytes:
    """The DATA_KEY secret as raw bytes; fails loudly if missing or the wrong length."""
    raw = base64.b64decode((b64 or os.environ.get("DATA_KEY", "")).strip())
    if len(raw) != 32:
        raise ValueError("DATA_KEY must be 32 bytes, base64-encoded")
    return raw


def encrypt(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(key).encrypt(nonce, data, MAGIC)


def decrypt(blob: bytes, key: bytes) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not a cold-mailer encrypted file")
    return AESGCM(key).decrypt(blob[3:15], blob[15:], MAGIC)


def encrypt_for_browser(obj, passphrase: str) -> dict:
    """JSON-serialise `obj` and encrypt it for site/index.html (all fields base64)."""
    salt, iv = os.urandom(16), os.urandom(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=PBKDF2_ITERATIONS).derive(passphrase.encode("utf-8"))
    ct = AESGCM(key).encrypt(iv, json.dumps(obj, ensure_ascii=False).encode("utf-8"), None)
    b64 = lambda b: base64.b64encode(b).decode()
    return {"v": 1, "iter": PBKDF2_ITERATIONS, "salt": b64(salt), "iv": b64(iv), "ct": b64(ct)}


def decrypt_from_browser_format(blob: dict, passphrase: str):
    """Inverse of encrypt_for_browser (used by tests and for local checks)."""
    d = lambda k: base64.b64decode(blob[k])
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=d("salt"),
                     iterations=blob["iter"]).derive(passphrase.encode("utf-8"))
    return json.loads(AESGCM(key).decrypt(d("iv"), d("ct"), None))
