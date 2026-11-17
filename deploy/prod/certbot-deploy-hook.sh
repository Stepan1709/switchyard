#!/bin/sh
# Runs inside the certbot container after a certificate was issued or renewed: put it where
# nginx reads it. nginx reloads on a timer and picks it up.
set -eu
lineage="${RENEWED_LINEAGE:?}"
cp "$lineage/fullchain.pem" /tls/fullchain.pem
cp "$lineage/privkey.pem" /tls/privkey.pem
chmod 644 /tls/fullchain.pem
chmod 600 /tls/privkey.pem
echo "certificate copied to /tls"
