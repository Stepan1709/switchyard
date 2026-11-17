#!/bin/sh
# Run a version: pull its images, take a dump of the database, start it, and go back to the
# previous version if it does not become healthy.
#
#   deploy/prod/deploy.sh v0.1.0
#
# The release workflow calls this over SSH after it has published the images.
set -eu
cd "$(dirname "$0")/../.."
version="${1:?usage: deploy.sh <version>}"
. deploy/prod/lib.sh

previous="$SWITCHYARD_VERSION"
echo "deploying $version (running now: $previous)"

# The configuration (compose files, nginx) comes from the repository at that version.
if [ -d .git ]; then
  git fetch --tags --quiet
  git checkout --quiet "$version"
fi

set_env SWITCHYARD_VERSION "$version"
# shellcheck disable=SC1090
. "./$ENV_FILE"
compose pull

# A dump before the migrations run: they only go forward, so this is the way back if a
# release turns out to be wrong for the data.
if compose ps --status running postgres 2>/dev/null | grep -q postgres; then
  mkdir -p "$PRE_DEPLOY_BACKUP_DIR"
  dump="$PRE_DEPLOY_BACKUP_DIR/pre-deploy-$version-$(date -u +%Y%m%dT%H%M%SZ).dump"
  if compose exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$dump"; then
    echo "dump before the upgrade: $dump"
  else
    rm -f "$dump"
    echo "no dump taken (is the database up?)" >&2
  fi
fi

if compose up -d --remove-orphans --wait; then
  echo "$previous" > deploy/prod/.previous_version
  echo "now running $version"
else
  echo "$version did not become healthy: going back to $previous" >&2
  set_env SWITCHYARD_VERSION "$previous"
  # shellcheck disable=SC1090
  . "./$ENV_FILE"
  if [ -d .git ]; then git checkout --quiet "$previous" || true; fi
  compose up -d --remove-orphans --wait
  exit 1
fi
