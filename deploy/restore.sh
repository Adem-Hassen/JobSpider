#!/usr/bin/env bash
# Restore jobfinder data from a backup archive made by backup.sh. Run as root:
#   sudo /opt/jobfinder/current/deploy/restore.sh /var/backups/jobfinder/jobfinder-<stamp>.tar.gz
#
# Stops the timer, waits for a running pass to end, moves the current data aside to
# DATA_DIR/pre-restore-<stamp>/, restores state.db, the workbook or opportunities.db,
# profiles/ and cv/, fixes ownership and restarts the timer. The archived YAML config is
# extracted next to the live one for comparison; it is copied over it only with
# --with-config.
#
# Environment: DATA_DIR=/var/lib/jobfinder CONFIG_DIR=/etc/jobfinder/config
#              SERVICE_USER=jobfinder SKIP_SYSTEMCTL=1 (tests only)
set -euo pipefail

DATA_DIR=${DATA_DIR:-/var/lib/jobfinder}
CONFIG_DIR=${CONFIG_DIR:-/etc/jobfinder/config}
SERVICE_USER=${SERVICE_USER:-jobfinder}
SKIP_SYSTEMCTL=${SKIP_SYSTEMCTL:-0}

with_config=0
archive=""
for arg in "$@"; do
    case $arg in
        --with-config) with_config=1 ;;
        -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
        *) archive=$arg ;;
    esac
done
[ -n "$archive" ] && [ -f "$archive" ] || { echo "usage: $0 [--with-config] <archive.tar.gz>" >&2; exit 2; }
tar -tzf "$archive" jobfinder/MANIFEST >/dev/null || { echo "$archive is not a jobfinder backup" >&2; exit 1; }

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
tar -C "$work" -xzf "$archive"
src="$work/jobfinder"
(cd "$src" && sha256sum --quiet -c MANIFEST) || { echo "checksum mismatch in $archive" >&2; exit 1; }

if [ "$SKIP_SYSTEMCTL" != 1 ]; then
    systemctl stop jobfinder.timer
    while systemctl is-active --quiet jobfinder.service; do sleep 2; done
fi

stamp=$(date -u +%Y%m%dT%H%M%SZ)
aside="$DATA_DIR/pre-restore-$stamp"
mkdir -p "$aside"
for item in state.db state.db-wal state.db-shm opportunities.db opportunities.db-wal \
            opportunities.db-shm opportunities.xlsx profiles cv; do
    if [ -e "$DATA_DIR/$item" ]; then
        mv "$DATA_DIR/$item" "$aside/"
    fi
done
for item in state.db opportunities.db opportunities.xlsx profiles cv; do
    if [ -e "$src/$item" ]; then
        cp -rp "$src/$item" "$DATA_DIR/"
    fi
done
if [ -d "$src/config" ]; then
    if [ "$with_config" = 1 ]; then
        cp -rp "$CONFIG_DIR" "$aside/config"
        cp -rp "$src/config/." "$CONFIG_DIR/"
        echo "config restored (previous config kept in $aside/config)"
    else
        rm -rf "$aside/config-from-backup"
        cp -rp "$src/config" "$aside/config-from-backup"
        echo "archived config extracted to $aside/config-from-backup (live config unchanged)"
    fi
fi
if id -u "$SERVICE_USER" >/dev/null 2>&1 && [ "$(id -u)" = 0 ]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
fi
chmod -R go-rwx "$DATA_DIR"

if [ "$SKIP_SYSTEMCTL" != 1 ]; then
    systemctl start jobfinder.timer
fi
echo "restored from $archive; previous data in $aside"
