"""Load scenarios. Started by scripts/load.py (headless), one user class at a time:

    locust -f loadtest/locustfile.py --headless -u 32 -r 32 -t 20s GatewayUser

DirectUser talks straight to the mock provider; GatewayUser sends the same request through the
gateway. The difference between the two is what the gateway costs. StreamUser holds streaming
connections open (the mock is told to delay, so each stream lasts a few seconds).
"""

import os

from locust import FastHttpUser, constant, task

GATEWAY = os.environ.get("GATEWAY_URL", "http://localhost:8000")
MOCK = os.environ.get("MOCK_URL", "http://localhost:8003")
KEY = os.environ.get("LOAD_KEY", "")
HOLD_MS = os.environ.get("STREAM_HOLD_MS", "3000")

BODY = {
    "model": "auto",
    "messages": [
        {"role": "user", "content": "Review: great product, would buy again. Labels: a, b, c"}
    ],
}
STREAM_BODY = {**BODY, "stream": True}


class DirectUser(FastHttpUser):
    """The provider alone."""

    host = MOCK
    wait_time = constant(0)

    @task
    def chat(self) -> None:
        body = {**BODY, "model": "mock-strong"}
        with self.client.post(
            "/v1/chat/completions", json=body, name="direct", catch_response=True
        ) as r:
            if r.status_code != 200:
                r.failure(f"http {r.status_code}")


class GatewayUser(FastHttpUser):
    """The same request through the gateway."""

    host = GATEWAY
    wait_time = constant(0)

    @task
    def chat(self) -> None:
        headers = {"authorization": f"Bearer {KEY}", "x-task-type": "load"}
        with self.client.post(
            "/v1/chat/completions", json=BODY, headers=headers, name="gateway", catch_response=True
        ) as r:
            if r.status_code != 200:
                r.failure(f"http {r.status_code}")


class StreamUser(FastHttpUser):
    """A long-lived stream: the mock is told to wait before it starts, so each stream keeps its
    connection open for a few seconds. The whole stream must arrive and end with [DONE]."""

    host = GATEWAY
    wait_time = constant(0)

    @task
    def stream(self) -> None:
        headers = {
            "authorization": f"Bearer {KEY}",
            "x-task-type": "load",
            "x-mock-delay-ms": HOLD_MS,
        }
        with self.client.post(
            "/v1/chat/completions", json=STREAM_BODY, headers=headers, name="stream", catch_response=True
        ) as r:
            if r.status_code != 200:
                r.failure(f"http {r.status_code}")
            elif "data: [DONE]" not in r.text:
                r.failure("stream ended without [DONE]")
