#!/bin/sh
# Renews the short-lived Let’s Encrypt certificate for the public IP, then
# reloads Caddy so it starts serving the new certificate.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)

docker run --rm \
  -v "$project_dir/certbot/conf:/etc/letsencrypt" \
  -v "$project_dir/certbot/www:/var/www/certbot" \
  certbot/certbot:v5.4.0 renew --quiet

docker exec da-caddy caddy reload \
  --config /etc/caddy/Caddyfile \
  --adapter caddyfile
