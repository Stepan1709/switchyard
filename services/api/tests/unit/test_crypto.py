import base64

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.crypto import DecryptionError, KeyRing, mask_secret


def b64(byte: bytes) -> str:
    return base64.b64encode(byte * 32).decode()


RING = KeyRing.from_text(f"k1:{b64(b'1')}")


def test_roundtrip_and_format():
    token = RING.encrypt("sk-secret", aad="row-1")
    assert token.startswith("v1.k1.")
    assert "sk-secret" not in token
    assert RING.decrypt(token, aad="row-1") == "sk-secret"


def test_every_encryption_uses_a_fresh_nonce():
    assert RING.encrypt("same", aad="a") != RING.encrypt("same", aad="a")


def test_ciphertext_does_not_decrypt_for_another_row():
    token = RING.encrypt("sk-secret", aad="row-1")
    with pytest.raises(DecryptionError):
        RING.decrypt(token, aad="row-2")


def test_tampered_ciphertext_is_rejected():
    version, key_id, blob = RING.encrypt("sk-secret", aad="a").split(".")
    raw = bytearray(base64.urlsafe_b64decode(blob))
    raw[-1] ^= 1  # flip one bit in the authentication tag
    forged = ".".join([version, key_id, base64.urlsafe_b64encode(bytes(raw)).decode()])
    with pytest.raises(DecryptionError):
        RING.decrypt(forged, aad="a")


@pytest.mark.parametrize("garbage", ["", "x", "v1.k1", "v1.k1.!!!", "v2.k1.AAAA", "v1.zz.AAAA"])
def test_garbage_is_a_decryption_error_not_a_crash(garbage):
    with pytest.raises(DecryptionError):
        RING.decrypt(garbage, aad="a")


def test_key_rotation_old_values_still_decrypt():
    old = RING.encrypt("sk-secret", aad="row")
    rotated = KeyRing.from_text(f"k2:{b64(b'2')}\nk1:{b64(b'1')}")
    assert rotated.active_key_id == "k2"
    assert rotated.decrypt(old, aad="row") == "sk-secret"  # written with k1
    assert rotated.encrypt("new", aad="row").startswith("v1.k2.")  # new values use k2


def test_value_of_a_removed_key_cannot_be_decrypted():
    only_k2 = KeyRing.from_text(f"k2:{b64(b'2')}")
    with pytest.raises(DecryptionError):
        only_k2.decrypt(RING.encrypt("x", aad="a"), aad="a")


def test_wrong_key_material_fails():
    other = KeyRing.from_text(f"k1:{b64(b'9')}")  # same id, different bytes
    with pytest.raises(DecryptionError):
        other.decrypt(RING.encrypt("x", aad="a"), aad="a")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "no master keys"),
        ("# only a comment\n", "no master keys"),
        ("k1:not-base64!!", "base64"),
        (f"k1:{base64.b64encode(b'short').decode()}", "32 bytes"),
        (f"bad id:{b64(b'1')}", "key id"),
        (f"k1:{b64(b'1')}\nk1:{b64(b'2')}", "duplicate"),
    ],
)
def test_invalid_key_files_are_rejected(text, message):
    with pytest.raises(ValueError, match=message):
        KeyRing.from_text(text)


def test_comments_and_blank_lines_are_ignored():
    ring = KeyRing.from_text(f"# generated\n\nk1:{b64(b'1')}\n")
    assert ring.active_key_id == "k1"


def test_mask_shows_at_most_the_last_four_characters():
    assert mask_secret("short") == "****"
    assert mask_secret("sk-abcdefghijkl") == "****ijkl"


@given(st.text(max_size=200), st.text(max_size=50))
def test_roundtrip_property(plaintext, aad):
    assert RING.decrypt(RING.encrypt(plaintext, aad), aad) == plaintext
