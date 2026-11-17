#!/bin/sh
# Daily dump of the database (pg_dump custom format) into /backups; the newest BACKUP_KEEP
# files are kept. Runs inside the backup container of the compose stack.
set -eu

: "${POSTGRES_USER:?}" "${POSTGRES_DB:?}"
export PGPASSWORD
PGPASSWORD="$(cat /run/secrets/postgres_password)"
interval="${BACKUP_INTERVAL_SECONDS:-86400}"
keep="${BACKUP_KEEP:-7}"

while true; do
  file="/backups/switchyard-$(date -u +%Y%m%dT%H%M%SZ).dump"
  # Written under a temporary name and renamed: a half-written dump never looks like a backup.
  if pg_dump -h postgres -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$file.part"; then
    mv "$file.part" "$file"
    echo "backup ok: $file ($(wc -c < "$file") bytes)"
  else
    rm -f "$file.part"
    echo "backup FAILED" >&2
  fi
  # shellcheck disable=SC2012
  ls -1t /backups/switchyard-*.dump 2>/dev/null | tail -n +"$((keep + 1))" | xargs -r rm -f
  sleep "$interval"
done
