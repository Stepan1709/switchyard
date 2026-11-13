from typing import Any

import httpx
import pytest
from sqlalchemy import text

from tests.integration.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

pytestmark = pytest.mark.integration

GOOD_PASSWORD = "a-long-enough-password"


async def login(
    client: httpx.AsyncClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
):
    return await client.post("/auth/login", json={"email": email, "password": password})


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def make_admin(client, headers, email: str, role: str = "viewer") -> None:
    r = await client.post(
        "/admins", json={"email": email, "password": GOOD_PASSWORD, "role": role}, headers=headers
    )
    assert r.status_code == 201, r.text


async def audit_actions(api_app) -> list[str]:
    async with api_app.state.resources.session_factory() as s:
        rows = await s.execute(text("SELECT action FROM audit_log ORDER BY created_at"))
        return [r[0] for r in rows]


async def test_first_admin_is_bootstrapped_and_can_login(client):
    r = await login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["expires_in"] == 900
    me = await client.get("/auth/me", headers=bearer(body))
    assert me.json()["email"] == ADMIN_EMAIL and me.json()["role"] == "admin"
    assert "password" not in me.text


async def test_bootstrap_is_idempotent(api_app, app_settings):
    from app.modules.auth.service import AuthService

    async with api_app.state.resources.session_factory() as s:
        created = await AuthService(s, app_settings).ensure_first_admin(
            "other@example.com", GOOD_PASSWORD
        )
    assert created is False  # an administrator already exists


async def test_wrong_password_and_unknown_email_look_the_same(client):
    wrong = await login(client, password="nope-nope-nope")
    unknown = await login(client, email="ghost@example.com", password="nope-nope-nope")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]
    assert "WWW-Authenticate" in wrong.headers


async def test_password_is_stored_as_argon2_hash(api_app):
    async with api_app.state.resources.session_factory() as s:
        stored = (await s.execute(text("SELECT password_hash FROM admins"))).scalar_one()
    assert stored.startswith("$argon2id$") and ADMIN_PASSWORD not in stored


async def test_login_is_throttled_after_repeated_failures(client):
    for _ in range(5):
        assert (await login(client, password="wrong-wrong-wrong")).status_code == 401
    blocked = await login(client, password="wrong-wrong-wrong")
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) > 0
    # Even the right password is refused while the lock is active.
    assert (await login(client)).status_code == 429


async def test_throttle_is_per_email(client):
    for _ in range(5):
        await login(client, password="wrong-wrong-wrong")
    assert (await login(client)).status_code == 429
    # Another account from the same IP is still allowed to log in.
    tokens = await client.post("/auth/login", json={"email": "x@example.com", "password": "x"})
    assert tokens.status_code == 401  # unknown email: wrong credentials, but not throttled


async def test_success_resets_failure_counter(client):
    for _ in range(4):
        await login(client, password="wrong-wrong-wrong")
    assert (await login(client)).status_code == 200
    for _ in range(4):
        assert (await login(client, password="wrong-wrong-wrong")).status_code == 401


async def test_refresh_rotates_and_old_token_revokes_the_family(client):
    first = (await login(client)).json()
    second = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert second.status_code == 200
    second = second.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert (await client.get("/auth/me", headers=bearer(second))).status_code == 200

    # Replaying the already used token is treated as theft ...
    replay = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    # ... and kills the whole chain, including the token legitimately issued after it.
    after = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert after.status_code == 401


async def test_logout_revokes_refresh_token(client):
    tokens = (await login(client)).json()
    out = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert out.status_code == 204
    again = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert again.status_code == 401
    # logout is idempotent and does not reveal whether a token existed
    assert (await client.post("/auth/logout", json={"refresh_token": "unknown"})).status_code == 204


async def test_refresh_tokens_are_stored_hashed(client, api_app):
    tokens = (await login(client)).json()
    async with api_app.state.resources.session_factory() as s:
        rows = [r[0] for r in await s.execute(text("SELECT token_hash FROM refresh_tokens"))]
    assert rows and tokens["refresh_token"] not in rows


async def test_expired_refresh_token_is_rejected(client, api_app):
    tokens = (await login(client)).json()
    async with api_app.state.resources.session_factory() as s:
        await s.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 minute'"))
        await s.commit()
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_protected_endpoints_reject_missing_and_bad_tokens(client):
    assert (await client.get("/auth/me")).status_code == 401
    bad = await client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert bad.status_code == 401


async def test_disabled_admin_loses_access_immediately(client, api_app):
    tokens = (await login(client)).json()
    async with api_app.state.resources.session_factory() as s:
        await s.execute(text("UPDATE admins SET is_active = false"))
        await s.commit()
    assert (await client.get("/auth/me", headers=bearer(tokens))).status_code == 401
    assert (await login(client)).status_code == 401


async def test_viewer_cannot_create_admins_or_projects_but_can_read(client):
    root = bearer((await login(client)).json())
    await make_admin(client, root, "viewer@example.com", "viewer")
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())

    assert (await client.get("/auth/me", headers=viewer)).status_code == 200
    assert (await client.get("/projects", headers=viewer)).status_code == 200
    forbidden = await client.post("/projects", json={"name": "p"}, headers=viewer)
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "forbidden"
    assert (
        await client.post(
            "/admins", json={"email": "z@example.com", "password": GOOD_PASSWORD}, headers=viewer
        )
    ).status_code == 403


async def test_create_admin_validation(client):
    root = bearer((await login(client)).json())
    weak = await client.post(
        "/admins", json={"email": "w@example.com", "password": "short"}, headers=root
    )
    assert weak.status_code == 422 and weak.json()["code"] == "weak_password"
    await make_admin(client, root, "dup@example.com")
    dup = await client.post(
        "/admins", json={"email": "DUP@example.com", "password": GOOD_PASSWORD}, headers=root
    )
    assert dup.status_code == 409  # emails are case-insensitive
    bad = await client.post(
        "/admins", json={"email": "not-an-email", "password": GOOD_PASSWORD}, headers=root
    )
    assert bad.status_code == 422


async def test_audit_log_records_auth_events_without_secrets(client, api_app):
    await login(client, password="wrong-wrong-wrong")
    tokens = (await login(client)).json()
    await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    actions = await audit_actions(api_app)
    assert {"admin.create", "auth.login_failed", "auth.login", "auth.logout"} <= set(actions)
    async with api_app.state.resources.session_factory() as s:
        dump = str((await s.execute(text("SELECT details, ip FROM audit_log"))).all())
    for secret in (
        ADMIN_PASSWORD,
        "wrong-wrong-wrong",
        tokens["refresh_token"],
        tokens["access_token"],
    ):
        assert secret not in dump
