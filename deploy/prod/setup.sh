#!/bin/sh
# Prepares a fresh server once (running it again keeps what exists):
#   - random secrets in $SECRETS_DIR
#   - a self-signed certificate in $TLS_DIR so that nginx can start
#   - deploy/prod/.env from the template
# Needs docker (with compose v2) and openssl. Run as the user that will run the stack.
set -eu
cd "$(dirname "$0")/../.."

SECRETS_DIR="${SECRETS_DIR:-/etc/switchyard/secrets}"
TLS_DIR="${TLS_DIR:-/etc/switchyard/tls}"
for tool in docker openssl; do
  command -v "$tool" >/dev/null || { echo "$tool is required" >&2; exit 1; }
done
docker compose version >/dev/null || { echo "docker compose v2 is required" >&2; exit 1; }

mkdir -p "$SECRETS_DIR" "$TLS_DIR" 2>/dev/null || {
  echo "cannot create $SECRETS_DIR: create it with sudo and make it yours, then run this again" >&2
  exit 1
}
chmod 700 "$SECRETS_DIR" "$TLS_DIR"

secret() {
  file="$SECRETS_DIR/$1"
  if [ ! -f "$file" ]; then
    openssl rand -base64 33 | tr -d '/+=\n' > "$file"
    # 644 inside a 700 directory: the processes in the containers (postgres, rabbitmq, the
    # application) run as other users and must be able to read their file; other users of the
    # server cannot reach the directory.
    chmod 644 "$file"
    echo "created $file"
  fi
}
for name in postgres_password redis_password rabbitmq_password jwt_secret key_pepper admin_password; do
  secret "$name"
done
if [ ! -f "$SECRETS_DIR/master_keys" ]; then
  # The key that encrypts the providers' API keys. Keep a copy somewhere safe: without it the
  # stored provider keys cannot be read again.
  echo "k1:$(openssl rand -base64 32)" > "$SECRETS_DIR/master_keys"
  chmod 644 "$SECRETS_DIR/master_keys"
  echo "created $SECRETS_DIR/master_keys (keep a copy of it)"
fi

if [ ! -f "$TLS_DIR/fullchain.pem" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=${DOMAIN:-localhost}" \
    -keyout "$TLS_DIR/privkey.pem" -out "$TLS_DIR/fullchain.pem" 2>/dev/null
  # The key is private to the user who runs the stack; the nginx container is given the one
  # capability (DAC_READ_SEARCH) that lets it read the directory and the key anyway.
  chmod 644 "$TLS_DIR/fullchain.pem"
  chmod 600 "$TLS_DIR/privkey.pem"
  echo "created a self-signed certificate in $TLS_DIR (replace it with deploy/prod/tls.sh)"
fi

if [ ! -f deploy/prod/.env ]; then
  cp deploy/prod/env.example deploy/prod/.env
  sed -i "s|^SECRETS_DIR=.*|SECRETS_DIR=$SECRETS_DIR|; s|^TLS_DIR=.*|TLS_DIR=$TLS_DIR|" deploy/prod/.env
  echo "created deploy/prod/.env: set ADMIN_EMAIL (and DOMAIN) in it"
fi

cat <<MSG

Next:
  1. edit deploy/prod/.env (ADMIN_EMAIL, and the version to run if not 'latest')
  2. deploy/prod/deploy.sh <version>        for example v0.1.0
  3. sign in at https://<this server>/ as ADMIN_EMAIL; the password is in $SECRETS_DIR/admin_password
  4. with a domain: deploy/prod/tls.sh <domain> <email>
MSG
