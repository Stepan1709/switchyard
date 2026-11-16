"""The path of the product through the web interface, on the real api."""

import hashlib
import json
import time
import uuid

import httpx
import pytest

from tests.conftest import csrf_of, grab
from tests.integration.conftest import ADMIN_EMAIL, ADMIN_PASSWORD

pytestmark = pytest.mark.integration


async def login(
    browser: httpx.AsyncClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
):
    form = await browser.get("/login")
    return await browser.post(
        "/login",
        data={
            "email": email,
            "password": password,
            "csrf_token": csrf_of(form.text),
            "next": "/overview",
        },
    )


def token_of(html: str) -> dict[str, str]:
    return {"x-csrf-token": grab(r'hx-headers=\'\{"x-csrf-token": "([^"]+)"', html)}


async def open_page(browser: httpx.AsyncClient, path: str) -> httpx.Response:
    """GET a page, following redirects (a person's browser does)."""
    response = await browser.get(path)
    for _ in range(3):
        if response.status_code != 303:
            break
        response = await browser.get(response.headers["location"])
    return response


async def test_a_wrong_password_is_refused_by_the_real_api(browser):
    r = await login(browser, password="not-the-password-at-all")
    assert r.status_code == 401 and "Wrong email or password" in r.text


async def test_every_page_renders_against_the_real_api(browser):
    assert (await login(browser)).status_code == 303
    for path in (
        "/overview",
        "/matrix",
        "/models",
        "/runs",
        "/rules",
        "/usage",
        "/feedback",
        "/projects",
    ):
        r = await open_page(browser, path)
        assert r.status_code in (200, 404), (path, r.status_code, r.text[:300])
        assert "Traceback" not in r.text and "Something went wrong" not in r.text, path


async def test_a_person_builds_a_project_from_nothing_in_the_browser(browser):
    suffix = uuid.uuid4().hex[:8]
    assert (await login(browser)).status_code == 303
    page = await open_page(browser, "/projects")
    headers = token_of(page.text)

    # 1. a provider and a model
    r = await browser.post(
        "/models/providers",
        data={
            "name": f"local-{suffix}",
            "base_url": "http://localhost:9/v1",
            "auth_type": "none",
            "is_local": "1",
        },
        headers=headers,
    )
    assert r.status_code == 303
    models_page = await open_page(browser, "/models")
    assert f"local-{suffix}" in models_page.text
    provider_id = grab(r"/models/providers/([0-9a-f-]{36})/test", models_page.text)
    r = await browser.post(
        "/models/models",
        data={"provider_id": provider_id, "name": f"big-{suffix}", "context_window": "8192", "tier": "5",
              "cost_type": "per_token", "input_per_1m": "5", "output_per_1m": "15", "supports_stream": "1"},
        headers=headers,
    )  # fmt: skip
    assert r.status_code == 303
    models_page = await open_page(browser, "/models")
    assert f"big-{suffix}" in models_page.text and "$5.0000 in / $15.0000 out" in models_page.text

    # 2. a project with the model as its reference, and a key
    r = await browser.post(
        "/projects", data={"name": f"proj-{suffix}", "description": "e2e"}, headers=headers
    )
    assert r.status_code == 303
    projects = await open_page(browser, "/projects")
    assert f"proj-{suffix}" in projects.text
    model_id = grab(r'<option value="([0-9a-f-]{36})"[^>]*>big-' + suffix, projects.text)
    r = await browser.post(
        "/projects/settings",
        data={"description": "e2e", "store_bodies": "1", "reference_model_id": model_id},
        headers=headers,
    )
    assert r.status_code == 303
    r = await browser.post(
        "/projects/keys", data={"name": "prod", "monthly_budget_usd": "5"}, headers=headers
    )
    assert r.status_code == 303
    revealed = await open_page(browser, "/projects")
    key = grab(r"(swy_[A-Za-z0-9_\-]+)", revealed.text[revealed.text.index("was created") :])
    assert "shown only once" in revealed.text
    assert "shown only once" not in (await open_page(browser, "/projects")).text
    assert "$5.0000" in revealed.text or "5.0000" in revealed.text  # the budget of the key

    # 3. the empty states of the pages that follow
    assert "No traffic in" in (await open_page(browser, "/overview")).text
    assert "No task types yet" in (await open_page(browser, "/matrix")).text
    runs = await open_page(browser, "/runs")
    assert "No calibrations yet" in runs.text
    assert key.startswith("swy_")


async def test_a_viewer_can_look_but_the_real_api_would_refuse_changes_too(browser):
    suffix = uuid.uuid4().hex[:8]
    assert (await login(browser)).status_code == 303
    headers = token_of((await open_page(browser, "/projects")).text)
    email = f"viewer-{suffix}@example.com"
    r = await browser.post(
        "/admins",
        data={"email": email, "password": "a-long-viewer-password", "role": "viewer"},
        headers=headers,
    )
    assert r.status_code == 303
    await browser.post("/logout", headers=headers)
    browser.cookies.clear()

    assert (await login(browser, email, "a-long-viewer-password")).status_code == 303
    page = await open_page(browser, "/models")
    assert page.status_code == 200 and "Add a provider" not in page.text
    r = await browser.post(
        "/models/providers",
        data={"name": "x", "base_url": "http://localhost/v1"},
        headers=token_of(page.text),
    )
    assert r.status_code == 303
    assert "may look but not change" in (await open_page(browser, "/models")).text


async def test_the_token_is_renewed_through_the_real_api(browser, web):
    assert (await login(browser)).status_code == 303
    redis = web.state.redis
    key = "web:session:" + hashlib.sha256(browser.cookies["swy_session"].encode()).hexdigest()
    data = json.loads(await redis.get(key))
    assert data["access_token"]
    old_refresh = data["refresh_token"]
    data["access_expires_at"] = time.time() - 10
    await redis.set(key, json.dumps(data), keepttl=True)
    r = await open_page(browser, "/models")
    assert r.status_code == 200
    renewed = json.loads(await redis.get(key))
    assert renewed["refresh_token"] != old_refresh  # the api rotated it
    assert renewed["access_expires_at"] > time.time()


async def test_logging_out_makes_the_old_cookie_useless(browser):
    assert (await login(browser)).status_code == 303
    page = await open_page(browser, "/models")
    cookie = browser.cookies["swy_session"]
    await browser.post("/logout", headers=token_of(page.text))
    browser.cookies.clear()
    browser.cookies.set("swy_session", cookie)
    r = await browser.get("/models")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
