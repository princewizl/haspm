#!/usr/bin/env bash
# Snapshot an environment's database and uploads.
#   backup.sh live     (default)
#   backup.sh test
#
# SQLite must be copied with the .backup command, not cp — a plain copy of a
# database being written to can land on disk mid-transaction and restore corrupt.
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/haspm}
ENV_NAME=${1:-live}
KEEP_DAYS=${KEEP_DAYS:-30}

SRC="$APP_DIR/data/$ENV_NAME"
DEST="$APP_DIR/backups/$ENV_NAME"
STAMP=$(date -u +%Y%m%d-%H%M%S)

[ -d "$SRC" ] || { echo "No such environment: $ENV_NAME" >&2; exit 1; }
mkdir -p "$DEST"

DB="$SRC/data/hs_property.db"
if [ -f "$DB" ]; then
  docker run --rm -v "$SRC/data":/d -v "$DEST":/out \
    keinos/sqlite3:latest \
    sqlite3 /d/hs_property.db ".backup '/out/hs_property-$STAMP.db'"
  gzip -f "$DEST/hs_property-$STAMP.db"
  echo "  db      -> $DEST/hs_property-$STAMP.db.gz"
else
  echo "  db      -> none yet (fresh environment)"
fi

tar czf "$DEST/uploads-$STAMP.tar.gz" \
    -C "$SRC" static-uploads doc-uploads 2>/dev/null || true
echo "  uploads -> $DEST/uploads-$STAMP.tar.gz"

# Config holds SMTP and Paystack credentials, so keep it out of the world.
if compgen -G "$SRC/data/*.json" > /dev/null; then
  tar czf "$DEST/config-$STAMP.tar.gz" -C "$SRC/data" \
      $(cd "$SRC/data" && ls *.json)
  chmod 600 "$DEST/config-$STAMP.tar.gz"
  echo "  config  -> $DEST/config-$STAMP.tar.gz"
fi

find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete
echo "Backup of '$ENV_NAME' complete (retention ${KEEP_DAYS}d)."
