import re

import pytest
from sqlalchemy import text

from app.modules.projects.service import ProjectService
from tests.integration.conftest import ADMIN_EMAIL, ADMIN_PASSWORD
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration

KEY_RE = re.compile(r"^swy_[A-Za-z0-9]{8}_[A-Za-z0-9]{32}$")


@pytest.fixture
async def root(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def project(client, root):
    r = await client.post("/projects", json={"name": "shop", "description": "demo"}, headers=root)
    assert r.status_code == 201, r.text
    return r.json()


async def second_admin(client, root, email="second@example.com", role="admin"):
    await make_admin(client, root, email, role)
    return bearer((await login(client, email, GOOD_PASSWORD)).json())


# ---- projects and membership (IDOR) ----------------------------------------------------------
async def test_creator_is_a_member_and_sees_the_project(client, root, project):
    listed = (await client.get("/projects", headers=root)).json()
    assert [p["id"] for p in listed] == [project["id"]]
    members = (await client.get(f"/projects/{project['id']}/members", headers=root)).json()
    assert [m["email"] for m in members] == [ADMIN_EMAIL]


async def test_duplicate_project_name_conflicts(client, root, project):
    r = await client.post("/projects", json={"name": "shop"}, headers=root)
    assert r.status_code == 409


async def test_non_member_cannot_see_or_touch_the_project(client, root, project):
    other = await second_admin(client, root)
    pid = project["id"]
    assert (await client.get("/projects", headers=other)).json() == []
    # 404, not 403: the response must not reveal that the id exists
    assert (await client.get(f"/projects/{pid}", headers=other)).status_code == 404
    assert (await client.get(f"/projects/{pid}/keys", headers=other)).status_code == 404
    assert (await client.get(f"/projects/{pid}/members", headers=other)).status_code == 404
    created = await client.post(f"/projects/{pid}/keys", json={"name": "x"}, headers=other)
    assert created.status_code == 404
    unknown = await client.get("/projects/00000000-0000-0000-0000-000000000000", headers=other)
    foreign = await client.get(f"/projects/{pid}", headers=other)
    # a foreign project and a missing one are indistinguishable (apart from the request id)
    strip = lambda r: {k: v for k, v in r.json().items() if k != "correlation_id"}  # noqa: E731
    assert unknown.status_code == foreign.status_code == 404
    assert strip(unknown) == strip(foreign)


async def test_member_added_gains_access_and_can_be_removed(client, root, project):
    other = await second_admin(client, root)
    pid = project["id"]
    added = await client.post(
        f"/projects/{pid}/members", json={"email": "second@example.com"}, headers=root
    )
    assert added.status_code == 201
    assert (await client.get(f"/projects/{pid}", headers=other)).status_code == 200
    again = await client.post(
        f"/projects/{pid}/members", json={"email": "second@example.com"}, headers=root
    )
    assert again.status_code == 409

    removed = await client.delete(
        f"/projects/{pid}/members/{added.json()['admin_id']}", headers=root
    )
    assert removed.status_code == 204
    assert (await client.get(f"/projects/{pid}", headers=other)).status_code == 404


async def test_last_member_cannot_be_removed(client, root, project):
    me = (await client.get("/auth/me", headers=root)).json()
    r = await client.delete(f"/projects/{project['id']}/members/{me['id']}", headers=root)
    assert r.status_code == 409


async def test_add_unknown_member_is_404(client, root, project):
    r = await client.post(
        f"/projects/{project['id']}/members", json={"email": "ghost@example.com"}, headers=root
    )
    assert r.status_code == 404


async def test_viewer_member_can_read_but_not_change(client, root, project):
    viewer = await second_admin(client, root, "viewer@example.com", "viewer")
    pid = project["id"]
    await client.post(
        f"/projects/{pid}/members", json={"email": "viewer@example.com"}, headers=root
    )
    assert (await client.get(f"/projects/{pid}/keys", headers=viewer)).status_code == 200
    assert (
        await client.post(f"/projects/{pid}/keys", json={"name": "k"}, headers=viewer)
    ).status_code == 403


# ---- API keys --------------------------------------------------------------------------------
async def create_key(client, root, project, **body):
    r = await client.post(
        f"/projects/{project['id']}/keys", json={"name": "prod", **body}, headers=root
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_key_is_shown_once_and_never_again(client, root, project):
    created = await create_key(client, root, project)
    assert KEY_RE.match(created["key"])
    assert created["key"].split("_")[1] == created["prefix"]

    listed = (await client.get(f"/projects/{project['id']}/keys", headers=root)).json()
    assert len(listed) == 1 and listed[0]["prefix"] == created["prefix"]
    assert "key" not in listed[0] and "secret_hash" not in listed[0]
    secret = created["key"].split("_")[2]
    assert secret not in (await client.get(f"/projects/{project['id']}/keys", headers=root)).text


async def test_secret_cannot_be_recovered_from_the_database(client, root, project, api_app):
    created = await create_key(client, root, project)
    secret = created["key"].split("_")[2]
    async with api_app.state.resources.session_factory() as s:
        row = (await s.execute(text("SELECT prefix, secret_hash FROM api_keys"))).one()
        everything = str((await s.execute(text("SELECT * FROM audit_log"))).all())
    assert row.prefix == created["prefix"]
    assert len(row.secret_hash) == 64 and secret not in row.secret_hash
    assert secret not in everything and created["key"] not in everything


async def test_key_authentication_rules(client, root, project, api_app, app_settings):
    created = await create_key(client, root, project)
    raw = created["key"]
    async with api_app.state.resources.session_factory() as s:
        service = ProjectService(s, app_settings)
        found = await service.authenticate_key(raw)
        assert found is not None and found.prefix == created["prefix"]
        # wrong secret, wrong prefix, malformed
        assert await service.authenticate_key(raw[:-1] + ("a" if raw[-1] != "a" else "b")) is None
        assert await service.authenticate_key("swy_AAAAAAAA_" + "b" * 32) is None
        assert await service.authenticate_key("garbage") is None
        assert await service.authenticate_key(raw + "\n") is None


async def test_revoked_key_no_longer_authenticates(client, root, project, api_app, app_settings):
    created = await create_key(client, root, project)
    pid, kid = project["id"], created["id"]
    r = await client.post(f"/projects/{pid}/keys/{kid}/revoke", headers=root)
    assert r.status_code == 200 and r.json()["revoked_at"] is not None
    again = await client.post(f"/projects/{pid}/keys/{kid}/revoke", headers=root)
    assert again.json()["revoked_at"] == r.json()["revoked_at"]  # idempotent
    async with api_app.state.resources.session_factory() as s:
        assert await ProjectService(s, app_settings).authenticate_key(created["key"]) is None


async def test_key_settings_are_stored_and_partially_updatable(client, root, project):
    created = await create_key(
        client,
        root,
        project,
        default_task_type="classification",
        rate_limit_rpm=60,
        monthly_budget_usd="12.5",
    )
    assert created["default_task_type"] == "classification" and created["rate_limit_rpm"] == 60
    assert created["monthly_budget_usd"] == "12.5000"

    url = f"/projects/{project['id']}/keys/{created['id']}"
    patched = (await client.patch(url, json={"rate_limit_rpm": 120}, headers=root)).json()
    assert patched["rate_limit_rpm"] == 120 and patched["default_task_type"] == "classification"
    cleared = (await client.patch(url, json={"monthly_budget_usd": None}, headers=root)).json()
    assert cleared["monthly_budget_usd"] is None and cleared["rate_limit_rpm"] == 120
    renamed = (await client.patch(url, json={"name": "renamed"}, headers=root)).json()
    assert renamed["name"] == "renamed"


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "k", "rate_limit_rpm": 0},
        {"name": "k", "rate_limit_rpm": -5},
        {"name": "k", "monthly_budget_usd": "-1"},
        {"name": "k", "default_task_type": "Has Spaces"},
        {"name": "k", "default_task_type": "x" * 65},
    ],
)
async def test_invalid_key_settings_are_rejected(client, root, project, body):
    r = await client.post(f"/projects/{project['id']}/keys", json=body, headers=root)
    assert r.status_code == 422


async def test_key_of_unknown_project_or_other_project_is_404(client, root, project):
    created = await create_key(client, root, project)
    other_project = (await client.post("/projects", json={"name": "second"}, headers=root)).json()
    r = await client.post(
        f"/projects/{other_project['id']}/keys/{created['id']}/revoke", headers=root
    )
    assert r.status_code == 404  # the key belongs to a different project


async def test_audit_log_covers_project_and_key_actions(client, root, project, api_app):
    created = await create_key(client, root, project)
    await client.post(f"/projects/{project['id']}/keys/{created['id']}/revoke", headers=root)
    async with api_app.state.resources.session_factory() as s:
        actions = {r[0] for r in await s.execute(text("SELECT action FROM audit_log"))}
    assert {"project.create", "api_key.create", "api_key.revoke"} <= actions
    assert ADMIN_PASSWORD not in str(actions)
