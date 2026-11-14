"""Test-only worker: the real runtime with one consumer that records what it does.

Payload options: {"sleep": seconds, "fail_times": n}. Tables echo_started / echo_results are
created by the test.
"""

import asyncio
import os

from sqlalchemy import text

from app.core.config import Settings
from app.core.inbox import process_once
from app.core.messaging import ConsumerSpec, Message, TemporaryError
from app.core.resources import Resources
from app.worker.runtime import main

QUEUE = os.environ["ECHO_QUEUE"]


def build(res: Resources, settings: Settings) -> list[ConsumerSpec]:
    async def handler(message: Message) -> None:
        async with res.session_factory() as s, s.begin():
            await s.execute(
                text("INSERT INTO echo_started (message_id, pid, attempt) VALUES (:m, :p, :a)"),
                {"m": message.message_id, "p": os.getpid(), "a": message.attempt},
            )
        await asyncio.sleep(float(message.payload.get("sleep", 0)))
        if message.attempt < int(message.payload.get("fail_times", 0)):
            raise TemporaryError("not yet")

        async def work(session):
            await session.execute(
                text("INSERT INTO echo_results (message_id, pid) VALUES (:m, :p)"),
                {"m": message.message_id, "p": os.getpid()},
            )

        await process_once(res.session_factory, "echo", message, work)

    return [ConsumerSpec(QUEUE, ["echo.event"], handler)]


if __name__ == "__main__":
    main(build)
