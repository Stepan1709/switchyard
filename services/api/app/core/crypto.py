"""Encryption of secrets stored in the database (provider access keys).

Scheme: AES-256-GCM (authenticated encryption). Stored value:

    v1.<key_id>.<base64url(nonce || ciphertext+tag)>

* `key_id` names the master key that encrypted the value, so the key ring can hold several
  keys: new values use the first key, old values are still decrypted with the key they were
  written with. That makes master key rotation possible without downtime.
* A fresh random 96-bit nonce is generated for every encryption (never reused with a key).
* AAD (additional authenticated data) is the id of the owning row. GCM then fails if someone
  copies a ciphertext from one row to another, which a plain encryption would not notice.
"""

import base64
import binascii
import os
import re

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_KEY_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")
_VERSION = "v1"
_NONCE_BYTES = 12


class DecryptionError(Exception):
    """The value is malformed, was encrypted with an unknown key, or was tampered with."""


class KeyRing:
    def __init__(self, keys: dict[str, bytes], active: str) -> None:
        self._keys = keys
        self._active = active

    @classmethod
    def from_text(cls, text: str) -> "KeyRing":
        """Parse lines `key_id:base64(32 bytes)`; the first line is the active key."""
        keys: dict[str, bytes] = {}
        active: str | None = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key_id, _, encoded = line.partition(":")
            if not _KEY_ID_RE.fullmatch(key_id):
                raise ValueError("master key id must be 1-16 characters of [A-Za-z0-9_-]")
            if key_id in keys:
                raise ValueError(f"duplicate master key id {key_id!r}")
            try:
                key = base64.b64decode(encoded, validate=True)
            except binascii.Error as exc:
                raise ValueError(f"master key {key_id!r} is not valid base64") from exc
            if len(key) != 32:
                raise ValueError(f"master key {key_id!r} must be exactly 32 bytes")
            keys[key_id] = key
            active = active or key_id
        if active is None:
            raise ValueError("no master keys configured (set MASTER_KEYS_FILE)")
        return cls(keys, active)

    @property
    def active_key_id(self) -> str:
        return self._active

    def encrypt(self, plaintext: str, aad: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(self._keys[self._active]).encrypt(nonce, plaintext.encode(), aad.encode())
        blob = base64.urlsafe_b64encode(nonce + sealed).decode()
        return f"{_VERSION}.{self._active}.{blob}"

    def decrypt(self, token: str, aad: str) -> str:
        try:
            version, key_id, blob = token.split(".")
            if version != _VERSION or key_id not in self._keys:
                raise DecryptionError("unsupported version or unknown master key")
            raw = base64.urlsafe_b64decode(blob)
            nonce, sealed = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
            return AESGCM(self._keys[key_id]).decrypt(nonce, sealed, aad.encode()).decode()
        except (ValueError, binascii.Error, InvalidTag) as exc:
            # One generic error: details of why decryption failed help an attacker, not us.
            raise DecryptionError("cannot decrypt value") from exc


def mask_secret(secret: str) -> str:
    """What the API may show instead of a secret: only the last four characters."""
    return "****" if len(secret) <= 8 else f"****{secret[-4:]}"
