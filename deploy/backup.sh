#!/usr/bin/env bash
# Daily jobfinder backup: a consistent copy of state.db (SQLite online backup), the
# workbook or opportunities.db, profiles/, cv/ and the YAML config, as one dated
# tar.gz in BACKUP_DIR. The newest BACKUP_KEEP archives are kept.
#
# Environment (defaults match deploy/install.sh):
#   DATA_DIR=/var/lib/jobfinder  BACKUP_DIR=/var/backups/jobfinder  BACKUP_KEEP=14
#   JOBFINDER_CONFIG_DIR=/etc/jobfinder/config  PYTHON=python3 (used if sqlite3 is missing)
set -euo pipefail

DATA_DIR=${DATA_DIR:-/var/lib/jobfinder}
BACKUP_DIR=${BACKUP_DIR:-/var/backups/jobfinder}
BACKUP_KEEP=${BACKUP_KEEP:-14}
CONFIG_DIR=${JOBFINDER_CONFIG_DIR:-/etc/jobfinder/config}
PYTHON=${PYTHON:-python3}

umask 077
log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

# Online backup of a SQLite database (safe while jobfinder is running; handles WAL).
backup_db() {
    local src=$1 dst=$2
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$src" ".backup '$dst'"
        [ "$(sqlite3 "$dst" 'PRAGMA integrity_check;')" = "ok" ]
    else
        "$PYTHON" - "$src" "$dst" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
src.close(); dst.close()
sys.exit(0 if ok == "ok" else 1)
PY
    fi
}

[ -d "$DATA_DIR" ] || { log "DATA_DIR $DATA_DIR does not exist"; exit 1; }
mkdir -p "$BACKUP_DIR"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
work=$(mktemp -d "$BACKUP_DIR/.work.XXXXXX")
trap 'rm -rf "$work"' EXIT
mkdir "$work/jobfinder"
dest="$work/jobfinder"

for db in state.db opportunities.db; do
    if [ -f "$DATA_DIR/$db" ]; then
        backup_db "$DATA_DIR/$db" "$dest/$db"
    fi
done
if [ -f "$DATA_DIR/opportunities.xlsx" ]; then
    # The same lock jobfinder takes while writing the workbook.
    flock "$DATA_DIR/opportunities.xlsx.lock" cp -p "$DATA_DIR/opportunities.xlsx" "$dest/"
fi
for dir in profiles cv; do
    if [ -d "$DATA_DIR/$dir" ]; then
        cp -rp "$DATA_DIR/$dir" "$dest/"
    fi
done
if [ -d "$CONFIG_DIR" ]; then
    cp -rp "$CONFIG_DIR" "$dest/config"
fi
(cd "$dest" && find . -type f ! -name MANIFEST -print0 | sort -z | xargs -0 -r sha256sum) > "$dest/MANIFEST"

archive="$BACKUP_DIR/jobfinder-$stamp.tar.gz"
tar -C "$work" -czf "$archive.partial" jobfinder
tar -tzf "$archive.partial" >/dev/null
mv "$archive.partial" "$archive"
log "wrote $archive ($(du -h "$archive" | cut -f1))"

# Keep the newest BACKUP_KEEP archives (names sort chronologically).
mapfile -t archives < <(find "$BACKUP_DIR" -maxdepth 1 -name 'jobfinder-*.tar.gz' -printf '%f\n' | sort)
excess=$(( ${#archives[@]} - BACKUP_KEEP ))
for (( i = 0; i < excess; i++ )); do
    rm -f -- "$BACKUP_DIR/${archives[i]}"
    log "removed old ${archives[i]}"
done
