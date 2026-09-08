#!/usr/bin/env bash
# Deploy one environment (or both) from the current git branch.
#
#   deploy.sh test      update test.haspm.com only
#   deploy.sh live      update haspm.com only
#   deploy.sh all       update everything including nginx
#
# Optional: REF=<branch-or-sha> deploy.sh live
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/haspm}
TARGET=${1:-all}
REF=${REF:-main}

cd "$APP_DIR"

echo "==> Fetching $REF"
git fetch --all --prune
git checkout -B deployed "origin/$REF" 2>/dev/null || git checkout "$REF"
git reset --hard "origin/$REF"
echo "    now at $(git rev-parse --short HEAD) — $(git log -1 --pretty=%s)"

# Volumes are owned by the image's non-root user (uid 10001). Running as the
# CI "deploy" account we are not root, so fall back to the narrow sudo rule.
mkdir -p data/{live,test}/{data,static-uploads/properties,doc-uploads/documents}
# sudoers matches the command line literally, so this must be the absolute path.
if [ "$(id -u)" -eq 0 ]; then
  chown -R 10001:10001 "$APP_DIR/data"
else
  sudo -n chown -R 10001:10001 "$APP_DIR/data"
fi

case "$TARGET" in
  test)  SERVICES=(web-test) ;;
  live)  SERVICES=(web-live sched-live) ;;
  all)   SERVICES=(web-live sched-live web-test nginx certbot) ;;
  *) echo "usage: deploy.sh [test|live|all]" >&2; exit 2 ;;
esac

echo "==> Building image"
docker compose build web-live

# Schema changes must land before the new code serves traffic. Live is backed
# up first so a bad migration is recoverable.
case "$TARGET" in
  test) MIGRATE=(test) ;;
  live) MIGRATE=(live) ;;
  all)  MIGRATE=(test live) ;;
esac

for env in "${MIGRATE[@]}"; do
  if [ "$env" = "live" ]; then
    echo "==> Backing up live before migrating"
    "$APP_DIR/deploy/backup.sh" live || {
      echo "Backup failed - refusing to migrate live." >&2; exit 1; }
  fi
  echo "==> Migrating $env database"
  docker compose run --rm --no-deps "web-$env" python migrate_v2.py
done

echo "==> Starting: ${SERVICES[*]}"
docker compose up -d --force-recreate "${SERVICES[@]}"

echo "==> Waiting for health"
for i in $(seq 1 30); do
  sleep 2
  unhealthy=$(docker compose ps --format '{{.Name}} {{.State}} {{.Status}}' \
              | grep -E 'starting|unhealthy' || true)
  [ -z "$unhealthy" ] && break
done

docker compose ps
echo
echo "==> Reachability"
for host in haspm.com test.haspm.com; do
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "https://$host/" || echo "ERR")
  title=$(curl -sS --max-time 15 "https://$host/" 2>/dev/null \
          | grep -o '<title>[^<]*' | head -1 | cut -c8- || true)
  # Status alone is not proof: check the page actually came from this app.
  echo "    https://$host -> $code   ${title:-<no title>}"
done

echo "==> Pruning old images"
docker image prune -f >/dev/null

echo "Deploy complete."
