"""Check graceful shutdown of the running api container.

Runs `docker compose stop api` and verifies that it stops within a few seconds with a clean
exit code (not SIGKILL after the 30 s grace period). Requires the stack from `make up`.
Finishing in-flight requests on SIGTERM is covered by the integration test
tests/integration/test_graceful_shutdown.py.
"""

import subprocess
import sys
import time

COMPOSE = [
    "docker", "compose", "--env-file", ".env",
    "-f", "deploy/compose.yml", "-f", "deploy/compose.dev.yml",
]  # fmt: skip
MAX_STOP_SECONDS = 10


def main() -> int:
    started = time.monotonic()
    result = subprocess.run([*COMPOSE, "stop", "api"], capture_output=True, text=True)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        print(result.stderr)
        return 1
    print(f"api stopped in {elapsed:.1f}s")
    if elapsed > MAX_STOP_SECONDS:
        print(f"FAIL: stop took longer than {MAX_STOP_SECONDS}s")
        return 1
    code = subprocess.run(
        [*COMPOSE, "ps", "-a", "--format", "{{.ExitCode}}", "api"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    # 0 or 143 (128 + SIGTERM) is a clean stop; 137 would mean SIGKILL after the grace period.
    if code not in {"0", "143"}:
        print(f"FAIL: unexpected exit code {code!r}")
        return 1
    print(f"OK: exit code {code}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
