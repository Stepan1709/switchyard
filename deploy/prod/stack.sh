#!/bin/sh
# start | stop | stop-apps | status | logs [service]
set -eu
cd "$(dirname "$0")/../.."
. deploy/prod/lib.sh

case "${1:-status}" in
  start)      compose up -d --remove-orphans --wait ;;
  stop)       compose --profile '*' down ;;
  # Everything that writes to the database stops; the database and the broker keep running.
  stop-apps)  compose stop nginx web gateway api worker ;;
  status)     compose --profile '*' ps ;;
  logs)       shift; compose logs -f --tail=100 "$@" ;;
  *)          echo "usage: stack.sh start|stop|stop-apps|status|logs [service]" >&2; exit 2 ;;
esac
