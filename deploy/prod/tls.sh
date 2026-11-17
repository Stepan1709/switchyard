#!/bin/sh
# Get a Let's Encrypt certificate for a domain that points at this server, and keep it renewed.
#
#   deploy/prod/tls.sh example.com you@example.com
#
# nginx must be running (it answers the validation on port 80). Until then it serves the
# self-signed certificate that setup.sh made.
set -eu
cd "$(dirname "$0")/../.."
domain="${1:?usage: tls.sh <domain> <email>}"
email="${2:?usage: tls.sh <domain> <email>}"
. deploy/prod/lib.sh

compose up -d nginx
compose --profile tls run --rm --entrypoint certbot certbot certonly --webroot -w /var/www/certbot \
  -d "$domain" --email "$email" --agree-tos --no-eff-email --keep-until-expiring \
  --deploy-hook /hooks/deploy.sh

set_env DOMAIN "$domain"
set_env ACME_EMAIL "$email"
case "${COMPOSE_PROFILES:-}" in
  *tls*) ;;
  *) set_env COMPOSE_PROFILES "${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}tls" ;;
esac
# shellcheck disable=SC1090
. "./$ENV_FILE"
compose up -d certbot
echo "certificate installed; certbot renews it twice a day when it is due"
