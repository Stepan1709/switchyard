"""Providers, models, prices, connection tests and SSRF protection through the real API."""

import ipaddress
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.crypto import DecryptionError
from tests.integration.conftest import MOCK_KEY, MockServer
from tests.integration.test_auth_api import GOOD_PASSWORD, bearer, login, make_admin

pytestmark = pytest.mark.integration


@pytest.fixture
async def root(client):
    return bearer((await login(client)).json())


@pytest.fixture
async def provider(client, root, mock_llm: MockServer):
    r = await client.post(
        "/providers",
        json={
            "name": "mock",
            "base_url": mock_llm.base_url,
            "auth_type": "bearer",
            "is_local": True,
            "api_key": MOCK_KEY,
        },
        headers=root,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def add_model(client, root, provider, name="mock-tiny", **kw):
    body = {"provider_id": provider["id"], "name": name, "context_window": 4096, **kw}
    r = await client.post("/models", json=body, headers=root)
    assert r.status_code == 201, r.text
    return r.json()


async def all_text(api_app, query: str) -> str:
    async with api_app.state.resources.session_factory() as s:
        return str((await s.execute(text(query))).all())


# ---- providers and the access key ----
async def test_provider_key_is_masked_everywhere_in_the_api(client, root, provider):
    assert provider["api_key_mask"] == "****" + MOCK_KEY[-4:]
    pid = provider["id"]
    bodies = [
        (await client.get(f"/providers/{pid}", headers=root)).text,
        (await client.get("/providers", headers=root)).text,
        (await client.patch(f"/providers/{pid}", json={"enabled": True}, headers=root)).text,
    ]
    for body in bodies:
        assert MOCK_KEY not in body and "api_key_encrypted" not in body and '"api_key"' not in body


async def test_provider_key_is_encrypted_in_the_database(api_app, provider):
    row = await all_text(api_app, "SELECT api_key_encrypted, api_key_mask FROM providers")
    assert MOCK_KEY not in row
    assert "v1.k2." in row  # encrypted with the active (first) master key
    keyring = api_app.state.keyring
    async with api_app.state.resources.session_factory() as s:
        token = (await s.execute(text("SELECT api_key_encrypted FROM providers"))).scalar_one()
    assert keyring.decrypt(token, aad=provider["id"]) == MOCK_KEY
    with pytest.raises(DecryptionError):  # bound to its row: cannot be moved to another provider
        keyring.decrypt(token, aad="00000000-0000-0000-0000-000000000000")


async def test_old_master_key_values_still_decrypt(api_app, provider):
    # A value written with the older key k1 (before a rotation) must remain readable.
    from app.core.crypto import KeyRing
    from tests.integration.conftest import MASTER_KEYS

    k1_only = KeyRing.from_text(MASTER_KEYS.splitlines()[1])
    token = k1_only.encrypt("old-secret", aad=provider["id"])
    assert api_app.state.keyring.decrypt(token, aad=provider["id"]) == "old-secret"


async def test_provider_key_never_reaches_logs_or_audit(client, root, api_app, mock_llm, capsys):
    secret = "sk-very-secret-key-do-not-log-1234"
    r = await client.post(
        "/providers",
        json={
            "name": "leaky",
            "base_url": mock_llm.base_url,
            "is_local": True,
            "api_key": secret,
        },
        headers=root,
    )
    pid = r.json()["id"]
    await client.post(f"/providers/{pid}/test", headers=root)  # sends the key to the mock
    await client.patch(f"/providers/{pid}", json={"api_key": secret + "-rotated"}, headers=root)
    assert secret not in capsys.readouterr().out  # application logs
    dump = await all_text(api_app, "SELECT details, ip, action, target_id FROM audit_log")
    assert secret not in dump


async def test_create_validation_rules(client, root, mock_llm):
    base = {"name": "p", "base_url": mock_llm.base_url, "is_local": True}
    no_key = await client.post("/providers", json={**base, "auth_type": "bearer"}, headers=root)
    assert no_key.status_code == 422  # bearer needs a key
    none_with_key = await client.post(
        "/providers", json={**base, "auth_type": "none", "api_key": "x"}, headers=root
    )
    assert none_with_key.status_code == 422
    ok = await client.post("/providers", json={**base, "auth_type": "none"}, headers=root)
    assert ok.status_code == 201 and ok.json()["api_key_mask"] is None
    dup = await client.post("/providers", json={**base, "auth_type": "none"}, headers=root)
    assert dup.status_code == 409


async def test_update_rotate_and_clear_key(client, root, provider, mock_llm):
    pid = provider["id"]
    rotated = await client.patch(
        f"/providers/{pid}", json={"api_key": "another-key-5678"}, headers=root
    )
    assert rotated.json()["api_key_mask"] == "****5678"
    # the stored key was replaced, so the mock (which wants the old one) now rejects it
    bad = (await client.post(f"/providers/{pid}/test", headers=root)).json()
    assert bad["ok"] is False and bad["models_endpoint"]["error"] == "authentication_failed"
    # the key is required for bearer providers, so it cannot simply be cleared
    cleared = await client.patch(f"/providers/{pid}", json={"clear_api_key": True}, headers=root)
    assert cleared.status_code == 422
    both = await client.patch(
        f"/providers/{pid}", json={"clear_api_key": True, "auth_type": "none"}, headers=root
    )
    assert both.status_code == 200 and both.json()["api_key_mask"] is None


async def test_delete_provider_rules(client, root, provider):
    model = await add_model(client, root, provider)
    pid = provider["id"]
    assert (await client.delete(f"/providers/{pid}", headers=root)).status_code == 409
    del model
    other = await client.post(
        "/providers",
        json={
            "name": "empty",
            "base_url": provider["base_url"],
            "is_local": True,
            "auth_type": "none",
        },
        headers=root,
    )
    assert (
        await client.delete(f"/providers/{other.json()['id']}", headers=root)
    ).status_code == 204


async def test_viewer_can_read_but_not_change_the_registry(client, root, provider):
    await make_admin(client, root, "viewer@example.com", "viewer")
    viewer = bearer((await login(client, "viewer@example.com", GOOD_PASSWORD)).json())
    assert (await client.get("/providers", headers=viewer)).status_code == 200
    assert (await client.get("/models", headers=viewer)).status_code == 200
    pid = provider["id"]
    assert (await client.post("/providers", json={}, headers=viewer)).status_code == 403
    assert (await client.post(f"/providers/{pid}/test", headers=viewer)).status_code == 403
    assert (await client.delete(f"/providers/{pid}", headers=viewer)).status_code == 403
    assert (await client.get("/providers")).status_code == 401


# ---- connection test ----
async def test_connection_test_against_the_mock_provider(client, root, provider):
    await add_model(client, root, provider, "mock-tiny")
    pid = provider["id"]
    result = (await client.post(f"/providers/{pid}/test", headers=root)).json()
    assert result["ok"] is True and result["model"] == "mock-tiny"
    assert (
        result["models_endpoint"]["ok"] and result["models_endpoint"]["detail"] == "4 models listed"
    )
    assert result["completion"]["ok"] and result["completion"]["status_code"] == 200
    assert isinstance(result["models_endpoint"]["latency_ms"], int)
    assert isinstance(result["completion"]["latency_ms"], int)

    stored = (await client.get(f"/providers/{pid}", headers=root)).json()
    assert stored["last_check_ok"] is True and stored["last_check_error"] is None
    assert stored["last_check_at"] is not None and stored["last_check_latency_ms"] is not None


async def test_connection_test_without_models_only_checks_the_model_list(client, root, provider):
    result = (await client.post(f"/providers/{provider['id']}/test", headers=root)).json()
    assert result["ok"] is True and result["completion"] is None and result["model"] is None


async def test_connection_test_with_an_explicit_unknown_model(client, root, provider):
    result = (
        await client.post(
            f"/providers/{provider['id']}/test", json={"model": "gpt-does-not-exist"}, headers=root
        )
    ).json()
    assert result["ok"] is False
    assert result["models_endpoint"]["ok"] is True
    assert result["completion"]["error"] == "http_404"
    stored = (await client.get(f"/providers/{provider['id']}", headers=root)).json()
    assert stored["last_check_ok"] is False and stored["last_check_error"] == "http_404"


async def test_wrong_key_is_reported_as_authentication_failure(client, root, mock_llm):
    r = await client.post(
        "/providers",
        json={
            "name": "badkey",
            "base_url": mock_llm.base_url,
            "is_local": True,
            "api_key": "wrong",
        },
        headers=root,
    )
    result = (await client.post(f"/providers/{r.json()['id']}/test", headers=root)).json()
    assert result["ok"] is False
    assert result["models_endpoint"]["error"] == "authentication_failed"
    assert "wrong" not in str(result)


async def test_unreachable_provider_is_a_clean_failure(client, root, mock_llm):
    dead = f"http://localhost:{mock_llm.port + 1}/v1"  # nothing listens there
    r = await client.post(
        "/providers",
        json={"name": "dead", "base_url": dead, "is_local": True, "auth_type": "none"},
        headers=root,
    )
    result = (await client.post(f"/providers/{r.json()['id']}/test", headers=root)).json()
    assert result["ok"] is False and result["models_endpoint"]["error"] == "connection_failed"


# ---- SSRF ----
@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://api.example.com/v1", False),  # non-local must use https
        ("file:///etc/passwd", False),
        ("https://user:pass@api.example.com/v1", False),
        ("https://169.254.169.254/latest/meta-data", False),  # cloud metadata
        ("https://[::ffff:169.254.169.254]/", False),
        ("https://127.0.0.1/v1", False),  # loopback is private: needs a local provider
        ("https://10.0.0.5/v1", False),
        ("https://metadata.google.internal/", False),
        ("http://169.254.169.254/v1", True),  # even local providers cannot use metadata
        ("http://internal-db.corp:5432/v1", True),  # not on the allowlist
    ],
)
async def test_forbidden_provider_addresses_are_rejected(client, root, url, local):
    r = await client.post(
        "/providers",
        json={"name": "evil", "base_url": url, "is_local": local, "auth_type": "none"},
        headers=root,
    )
    assert r.status_code == 422 and r.json()["code"] == "ssrf_blocked", r.text
    assert (await client.get("/providers", headers=root)).json() == []  # nothing was saved


async def test_metadata_address_is_rejected_even_when_someone_allowlists_it(
    client, root, app_settings, api_app
):
    api_app.state.settings.local_provider_hosts = ["169.254.169.254"]
    r = await client.post(
        "/providers",
        json={
            "name": "meta",
            "base_url": "http://169.254.169.254/v1",
            "is_local": True,
            "auth_type": "none",
        },
        headers=root,
    )
    assert r.status_code == 422 and "forbidden" in r.json()["detail"]


async def test_update_to_a_forbidden_address_is_rejected_and_nothing_changes(
    client, root, provider
):
    pid = provider["id"]
    r = await client.patch(
        f"/providers/{pid}", json={"base_url": "http://169.254.169.254/v1"}, headers=root
    )
    assert r.status_code == 422
    assert (await client.get(f"/providers/{pid}", headers=root)).json()["base_url"] == provider[
        "base_url"
    ]
    # turning a provider into a non-local one re-checks the current address as well
    flip = await client.patch(f"/providers/{pid}", json={"is_local": False}, headers=root)
    assert flip.status_code == 422


async def test_dns_rebinding_is_caught_when_the_connection_is_made(client, root, api_app):
    """The host name is public when the provider is saved and private when it is called."""
    answers = [["93.184.216.34"], ["127.0.0.1"]]

    async def rebinding_resolver(host, port):
        chosen = answers[0] if len(answers) == 1 else answers.pop(0)
        return [ipaddress.ip_address(a) for a in chosen]

    api_app.state.ssrf_resolver = rebinding_resolver
    created = await client.post(
        "/providers",
        json={"name": "rebind", "base_url": "https://rebind.example.com/v1", "auth_type": "none"},
        headers=root,
    )
    assert created.status_code == 201  # public at save time
    result = (await client.post(f"/providers/{created.json()['id']}/test", headers=root)).json()
    assert result["ok"] is False
    assert result["models_endpoint"]["error"] == "address_blocked"  # private at call time


# ---- models ----
async def test_model_crud_and_validation(client, root, provider):
    model = await add_model(
        client, root, provider, "mock-mid", tier=2, context_window=16384, max_output_tokens=1024,
        supports_tools=True, supports_json=True, idle_threshold_pct="15.5",
    )  # fmt: skip
    assert (
        model["tier"] == 2
        and model["supports_stream"] is True
        and model["cost_type"] == "per_token"
    )
    assert model["idle_threshold_pct"] == "15.50" or model["idle_threshold_pct"] == "15.5"

    mid = f"/models/{model['id']}"
    patched = (await client.patch(mid, json={"tier": 3, "enabled": False}, headers=root)).json()
    assert (
        patched["tier"] == 3 and patched["enabled"] is False and patched["context_window"] == 16384
    )
    cleared = (await client.patch(mid, json={"idle_threshold_pct": None}, headers=root)).json()
    assert cleared["idle_threshold_pct"] is None

    by_provider = (await client.get(f"/models?provider_id={provider['id']}", headers=root)).json()
    assert [m["name"] for m in by_provider] == ["mock-mid"]
    assert (
        await client.get(
            "/models?provider_id=" + "0" * 8 + "-0000-0000-0000-" + "0" * 12, headers=root
        )
    ).json() == []


@pytest.mark.parametrize(
    "body",
    [
        {"tier": 0},
        {"tier": 6},
        {"context_window": 0},
        {"max_output_tokens": 9999},  # larger than the context window
        {"idle_threshold_pct": 101},
        {"cost_type": "free"},
        {"name": ""},
    ],
)
async def test_invalid_models_are_rejected(client, root, provider, body):
    payload = {"provider_id": provider["id"], "name": "m", "context_window": 4096, **body}
    assert (await client.post("/models", json=payload, headers=root)).status_code == 422


async def test_model_names_are_unique_per_provider_and_provider_must_exist(client, root, provider):
    await add_model(client, root, provider, "same")
    dup = await client.post(
        "/models",
        json={"provider_id": provider["id"], "name": "same", "context_window": 10},
        headers=root,
    )
    assert dup.status_code == 409
    ghost = await client.post(
        "/models",
        json={
            "provider_id": "00000000-0000-0000-0000-000000000000",
            "name": "x",
            "context_window": 10,
        },
        headers=root,
    )
    assert ghost.status_code == 404


# ---- price history ----
async def test_price_history_is_kept_and_intervals_chain(client, root, provider):
    model = await add_model(client, root, provider)
    url = f"/models/{model['id']}/prices"
    first = (
        await client.post(url, json={"input_per_1m": "0.15", "output_per_1m": "0.60"}, headers=root)
    ).json()
    assert first["valid_to"] is None and first["input_per_1m"] == "0.150000"

    second = (
        await client.post(url, json={"input_per_1m": "0.10", "output_per_1m": "0.40"}, headers=root)
    ).json()
    history = (await client.get(url, headers=root)).json()
    assert [p["id"] for p in history] == [second["id"], first["id"]]  # newest first
    assert history[1]["valid_to"] == second["valid_from"]  # the old interval was closed exactly
    assert history[0]["valid_to"] is None
    assert history[1]["input_per_1m"] == "0.150000"  # the old price is preserved


async def test_price_at_returns_the_version_valid_at_that_time(client, root, provider):
    model = await add_model(client, root, provider)
    url = f"/models/{model['id']}/prices"
    t0 = datetime.now(UTC) - timedelta(days=10)
    t1 = datetime.now(UTC) - timedelta(days=5)
    await client.post(
        url,
        json={"effective_from": t0.isoformat(), "input_per_1m": "1", "output_per_1m": "2"},
        headers=root,
    )
    await client.post(
        url,
        json={"effective_from": t1.isoformat(), "input_per_1m": "3", "output_per_1m": "4"},
        headers=root,
    )

    def at(when):
        return client.get(
            f"/models/{model['id']}/price", params={"at": when.isoformat()}, headers=root
        )

    assert (await at(t0 + timedelta(days=1))).json()["input_per_1m"] == "1.000000"
    assert (await at(t1 + timedelta(days=1))).json()["input_per_1m"] == "3.000000"
    assert (await client.get(f"/models/{model['id']}/price", headers=root)).json()[
        "input_per_1m"
    ] == "3.000000"
    assert (await at(t0 - timedelta(days=1))).status_code == 404  # before any price existed


async def test_a_scheduled_price_does_not_apply_yet(client, root, provider):
    model = await add_model(client, root, provider)
    url = f"/models/{model['id']}/prices"
    await client.post(url, json={"input_per_1m": "1", "output_per_1m": "2"}, headers=root)
    future = datetime.now(UTC) + timedelta(days=30)
    await client.post(
        url,
        json={"effective_from": future.isoformat(), "input_per_1m": "9", "output_per_1m": "9"},
        headers=root,
    )
    now = (await client.get(f"/models/{model['id']}/price", headers=root)).json()
    assert now["input_per_1m"] == "1.000000"  # still the old price today
    later = await client.get(
        f"/models/{model['id']}/price",
        params={"at": (future + timedelta(days=1)).isoformat()},
        headers=root,
    )
    assert later.json()["input_per_1m"] == "9.000000"


async def test_price_validation(client, root, provider):
    model = await add_model(client, root, provider)
    url = f"/models/{model['id']}/prices"
    for body in (
        {"input_per_1m": "1"},  # output missing
        {"monthly_cost_usd": "100"},  # wrong kind for a per_token model
        {"input_per_1m": "1", "output_per_1m": "2", "monthly_cost_usd": "5"},
        {"input_per_1m": "-1", "output_per_1m": "2"},
        {
            "input_per_1m": "1",
            "output_per_1m": "2",
            "effective_from": "2026-01-01T00:00:00",
        },  # no zone
    ):
        assert (await client.post(url, json=body, headers=root)).status_code == 422, body
    ok = await client.post(url, json={"input_per_1m": "1", "output_per_1m": "2"}, headers=root)
    assert ok.status_code == 201
    older = datetime.now(UTC) - timedelta(days=1)
    stale = await client.post(
        url,
        json={"effective_from": older.isoformat(), "input_per_1m": "1", "output_per_1m": "2"},
        headers=root,
    )
    assert stale.status_code == 422  # cannot start before the current version


async def test_fixed_monthly_models_are_priced_per_month(client, root, provider):
    model = await add_model(client, root, provider, "own-gpu", cost_type="fixed_monthly")
    url = f"/models/{model['id']}/prices"
    assert (
        await client.post(url, json={"input_per_1m": "1", "output_per_1m": "2"}, headers=root)
    ).status_code == 422
    ok = await client.post(url, json={"monthly_cost_usd": "450.00"}, headers=root)
    assert ok.status_code == 201 and ok.json()["monthly_cost_usd"] == "450.00"
    assert ok.json()["input_per_1m"] is None


async def test_database_allows_only_one_current_price_per_model(client, root, provider, api_app):
    model = await add_model(client, root, provider)
    await client.post(
        f"/models/{model['id']}/prices",
        json={"input_per_1m": "1", "output_per_1m": "2"},
        headers=root,
    )
    async with api_app.state.resources.session_factory() as s:
        with pytest.raises(IntegrityError):  # a second open interval violates the partial index
            await s.execute(
                text(
                    "INSERT INTO model_prices"
                    " (id, model_id, valid_from, input_per_1m, output_per_1m)"
                    " VALUES (gen_random_uuid(), :m, now(), 1, 1)"
                ),
                {"m": model["id"]},
            )
            await s.commit()


# ---- reference model ----
async def test_reference_model_of_a_project(client, root, provider):
    model = await add_model(client, root, provider, "mock-strong")
    project = (await client.post("/projects", json={"name": "shop"}, headers=root)).json()
    assert project["reference_model_id"] is None
    url = f"/projects/{project['id']}/reference-model"

    set_ = await client.put(url, json={"model_id": model["id"]}, headers=root)
    assert set_.status_code == 200 and set_.json()["reference_model_id"] == model["id"]
    assert (await client.get(f"/projects/{project['id']}", headers=root)).json()[
        "reference_model_id"
    ] == model["id"]

    unknown = await client.put(
        url, json={"model_id": "00000000-0000-0000-0000-000000000000"}, headers=root
    )
    assert unknown.status_code == 404
    await client.patch(f"/models/{model['id']}", json={"enabled": False}, headers=root)
    assert (await client.put(url, json={"model_id": model["id"]}, headers=root)).status_code == 422
    cleared = await client.put(url, json={"model_id": None}, headers=root)
    assert cleared.json()["reference_model_id"] is None


async def test_reference_model_respects_project_membership(client, root, provider):
    model = await add_model(client, root, provider)
    project = (await client.post("/projects", json={"name": "private"}, headers=root)).json()
    await make_admin(client, root, "other@example.com", "admin")
    other = bearer((await login(client, "other@example.com", GOOD_PASSWORD)).json())
    r = await client.put(
        f"/projects/{project['id']}/reference-model", json={"model_id": model["id"]}, headers=other
    )
    assert r.status_code == 404  # not a member: the project does not exist for them


async def test_registry_actions_are_audited(client, root, provider, api_app):
    model = await add_model(client, root, provider)
    await client.post(
        f"/models/{model['id']}/prices",
        json={"input_per_1m": "1", "output_per_1m": "2"},
        headers=root,
    )
    await client.post(f"/providers/{provider['id']}/test", headers=root)
    actions = await all_text(api_app, "SELECT action FROM audit_log")
    for expected in ("provider.create", "model.create", "model.price_set", "provider.test"):
        assert expected in actions
