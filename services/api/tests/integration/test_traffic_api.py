"""Admin API of the traffic log, task types and project privacy settings."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def project(client, admin):
    return (await client.post("/projects", json={"name": "shop"}, headers=admin)).json()


async def insert_rows(api_app, project_id, specs):
    """Insert log rows directly. `specs` are dicts of column overrides."""
    async with api_app.state.resources.session_factory() as s, s.begin():
        for spec in specs:
            row = {
                "request_id": uuid.uuid4(),
                "started_at": datetime.now(UTC),
                "project_id": uuid.UUID(project_id),
                "mode": "record",
                "stream": False,
                "status": "ok",
                "task_type": "classification",
                "model": "mock-strong",
                **spec,
            }
            columns = ", ".join(row)
            params = ", ".join(f":{c}" for c in row)
            for key in ("request_payload", "response_payload"):
                if key in row:
                    row[key] = json.dumps(row[key])
            sql = f"INSERT INTO requests ({columns}) VALUES ({params})"
            if "request_payload" in row:
                sql = sql.replace(":request_payload", "CAST(:request_payload AS jsonb)")
            if "response_payload" in row:
                sql = sql.replace(":response_payload", "CAST(:response_payload AS jsonb)")
            await s.execute(text(sql), row)


def times(n, start=None, step=timedelta(minutes=1)):
    start = start or datetime.now(UTC) - timedelta(minutes=1)
    return [start - i * step for i in range(n)]


async def page(client, admin, pid, **params):
    r = await client.get(f"/projects/{pid}/requests", params=params, headers=admin)
    assert r.status_code == 200, r.text
    return r.json()


async def test_list_is_newest_first_and_hides_the_bodies(client, admin, project, api_app):
    ts = times(3)
    await insert_rows(
        api_app,
        project["id"],
        [
            {"started_at": t, "request_payload": {"secret": "body"}, "response_payload": {"a": 1}}
            for t in ts
        ],
    )
    data = await page(client, admin, project["id"])
    assert [i["started_at"] for i in data["items"]] == [
        t.isoformat().replace("+00:00", "Z") for t in ts
    ]
    assert data["next_cursor"] is None
    assert "request_payload" not in data["items"][0] and "response_payload" not in data["items"][0]


async def test_paging_by_cursor_neither_skips_nor_repeats_rows(client, admin, project, api_app):
    same = datetime.now(UTC) - timedelta(minutes=5)
    # twenty rows share ONE timestamp: only the (started_at, request_id) pair orders them
    await insert_rows(api_app, project["id"], [{"started_at": same} for _ in range(20)])
    await insert_rows(
        api_app, project["id"], [{"started_at": t} for t in times(15, same - timedelta(hours=1))]
    )

    seen, cursor, pages = [], None, 0
    while True:
        params = {"limit": 7, **({"cursor": cursor} if cursor else {})}
        data = await page(client, admin, project["id"], **params)
        seen += [i["request_id"] for i in data["items"]]
        pages += 1
        cursor = data["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == 35 and len(set(seen)) == 35  # nothing lost, nothing twice
    assert pages == 5


async def test_pages_cross_a_month_boundary(client, admin, project, api_app):
    now = datetime.now(UTC)
    last_month = now.replace(day=1) - timedelta(days=3)  # inside the previous month's partition
    await insert_rows(
        api_app, project["id"], [{"started_at": now - timedelta(minutes=i)} for i in range(3)]
    )
    await insert_rows(
        api_app,
        project["id"],
        [{"started_at": last_month - timedelta(minutes=i)} for i in range(3)],
    )
    first = await page(client, admin, project["id"], limit=4)
    second = await page(client, admin, project["id"], limit=4, cursor=first["next_cursor"])
    ids = [i["request_id"] for i in first["items"] + second["items"]]
    assert len(ids) == 6 and len(set(ids)) == 6


async def test_filters(client, admin, project, api_app):
    now = datetime.now(UTC)
    await insert_rows(
        api_app,
        project["id"],
        [
            {
                "task_type": "classification",
                "model": "mock-tiny",
                "status": "ok",
                "started_at": now - timedelta(minutes=1),
            },
            {
                "task_type": "classification",
                "model": "mock-strong",
                "status": "ok",
                "started_at": now - timedelta(minutes=2),
            },
            {
                "task_type": "extraction",
                "model": "mock-strong",
                "status": "upstream_error",
                "started_at": now - timedelta(hours=3),
            },
        ],
    )
    assert len((await page(client, admin, project["id"]))["items"]) == 3
    assert len((await page(client, admin, project["id"], task_type="classification"))["items"]) == 2
    assert len((await page(client, admin, project["id"], model="mock-strong"))["items"]) == 2
    errors = (await page(client, admin, project["id"], status="upstream_error"))["items"]
    assert [i["task_type"] for i in errors] == ["extraction"]
    recent = await page(client, admin, project["id"], since=(now - timedelta(hours=1)).isoformat())
    assert len(recent["items"]) == 2
    old = await page(client, admin, project["id"], until=(now - timedelta(hours=1)).isoformat())
    assert len(old["items"]) == 1
    both = await page(client, admin, project["id"], task_type="classification", model="mock-tiny")
    assert len(both["items"]) == 1


async def test_bad_parameters_are_rejected(client, admin, project):
    base = f"/projects/{project['id']}/requests"
    assert (await client.get(base, params={"limit": 0}, headers=admin)).status_code == 422
    assert (await client.get(base, params={"limit": 201}, headers=admin)).status_code == 422
    bad = await client.get(base, params={"cursor": "garbage"}, headers=admin)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"


async def test_detail_returns_the_stored_bodies(client, admin, project, api_app):
    rid = uuid.uuid4()
    await insert_rows(
        api_app,
        project["id"],
        [
            {
                "request_id": rid,
                "request_payload": {"messages": [{"role": "user", "content": "hi"}]},
                "response_payload": {"choices": []},
            }
        ],
    )
    r = await client.get(f"/projects/{project['id']}/requests/{rid}", headers=admin)
    assert r.status_code == 200
    assert r.json()["request_payload"]["messages"][0]["content"] == "hi"
    assert (
        await client.get(f"/projects/{project['id']}/requests/{uuid.uuid4()}", headers=admin)
    ).status_code == 404


async def test_a_request_of_another_project_is_not_reachable_through_this_one(
    client, admin, project, api_app
):
    other = (await client.post("/projects", json={"name": "other"}, headers=admin)).json()
    rid = uuid.uuid4()
    await insert_rows(api_app, other["id"], [{"request_id": rid}])
    assert (
        await client.get(f"/projects/{project['id']}/requests/{rid}", headers=admin)
    ).status_code == 404
    assert (await page(client, admin, project["id"]))["items"] == []  # and it is not in the list


async def test_non_members_see_nothing_and_viewers_can_read(client, admin, project, api_app):
    await insert_rows(api_app, project["id"], [{}])
    await make_admin(client, admin, "outsider@example.com", "admin")
    outsider = bearer((await login(client, "outsider@example.com", GOOD_PASSWORD)).json())
    pid = project["id"]
    for path in ("requests", f"requests/{uuid.uuid4()}", "task-types"):
        assert (await client.get(f"/projects/{pid}/{path}", headers=outsider)).status_code == 404

    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(
        f"/projects/{pid}/members", json={"email": "viewer@example.com"}, headers=admin
    )
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert len((await page(client, viewer, pid))["items"]) == 1
    assert (await client.get(f"/projects/{pid}/task-types", headers=viewer)).status_code == 200
    assert (
        await client.post(f"/projects/{pid}/task-types", json={"name": "x"}, headers=viewer)
    ).status_code == 403


async def test_task_types_can_be_created_and_their_mode_is_restricted(client, admin, project):
    pid = project["id"]
    created = await client.post(
        f"/projects/{pid}/task-types", json={"name": "classification"}, headers=admin
    )
    assert created.status_code == 201
    assert created.json() == {
        "name": "classification",
        "mode": "record",
        "shadow_model_ids": [],
        "shadow_rate": 0.0,
        "auto_registered": False,
        "created_at": created.json()["created_at"],
    }
    assert (
        await client.post(
            f"/projects/{pid}/task-types", json={"name": "classification"}, headers=admin
        )
    ).status_code == 409
    for bad in ("Has Space", "x" * 65, "-lead", ""):
        assert (
            await client.post(f"/projects/{pid}/task-types", json={"name": bad}, headers=admin)
        ).status_code == 422

    url = f"/projects/{pid}/task-types/classification"
    assert (await client.patch(url, json={"mode": "record"}, headers=admin)).status_code == 200
    # the other modes need what makes them work: an active rule, or models to copy to
    route = await client.patch(url, json={"mode": "route"}, headers=admin)
    assert route.status_code == 422 and "active rule" in route.json()["detail"]
    shadow = await client.patch(url, json={"mode": "shadow"}, headers=admin)
    assert shadow.status_code == 422 and "shadow_model_ids" in shadow.json()["detail"]
    assert (await client.patch(url, json={"mode": "bogus"}, headers=admin)).status_code == 422
    assert (
        await client.patch(
            f"/projects/{pid}/task-types/missing", json={"mode": "record"}, headers=admin
        )
    ).status_code == 404
    listed = (await client.get(f"/projects/{pid}/task-types", headers=admin)).json()
    assert [t["name"] for t in listed] == ["classification"]


async def test_project_privacy_settings(client, admin, project, api_app):
    pid = project["id"]
    assert project["store_bodies"] is True and project["mask_pii"] is False  # the defaults
    r = await client.patch(f"/projects/{pid}", json={"mask_pii": True}, headers=admin)
    assert (
        r.status_code == 200 and r.json()["mask_pii"] is True and r.json()["store_bodies"] is True
    )
    r = await client.patch(f"/projects/{pid}", json={"store_bodies": False}, headers=admin)
    assert r.json()["store_bodies"] is False and r.json()["mask_pii"] is True  # partial update
    assert (await client.patch(f"/projects/{pid}", json={"mask_pii": None}, headers=admin)).json()[
        "mask_pii"
    ] is True
    assert (await client.get(f"/projects/{pid}", headers=admin)).json()["store_bodies"] is False
    async with api_app.state.resources.session_factory() as s:
        actions = [
            r[0]
            for r in await s.execute(
                text("SELECT action FROM audit_log WHERE action = 'project.update'")
            )
        ]
    assert len(actions) == 3


async def test_only_admins_who_are_members_can_change_project_settings(client, admin, project):
    await make_admin(client, admin, "viewer@example.com", "viewer")
    await client.post(
        f"/projects/{project['id']}/members", json={"email": "viewer@example.com"}, headers=admin
    )
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (
        await client.patch(f"/projects/{project['id']}", json={"mask_pii": True}, headers=viewer)
    ).status_code == 403
    await make_admin(client, admin, "other@example.com", "admin")
    other = bearer((await login(client, "other@example.com", GOOD_PASSWORD)).json())
    assert (
        await client.patch(f"/projects/{project['id']}", json={"mask_pii": True}, headers=other)
    ).status_code == 404
