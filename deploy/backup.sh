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
  # sqlite3's online backup API via the stdlib - consistent even mid-write,
  # and no container or extra package needed.
  python3 - "$DB" "$DEST/hs_property-$STAMP.db" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
d = sqlite3.connect(dst)
with d:
    s.backup(d)
d.close(); s.close()
PY
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
