"""Middleware, error format and JSON logging, tested on a tiny app without any resources."""

import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.core.errors import AppError, install_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import CorrelationIdMiddleware


class Item(BaseModel):
    qty: int


class Teapot(AppError):
    status_code = 418
    title = "I am a teapot"
    code = "teapot"


def make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        return {"a": "b"}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    @app.get("/teapot")
    async def teapot() -> None:
        raise Teapot("short and stout")

    @app.get("/forbidden")
    async def forbidden() -> None:
        raise HTTPException(403, "no")

    @app.post("/items")
    async def items(item: Item) -> Item:
        return item

    return app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=make_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_request_id_is_generated_and_returned(client):
    r = await client.get("/ok")
    assert len(r.headers["x-request-id"]) == 36


async def test_valid_request_id_is_kept(client):
    r = await client.get("/ok", headers={"x-request-id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"


async def test_unsafe_request_id_is_replaced(client):
    r = await client.get("/ok", headers={"x-request-id": 'bad"id\n{x}'})
    assert r.headers["x-request-id"] != 'bad"id\n{x}'
    assert len(r.headers["x-request-id"]) == 36


async def test_unhandled_error_is_problem_json_without_details(client):
    r = await client.get("/boom", headers={"x-request-id": "rid-1"})
    assert r.status_code == 500
    assert r.headers["content-type"] == "application/problem+json"
    body = r.json()
    assert body["status"] == 500 and body["correlation_id"] == "rid-1"
    assert "secret internal detail" not in r.text


async def test_app_error_maps_status_and_code(client):
    r = await client.get("/teapot")
    assert r.status_code == 418
    assert r.json()["code"] == "teapot" and r.json()["detail"] == "short and stout"


async def test_http_exception_is_problem_json(client):
    r = await client.get("/forbidden")
    assert r.status_code == 403 and r.json()["title"] == "no"


async def test_validation_error_hides_input_values(client):
    r = await client.post("/items", json={"qty": "my-password"})
    assert r.status_code == 422
    assert r.json()["errors"][0]["loc"] == ["body", "qty"]
    assert "my-password" not in r.text


async def test_logs_are_json_with_correlation_id(client, capsys):
    configure_logging("api", "test", "INFO")
    await client.get("/ok", headers={"x-request-id": "rid-42"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    entry = next(e for e in lines if e["event"] == "request")
    assert entry["correlation_id"] == "rid-42"
    assert entry["service"] == "api" and entry["env"] == "test"
    assert entry["level"] == "info" and entry["status"] == 200 and "timestamp" in entry
