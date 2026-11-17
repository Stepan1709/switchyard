#!/bin/sh
# Restore the live database from a dump. DESTROYS the current content of the database.
# Run on the server from the repository root, with the application stopped:
#
#   deploy/prod/stack.sh stop-apps
#   deploy/backup/restore.sh /path/to/switchyard-YYYYmmddTHHMMSSZ.dump
#   deploy/prod/stack.sh start
set -eu

dump="${1:?usage: restore.sh <dump file>}"
[ -f "$dump" ] || { echo "no such file: $dump" >&2; exit 1; }
cd "$(dirname "$0")/../.."
. deploy/prod/lib.sh

echo "This replaces the database $POSTGRES_DB with the content of $dump. Type YES to go on:"
read -r answer
[ "$answer" = "YES" ] || { echo "cancelled"; exit 1; }

compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE IF EXISTS $POSTGRES_DB WITH (FORCE)"
compose exec -T postgres psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $POSTGRES_DB"
compose exec -T postgres pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner < "$dump"
echo "restored. Start the application again."
