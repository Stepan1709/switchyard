"""Test-only app: the real application plus a slow endpoint, run under a real uvicorn."""

import asyncio

from app.api_main import create_app

app = create_app()


@app.get("/slow")
async def slow() -> dict[str, bool]:
    await asyncio.sleep(3)
    return {"done": True}
