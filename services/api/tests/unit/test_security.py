import time

import jwt
import pytest

from app.core.errors import Unauthorized
from app.modules.auth import security

SECRET = "s" * 40


async def test_password_hash_verifies_and_is_salted():
    h1 = await security.hash_password("correct horse battery")
    h2 = await security.hash_password("correct horse battery")
    assert h1 != h2  # random salt
    assert h1.startswith("$argon2id$")
    assert await security.verify_password(h1, "correct horse battery")
    assert not await security.verify_password(h1, "wrong")


async def test_unknown_account_never_verifies():
    # No stored hash (unknown email): always False, but a dummy hash is still checked.
    assert not await security.verify_password(None, "anything")


async def test_garbage_hash_is_a_failure_not_an_exception():
    assert not await security.verify_password("not-a-hash", "x")


def test_access_token_roundtrip():
    import uuid

    admin_id = uuid.uuid4()
    token = security.issue_access_token(admin_id, "admin", SECRET, 60)
    claims = security.decode_access_token(token, SECRET)
    assert claims.admin_id == admin_id and claims.role == "admin"


def test_expired_token_is_rejected():
    import uuid

    token = security.issue_access_token(uuid.uuid4(), "admin", SECRET, -10)
    with pytest.raises(Unauthorized):
        security.decode_access_token(token, SECRET)


def test_token_signed_with_another_secret_is_rejected():
    import uuid

    token = security.issue_access_token(uuid.uuid4(), "admin", "x" * 40, 60)
    with pytest.raises(Unauthorized):
        security.decode_access_token(token, SECRET)


def test_alg_none_token_is_rejected():
    now = int(time.time())
    payload = {
        "sub": "7d444840-9dc0-11d1-b245-5ffdce74fad2",
        "typ": "access",
        "iat": now,
        "exp": now + 60,
    }
    forged = jwt.encode(payload, key="", algorithm="none")
    with pytest.raises(Unauthorized):
        security.decode_access_token(forged, SECRET)


def test_token_of_wrong_type_is_rejected():
    now = int(time.time())
    token = jwt.encode(
        {
            "sub": "7d444840-9dc0-11d1-b245-5ffdce74fad2",
            "typ": "refresh",
            "iat": now,
            "exp": now + 60,
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(Unauthorized):
        security.decode_access_token(token, SECRET)


def test_token_without_required_claims_is_rejected():
    token = jwt.encode({"sub": "x"}, SECRET, algorithm="HS256")
    with pytest.raises(Unauthorized):
        security.decode_access_token(token, SECRET)


def test_refresh_tokens_are_random_and_hash_is_stable():
    a, b = security.new_refresh_token(), security.new_refresh_token()
    assert a != b and len(a) >= 40
    assert security.hash_refresh_token(a) == security.hash_refresh_token(a)
    assert security.hash_refresh_token(a) != security.hash_refresh_token(b)
    assert a not in security.hash_refresh_token(a)
