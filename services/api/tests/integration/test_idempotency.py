"""Idempotency-Key on unsafe administrator POSTs."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.idempotency import purge_expired
from tests.integration.conftest import MOCK_KEY, MockServer
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration


@pytest.fixture
async def root(client):
    return bearer((await login(client)).json())


def key() -> str:
    return f"key-{uuid.uuid4().hex[:12]}"


async def count(api_app, table: str, where: str = "true") -> int:
    async with api_app.state.resources.session_factory() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))).scalar_one()


async def test_a_repeated_request_returns_the_stored_response_and_creates_nothing_new(
    client, root, api_app
):
    k = key()
    headers = {**root, "Idempotency-Key": k}
    first = await client.post("/projects", json={"name": "shop"}, headers=headers)
    again = await client.post("/projects", json={"name": "shop"}, headers=headers)

    assert first.status_code == again.status_code == 201
    assert first.json() == again.json()  # the very same project id
    assert "idempotent-replayed" not in first.headers
    assert again.headers["idempotent-replayed"] == "true"
    assert await count(api_app, "projects") == 1


async def test_without_the_header_requests_are_not_deduplicated(client, root):
    one = await client.post("/projects", json={"name": "a"}, headers=root)
    two = await client.post("/projects", json={"name": "b"}, headers=root)
    assert one.status_code == two.status_code == 201 and one.json()["id"] != two.json()["id"]
    dup = await client.post("/projects", json={"name": "a"}, headers=root)
    assert dup.status_code == 409  # a normal business conflict, not a replay


async def test_the_same_key_with_a_different_request_is_rejected(client, root, api_app):
    headers = {**root, "Idempotency-Key": key()}
    assert (
        await client.post("/projects", json={"name": "one"}, headers=headers)
    ).status_code == 201
    other = await client.post("/projects", json={"name": "two"}, headers=headers)
    assert other.status_code == 422 and other.json()["code"] == "idempotency_key_reused"
    assert await count(api_app, "projects") == 1  # the second request was not executed

    elsewhere = await client.post("/providers", json={"name": "p"}, headers=headers)
    assert (
        elsewhere.status_code == 422
    )  # another endpoint with the same key is also a different request


async def test_parallel_duplicates_execute_once(client, root, api_app):
    headers = {**root, "Idempotency-Key": key()}
    responses = await asyncio.gather(
        *(client.post("/projects", json={"name": "race"}, headers=headers) for _ in range(6))
    )
    statuses = sorted(r.status_code for r in responses)
    assert await count(api_app, "projects") == 1
    assert statuses.count(201) >= 1 and set(statuses) <= {201, 409}  # 409 = still in progress
    ids = {r.json()["id"] for r in responses if r.status_code == 201}
    assert len(ids) == 1  # everybody who succeeded got the same project


async def test_an_unfinished_request_with_the_same_key_gets_409_and_retry_after(
    client, root, api_app
):
    body = json.dumps({"name": "slow"}).encode()
    k = key()
    async with api_app.state.resources.session_factory() as s, s.begin():
        me = (await s.execute(text("SELECT id FROM admins"))).scalar_one()
        await s.execute(
            text(
                "INSERT INTO idempotency_keys (id, admin_id, key, method, path, request_hash,"
                " locked_at, expires_at) VALUES (gen_random_uuid(), :a, :k, 'POST', '/projects',"
                " :h, now(), now() + interval '1 day')"
            ),
            {"a": me, "k": k, "h": hashlib.sha256(body).hexdigest()},
        )
    r = await client.post(
        "/projects",
        content=body,
        headers={**root, "Idempotency-Key": k, "content-type": "application/json"},
    )
    assert r.status_code == 409 and r.json()["code"] == "request_in_progress"
    assert r.headers["retry-after"] == "1"


async def test_a_claim_left_by_a_crashed_process_is_taken_over(client, root, api_app):
    body = json.dumps({"name": "after-crash"}).encode()
    k = key()
    async with api_app.state.resources.session_factory() as s, s.begin():
        me = (await s.execute(text("SELECT id FROM admins"))).scalar_one()
        await s.execute(
            text(
                "INSERT INTO idempotency_keys (id, admin_id, key, method, path, request_hash,"
                " locked_at, expires_at) VALUES (gen_random_uuid(), :a, :k, 'POST', '/projects',"
                " :h, now() - interval '10 minutes', now() + interval '1 day')"
            ),
            {"a": me, "k": k, "h": hashlib.sha256(body).hexdigest()},
        )
    r = await client.post(
        "/projects",
        content=body,
        headers={**root, "Idempotency-Key": k, "content-type": "application/json"},
    )
    assert r.status_code == 201  # the stale claim (older than the lock time) did not block it


async def test_a_failed_first_attempt_does_not_poison_the_key(client, root, api_app):
    await client.post("/projects", json={"name": "taken"}, headers=root)
    k = key()
    headers = {**root, "Idempotency-Key": k}
    failed = await client.post("/projects", json={"name": "taken"}, headers=headers)
    assert failed.status_code == 409  # the business rule failed
    assert await count(api_app, "idempotency_keys") == 0  # no claim was left behind

    fixed = await client.post("/projects", json={"name": "fresh"}, headers=headers)
    assert fixed.status_code == 201  # the same key works once the request is corrected


async def test_keys_belong_to_the_administrator_who_used_them(client, root):
    await make_admin(client, root, "second@example.com", "admin")
    other = bearer((await login(client, "second@example.com", GOOD_PASSWORD)).json())
    k = key()
    mine = await client.post(
        "/projects", json={"name": "mine"}, headers={**root, "Idempotency-Key": k}
    )
    theirs = await client.post(
        "/projects", json={"name": "theirs"}, headers={**other, "Idempotency-Key": k}
    )
    assert mine.status_code == theirs.status_code == 201  # the same key text, independent scopes
    assert mine.json()["id"] != theirs.json()["id"]


async def test_expired_keys_are_forgotten(client, root, api_app):
    k = key()
    headers = {**root, "Idempotency-Key": k}
    assert (
        await client.post("/projects", json={"name": "old"}, headers=headers)
    ).status_code == 201
    async with api_app.state.resources.session_factory() as s, s.begin():
        await s.execute(text("UPDATE idempotency_keys SET expires_at = now() - interval '1 hour'"))
    again = await client.post("/projects", json={"name": "old"}, headers=headers)
    assert again.status_code == 409  # executed again (the name is taken), not replayed
    assert "idempotent-replayed" not in again.headers


async def test_purge_removes_only_expired_keys(client, root, api_app):
    for name in ("a", "b"):
        await client.post(
            "/projects", json={"name": name}, headers={**root, "Idempotency-Key": key()}
        )
    async with api_app.state.resources.session_factory() as s, s.begin():
        await s.execute(
            text(
                "UPDATE idempotency_keys SET expires_at = :t"
                " WHERE id = (SELECT id FROM idempotency_keys LIMIT 1)"
            ),
            {"t": datetime.now(UTC) - timedelta(hours=1)},
        )
    assert await purge_expired(api_app.state.resources.session_factory) == 1
    assert await count(api_app, "idempotency_keys") == 1


@pytest.mark.parametrize("bad", ["", "has space", "x" * 256, "semi;colon", "a/b"])
async def test_malformed_keys_are_rejected(client, root, bad):
    r = await client.post("/projects", json={"name": "x"}, headers={**root, "Idempotency-Key": bad})
    assert r.status_code in {422, 400}


async def test_other_creating_endpoints_support_the_key(client, root, mock_llm: MockServer):
    # providers, models and prices are covered too
    pk = {**root, "Idempotency-Key": key()}
    body = {"name": "mock", "base_url": mock_llm.base_url, "is_local": True, "api_key": MOCK_KEY}
    p1 = await client.post("/providers", json=body, headers=pk)
    p2 = await client.post("/providers", json=body, headers=pk)
    assert p1.status_code == p2.status_code == 201 and p1.json()["id"] == p2.json()["id"]

    mk = {**root, "Idempotency-Key": key()}
    model = {"provider_id": p1.json()["id"], "name": "mock-tiny", "context_window": 4096}
    m1 = await client.post("/models", json=model, headers=mk)
    m2 = await client.post("/models", json=model, headers=mk)
    assert m1.json()["id"] == m2.json()["id"] and m2.headers["idempotent-replayed"] == "true"

    prk = {**root, "Idempotency-Key": key()}
    price = {"input_per_1m": "0.1", "output_per_1m": "0.2"}
    r1 = await client.post(f"/models/{m1.json()['id']}/prices", json=price, headers=prk)
    r2 = await client.post(f"/models/{m1.json()['id']}/prices", json=price, headers=prk)
    assert r1.json()["id"] == r2.json()["id"]
    history = await client.get(f"/models/{m1.json()['id']}/prices", headers=root)
    assert len(history.json()) == 1  # a replay did not add a second price version


async def test_stored_responses_never_contain_provider_secrets(client, root, api_app, mock_llm):
    body = {"name": "mock", "base_url": mock_llm.base_url, "is_local": True, "api_key": MOCK_KEY}
    await client.post("/providers", json=body, headers={**root, "Idempotency-Key": key()})
    async with api_app.state.resources.session_factory() as s:
        rows = await s.execute(text("SELECT response::text FROM idempotency_keys"))
        stored = " ".join(r[0] for r in rows)  # the whole JSON, not a shortened repr
    assert MOCK_KEY not in stored and "****" in stored  # only the mask was stored


async def test_client_key_creation_is_deliberately_not_idempotent(client, root, api_app):
    """The response holds the key secret, which must not be stored for replays."""
    project = (await client.post("/projects", json={"name": "keys"}, headers=root)).json()
    headers = {**root, "Idempotency-Key": key()}
    one = await client.post(f"/projects/{project['id']}/keys", json={"name": "k"}, headers=headers)
    two = await client.post(f"/projects/{project['id']}/keys", json={"name": "k"}, headers=headers)
    assert one.json()["key"] != two.json()["key"]  # two different keys: the header is ignored here
    assert await count(api_app, "idempotency_keys") == 0


async def test_viewers_cannot_use_idempotent_creation_endpoints(client, root):
    await make_admin(client, root, "viewer@example.com", "viewer")
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    r = await client.post(
        "/projects", json={"name": "x"}, headers={**viewer, "Idempotency-Key": key()}
    )
    assert r.status_code == 403
