"""Login, sessions, CSRF, token refresh."""

import asyncio
import json
import time

import httpx

from tests.conftest import FakeApi, FakeRedis, csrf_header, csrf_of, sign_in, tokens, world


async def test_a_page_needs_a_login_and_remembers_where_you_wanted_to_go(client):
    r = await client.get("/overview")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/overview"


async def test_an_htmx_request_without_a_login_gets_a_redirect_header(client):
    r = await client.get(
        "/matrix/cell",
        headers={"hx-request": "true"},
        params={"run_id": "x", "model_id": "y", "task_type": "z"},
    )
    assert r.status_code == 200 and r.headers["hx-redirect"] == "/login"


async def test_the_login_form_carries_a_csrf_token_and_sets_an_anonymous_cookie(client, redis):
    r = await client.get("/login")
    assert r.status_code == 200 and csrf_of(r.text)
    assert "httponly" in r.headers["set-cookie"].lower()
    assert not any(s.get("access_token") for s in redis.sessions().values())


async def test_a_good_login_keeps_tokens_in_redis_and_only_an_id_in_the_cookie(client, redis, api):
    r = await sign_in(client)
    assert r.status_code == 303 and r.headers["location"] == "/overview"
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
    session_id = client.cookies["swy_session"]
    assert "access-1" not in cookie and "refresh-1" not in cookie
    stored = [s for s in redis.sessions().values() if s.get("access_token")]
    assert len(stored) == 1
    assert stored[0]["access_token"] == "access-1" and stored[0]["role"] == "admin"
    # The redis key is a hash of the cookie, not the cookie itself.
    assert not any(session_id in key for key in redis.data)


async def test_the_session_id_changes_at_login(client, redis):
    first = await client.get("/login")
    anonymous_id = client.cookies["swy_session"]
    await client.post(
        "/login",
        data={
            "email": "a@example.com",
            "password": "pw",
            "csrf_token": csrf_of(first.text),
            "next": "/",
        },
    )
    assert client.cookies["swy_session"] != anonymous_id


async def test_a_wrong_password_shows_a_message_and_logs_nobody_in(client, api, redis):
    api.on("POST", "/auth/login", {"title": "Unauthorized", "detail": "Invalid credentials"}, 401)
    r = await sign_in(client)
    assert r.status_code == 401 and "Wrong email or password" in r.text
    assert not any(s.get("access_token") for s in redis.sessions().values())


async def test_too_many_attempts_is_explained(client, api):
    api.on("POST", "/auth/login", {"title": "Too many requests", "detail": "slow down"}, 429)
    r = await sign_in(client)
    assert r.status_code == 429 and "Too many attempts" in r.text


async def test_a_login_with_a_wrong_csrf_token_is_refused(client):
    await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": "a@example.com", "password": "pw", "csrf_token": "forged", "next": "/"},
    )
    assert r.status_code == 400 and "expired" in r.text


async def test_the_next_address_must_be_on_this_site(client):
    r = await sign_in(client, next_path="https://evil.example/phish")
    assert r.status_code == 303 and r.headers["location"] == "/overview"
    client.cookies.clear()
    r = await sign_in(client, next_path="//evil.example")
    assert r.headers["location"] == "/overview"


async def test_logout_destroys_the_session(session, redis, api):
    headers = csrf_header(session, redis)
    r = await session.post("/logout", headers=headers)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert api.calls_to("POST", "/auth/logout")
    assert not any(s.get("access_token") for s in redis.sessions().values())
    again = await session.get("/overview")
    assert again.status_code == 303 and again.headers["location"].startswith("/login")


async def test_a_post_without_the_csrf_token_is_refused(session):
    r = await session.post("/project/select", data={"project_id": "x"})
    assert r.status_code == 403


async def test_a_post_from_another_origin_is_refused_even_with_the_token(session, redis):
    headers = {**csrf_header(session, redis), "origin": "https://evil.example"}
    r = await session.post("/project/select", data={"project_id": "x"}, headers=headers)
    assert r.status_code == 403


async def test_the_form_token_works_as_well_as_the_header(session, redis):
    token = csrf_header(session, redis)["x-csrf-token"]
    r = await session.post(
        "/project/select",
        data={
            "project_id": "11111111-1111-1111-1111-111111111111",
            "csrf_token": token,
            "next": "/matrix",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/matrix"


# ---- token refresh -----------------------------------------------------------------------------
def _expire_access_token(redis: FakeRedis) -> None:
    for key, raw in list(redis.data.items()):
        data = json.loads(raw)
        if data.get("access_token"):
            data["access_expires_at"] = time.time() - 5
            redis.data[key] = json.dumps(data)


async def test_an_expiring_token_is_renewed_before_the_call(session, redis, api):
    api.on("POST", "/auth/refresh", tokens("access-2", "refresh-2"))
    api.on("GET", "/projects/11111111-1111-1111-1111-111111111111/task-types", [])
    _expire_access_token(redis)
    r = await session.get("/rules")
    assert r.status_code == 200
    refresh = api.calls_to("POST", "/auth/refresh")
    assert len(refresh) == 1 and json.loads(refresh[0].content) == {"refresh_token": "refresh-1"}
    last = api.calls_to("GET", "/models")[-1]
    assert last.headers["authorization"] == "Bearer access-2"
    stored = next(s for s in redis.sessions().values() if s.get("access_token"))
    assert stored["refresh_token"] == "refresh-2"


async def test_parallel_requests_refresh_the_token_only_once(session, redis, api):
    # The api treats a reused refresh token as theft, so two requests must not both use it.
    calls = []

    def refresh(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=tokens("access-2", "refresh-2"))

    api.on_call("POST", "/auth/refresh", refresh)
    api.on("GET", "/projects/11111111-1111-1111-1111-111111111111/task-types", [])
    _expire_access_token(redis)
    results = await asyncio.gather(
        session.get("/rules"), session.get("/rules"), session.get("/rules")
    )
    assert [r.status_code for r in results] == [200, 200, 200]
    assert len(calls) == 1


async def test_when_the_refresh_token_is_rejected_the_person_logs_in_again(session, redis, api):
    api.on(
        "POST", "/auth/refresh", {"title": "Unauthorized", "detail": "Invalid refresh token"}, 401
    )
    _expire_access_token(redis)
    r = await session.get("/models")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert "max-age=0" in r.headers["set-cookie"].lower()  # the stale cookie is cleared


async def test_a_rejected_access_token_is_renewed_once_and_the_call_repeated(session, api):
    seen = []

    def models(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        if len(seen) == 1:
            return httpx.Response(401, json={"title": "Unauthorized", "detail": "expired"})
        return httpx.Response(200, json=[])

    api.on_call("GET", "/models", models)
    api.on("POST", "/auth/refresh", tokens("access-2", "refresh-2"))
    api.on("GET", "/providers", [])
    r = await session.get("/models")
    assert r.status_code == 200
    assert seen[:2] == ["Bearer access-1", "Bearer access-2"]


async def test_world_helper_is_reusable():
    fake = FakeApi()
    world(fake, role="viewer")
    assert ("GET", "/auth/me") in fake.routes


# ---- the visitor's address behind a proxy ------------------------------------------------------
async def test_the_visitors_address_is_passed_to_the_api_only_when_the_proxy_is_trusted(api, redis):
    import httpx
    from asgi_lifespan import LifespanManager

    from app.config import Settings
    from app.main import create_app

    for trust, expected in ((True, "203.0.113.7"), (False, None)):
        http = httpx.AsyncClient(transport=httpx.MockTransport(api.handle), base_url="http://api")
        app = create_app(
            Settings(cookie_secure=False, api_url="http://api", trust_forwarded_for=trust),
            redis=redis,
            api_http=http,
        )
        async with (
            LifespanManager(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web") as c,
        ):
            form = await c.get("/login")
            api.calls.clear()
            await c.post(
                "/login",
                data={
                    "email": "a@example.com",
                    "password": "pw",
                    "csrf_token": csrf_of(form.text),
                    "next": "/",
                },
                headers={"x-forwarded-for": "203.0.113.7, 10.0.0.1"},
            )
        login = api.calls_to("POST", "/auth/login")[0]
        assert login.headers.get("x-forwarded-for") == expected
        await http.aclose()


async def test_a_forwarded_value_that_is_not_an_address_is_ignored():
    from types import SimpleNamespace

    from app.ui import visitor_address

    request = SimpleNamespace(headers={"x-forwarded-for": "not-an-address"})
    assert visitor_address(request, True) is None  # type: ignore[arg-type]
