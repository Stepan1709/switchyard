#!/bin/sh
# Go back to the version that ran before the last deploy (or to the one given).
#
#   deploy/prod/rollback.sh            # the previous one
#   deploy/prod/rollback.sh v0.1.0
#
# The database is not rolled back: migrations only add. If the data itself has to go back,
# restore the dump taken before the upgrade (deploy/backup/restore.sh).
set -eu
cd "$(dirname "$0")/../.."
target="${1:-}"
if [ -z "$target" ]; then
  [ -f deploy/prod/.previous_version ] || { echo "no previous version recorded: give one" >&2; exit 1; }
  target="$(cat deploy/prod/.previous_version)"
fi
exec deploy/prod/deploy.sh "$target"
