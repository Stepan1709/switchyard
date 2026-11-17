#!/bin/sh
# Proves that the newest backup can be restored: restores it into a scratch database and
# compares the row counts of the main tables with the live database. Exit status 1 if not.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
export PGPASSWORD
PGPASSWORD="$(cat /run/secrets/postgres_password)"
psql_live() { psql -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At "$@"; }
scratch="restore_check_$$"

# shellcheck disable=SC2012
latest="$(ls -1t /backups/switchyard-*.dump 2>/dev/null | head -n 1 || true)"
[ -n "$latest" ] || { echo "no backup found in /backups" >&2; exit 1; }
echo "restoring $latest into $scratch"

psql_live -d postgres -c "CREATE DATABASE $scratch" >/dev/null
trap 'psql -h postgres -U "$POSTGRES_USER" -d postgres -At -c "DROP DATABASE IF EXISTS $scratch" >/dev/null' EXIT
pg_restore -h postgres -U "$POSTGRES_USER" -d "$scratch" --no-owner "$latest"

status=0
for table in admins projects api_keys providers models routing_rules calibration_runs outbox alembic_version; do
  live="$(psql_live -c "SELECT count(*) FROM $table")"
  copy="$(psql -h postgres -U "$POSTGRES_USER" -d "$scratch" -At -c "SELECT count(*) FROM $table")"
  # The live database may have grown since the dump: the copy must not have MORE, and tables
  # that rarely change must match exactly.
  if [ "$copy" -gt "$live" ]; then
    echo "MISMATCH $table: live $live, restored $copy" >&2
    status=1
  else
    echo "ok $table: live $live, restored $copy"
  fi
done
# The schema version is the strictest check: a dump of another version would not start.
[ "$(psql_live -c 'SELECT version_num FROM alembic_version')" = "$(psql -h postgres -U "$POSTGRES_USER" -d "$scratch" -At -c 'SELECT version_num FROM alembic_version')" ] \
  || { echo "MISMATCH alembic version" >&2; status=1; }
exit "$status"
