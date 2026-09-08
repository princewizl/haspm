#!/usr/bin/env bash
# Initial Let's Encrypt issuance.
#
# Uses --standalone because nginx cannot start until the certificate it
# references exists. Renewals afterwards go through the webroot challenge in
# the certbot container, so they need no downtime.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/haspm}
EMAIL=${LETSENCRYPT_EMAIL:-princewill691@gmail.com}
PRIMARY=haspm.com
CANDIDATES=(haspm.com www.haspm.com test.haspm.com)

cd "$APP_DIR"

MY_IP=$(curl -fsS4 https://ifconfig.me || echo "unknown")
echo "==> This server: $MY_IP"

# Certbot fails the whole request if any single name does not resolve here,
# so only ask for the names that actually point at this box.
DOMAIN_ARGS=()
for d in "${CANDIDATES[@]}"; do
  resolved=$(getent ahostsv4 "$d" 2>/dev/null | awk '{print $1; exit}' || true)
  if [ "$resolved" = "$MY_IP" ]; then
    echo "    $d -> $resolved  OK"
    DOMAIN_ARGS+=(-d "$d")
  else
    echo "    $d -> ${resolved:-NXDOMAIN}  SKIPPED (does not point here)"
  fi
done

if [ ${#DOMAIN_ARGS[@]} -eq 0 ]; then
  echo "No domain resolves to this server. Fix DNS first." >&2
  exit 1
fi

# Port 80 must be free for the standalone challenge.
docker compose stop nginx 2>/dev/null || true

docker run --rm \
  -p 80:80 \
  -v haspm_letsencrypt:/etc/letsencrypt \
  -v haspm_certbot-www:/var/www/certbot \
  certbot/certbot certonly \
    --standalone \
    --non-interactive --agree-tos \
    --email "$EMAIL" \
    --cert-name "$PRIMARY" \
    "${DOMAIN_ARGS[@]}"

echo
echo "==> Certificate:"
docker run --rm -v haspm_letsencrypt:/etc/letsencrypt \
  certbot/certbot certificates

echo
echo "Done. Start the stack with: $APP_DIR/deploy/deploy.sh all"
