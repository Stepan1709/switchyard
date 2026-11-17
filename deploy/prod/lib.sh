# shellcheck shell=sh
# Shared by the scripts of this directory. Source it from the repository root:
#   cd "$(dirname "$0")/../.." && . deploy/prod/lib.sh
ENV_FILE="deploy/prod/.env"

[ -f "$ENV_FILE" ] || { echo "$ENV_FILE is missing: run deploy/prod/setup.sh first" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
. "./$ENV_FILE"
set +a

compose() {
  docker compose --env-file "$ENV_FILE" -f deploy/compose.yml -f deploy/compose.prod.yml "$@"
}

# Change one KEY=value line of the environment file (the version to run, the profiles).
set_env() {
  key="$1"; value="$2"
  if grep -q "^$key=" "$ENV_FILE"; then
    sed -i "s|^$key=.*|$key=$value|" "$ENV_FILE"
  else
    echo "$key=$value" >> "$ENV_FILE"
  fi
}
