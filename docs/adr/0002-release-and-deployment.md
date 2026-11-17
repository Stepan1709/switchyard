# 2. Release and deployment

## Status

Accepted.

## Context

The project has to be easy to run on a small server by someone who only has the repository and
a Docker host, and a release has to be repeatable and reversible. Building images on the server
would need a toolchain there and make every deploy slow.

## Decision

- **Images are built in CI** and published to GHCR for `linux/amd64` and `linux/arm64` when a
  tag `vX.Y.Z` is pushed on `main`. The tag is the image tag: the version that runs is the one
  written in `deploy/prod/.env`.
- **The server holds a clone of the repository** and pulls images. `deploy/prod/deploy.sh`
  checks out the tag (the compose and nginx files belong to the version), pulls the images,
  takes a database dump, starts the stack and waits for the health checks. If they fail it goes
  back to the previous version by itself.
- **Migrations only add** (a new column, a new table). That is what makes going back to the
  previous version safe without rolling the database back; the dump taken before the upgrade is
  the way back for the data.
- **One entry point, nginx**, terminates TLS. Without a domain it serves a self-signed
  certificate; with one, certbot obtains and renews a Let's Encrypt certificate. nginx sets the
  client address itself, which is what allows the services to trust `X-Forwarded-For`
  (`TRUST_FORWARDED_FOR`, off by default).
- **Containers are read-only** with no capabilities; secrets are files outside the repository.
- **Backups** are daily `pg_dump` files in a volume; a restore into a scratch database with a
  comparison against the live one runs in CI, so a backup that cannot be restored is noticed.
- **Deploying from GitHub is opt-in.** The deploy job runs only when a repository variable and
  the SSH secrets exist; without them a release only publishes the images.

## Consequences

- A new server needs `setup.sh`, a version and (optionally) a domain, nothing else.
- Releases are tags on `main`, so the artifacts always correspond to reviewed code.
- A rollback is a command, but a release that needs a destructive migration would need its own
  plan: that has to be an explicit exception.
- Actions are pinned to commit hashes; Dependabot proposes updates.
