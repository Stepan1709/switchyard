from hypothesis import given
from hypothesis import strategies as st

from app.modules.projects import keys

PEPPER = "p" * 40


def test_generated_key_has_documented_format():
    raw, prefix, secret = keys.generate_key()
    assert raw == f"swy_{prefix}_{secret}"
    assert len(prefix) == keys.PREFIX_LENGTH and len(secret) == keys.SECRET_LENGTH
    assert prefix.isalnum() and secret.isalnum()


def test_generated_keys_are_unique():
    seen = {keys.generate_key()[0] for _ in range(500)}
    assert len(seen) == 500


def test_parse_valid_and_invalid():
    raw, prefix, secret = keys.generate_key()
    assert keys.parse_key(raw) == (prefix, secret)
    for bad in [
        "",
        "swy_",
        f"sk_{prefix}_{secret}",
        f"swy_{prefix}_{secret[:-1]}",
        f"swy_{prefix}_{secret}x",
        f"swy_{prefix}-{secret}",
        f" {raw}",
        f"{raw}\n",
    ]:
        assert keys.parse_key(bad) is None, bad


def test_hash_is_hex_sha256_and_depends_on_pepper():
    _, _, secret = keys.generate_key()
    h = keys.hash_secret(secret, PEPPER)
    assert len(h) == 64 and int(h, 16) >= 0
    assert h == keys.hash_secret(secret, PEPPER)
    assert h != keys.hash_secret(secret, "q" * 40)
    assert secret not in h


def test_verify_accepts_only_the_right_secret_and_pepper():
    _, _, secret = keys.generate_key()
    stored = keys.hash_secret(secret, PEPPER)
    assert keys.verify_secret(secret, stored, PEPPER)
    assert not keys.verify_secret(secret + "x", stored, PEPPER)
    assert not keys.verify_secret(secret, stored, "q" * 40)
    assert not keys.verify_secret(secret, "0" * 64, PEPPER)


@given(st.text(max_size=80))
def test_arbitrary_text_never_crashes_and_only_valid_keys_parse(text: str):
    parsed = keys.parse_key(text)
    if parsed is not None:
        assert text == f"swy_{parsed[0]}_{parsed[1]}"


@given(st.integers(min_value=0, max_value=50))
def test_roundtrip_property(_: int):
    raw, prefix, secret = keys.generate_key()
    assert keys.parse_key(raw) == (prefix, secret)
    assert keys.verify_secret(secret, keys.hash_secret(secret, PEPPER), PEPPER)
