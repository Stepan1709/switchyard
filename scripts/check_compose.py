"""Fail if any compose service lacks log rotation or a healthcheck-friendly restart policy."""

import json
import subprocess
import sys

# One-shot containers (migrations) must not be restarted after they finish.
ONE_SHOT = {"migrate"}


def main() -> int:
    files = sys.argv[1:]
    cmd = ["docker", "compose", "--env-file", ".env"]
    for f in files:
        cmd += ["-f", f]
    out = subprocess.run(
        [*cmd, "config", "--format", "json"], check=True, capture_output=True, text=True
    )
    services = json.loads(out.stdout)["services"]
    bad = []
    for name, svc in services.items():
        opts = (svc.get("logging") or {}).get("options") or {}
        if "max-size" not in opts or "max-file" not in opts:
            bad.append(f"{name}: no log rotation")
        expected = "no" if name in ONE_SHOT else "unless-stopped"
        if svc.get("restart", "no" if name in ONE_SHOT else None) != expected:
            bad.append(f"{name}: restart policy is not {expected}")
    if bad:
        print("\n".join(bad))
        return 1
    print(f"ok: {len(services)} services")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
