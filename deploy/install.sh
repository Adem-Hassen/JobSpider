#!/usr/bin/env bash
# Install or update jobfinder on Ubuntu 24.04. Idempotent: run it again to update.
#
#   sudo deploy/install.sh               install, or update to this checkout
#   sudo deploy/install.sh --no-enable   install without enabling the timers
#   sudo deploy/install.sh --rollback    switch back to the previous release
#
# Layout:
#   /opt/jobfinder/releases/<stamp>-v<version>/   immutable release (code + .venv)
#   /opt/jobfinder/current -> releases/...         active release (atomic switch)
#   /opt/jobfinder/{bin/uv,python,cache}           pinned uv and its managed Python 3.12
#   /etc/jobfinder/jobfinder.env                   secrets (mode 600, never overwritten)
#   /etc/jobfinder/config/*.yaml                   settings (never overwritten)
#   /var/lib/jobfinder                             data (jobfinder:jobfinder, 700)
#   /var/backups/jobfinder                         daily backups
#   /usr/local/bin/jobfinder                       wrapper: runs as the service user
set -euo pipefail

UV_VERSION=0.13.0
PREFIX=/opt/jobfinder
SERVICE_USER=jobfinder
DATA_DIR=/var/lib/jobfinder
BACKUP_DIR=/var/backups/jobfinder
ETC_DIR=/etc/jobfinder
CONFIG_DIR=$ETC_DIR/config
ENV_FILE=$ETC_DIR/jobfinder.env
UNIT_DIR=/etc/systemd/system
KEEP_RELEASES=3
UNITS=(jobfinder.service jobfinder.timer jobfinder-alert.service
       jobfinder-backup.service jobfinder-backup.timer)
SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

enable=1
rollback=0
for arg in "$@"; do
    case $arg in
        --no-enable) enable=0 ;;
        --rollback) rollback=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
note() { printf '    %s\n' "$*"; }

[ "$(id -u)" = 0 ] || { echo "run as root (sudo $0)" >&2; exit 1; }
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release 2>/dev/null; then
    note "warning: written for Ubuntu 24.04; continuing anyway"
fi

switch_to() {  # atomically point $PREFIX/current at a release
    ln -sfn "$1" "$PREFIX/current.new"
    mv -Tf "$PREFIX/current.new" "$PREFIX/current"
}

if [ "$rollback" = 1 ]; then
    current=$(readlink -f "$PREFIX/current" || true)
    previous=$(find "$PREFIX/releases" -mindepth 1 -maxdepth 1 -type d | sort | grep -vx "$current" | tail -n 1 || true)
    [ -n "$previous" ] || { echo "no previous release to roll back to" >&2; exit 1; }
    say "Rolling back to $(basename "$previous")"
    switch_to "$previous"
    install -m 644 "$previous"/deploy/*.service "$previous"/deploy/*.timer "$UNIT_DIR/"
    systemctl daemon-reload
    note "now running $(basename "$previous"); $(basename "$current") is kept"
    exit 0
fi

say "System packages"
missing=()
for pkg in sqlite3 ca-certificates curl; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends "${missing[@]}"
fi

say "Service user and directories"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$DATA_DIR" "$BACKUP_DIR"
install -d -o root -g "$SERVICE_USER" -m 750 "$ETC_DIR" "$CONFIG_DIR"
install -d -o root -g root -m 755 "$PREFIX" "$PREFIX/bin" "$PREFIX/releases" "$PREFIX/python"
install -d -o root -g root -m 700 "$PREFIX/cache"

say "uv $UV_VERSION"
if [ ! -x "$PREFIX/bin/uv" ] || ! "$PREFIX/bin/uv" --version | grep -q " $UV_VERSION"; then
    curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" \
        | env UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 sh
fi

version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$SRC_DIR/pyproject.toml" | head -n 1)
release="$PREFIX/releases/$(date -u +%Y%m%dT%H%M%SZ)-v$version"
say "Building release $(basename "$release")"
install -d -m 755 "$release"
for item in pyproject.toml uv.lock README.md .python-version .env.example src config deploy; do
    cp -r "$SRC_DIR/$item" "$release/"
done
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_CACHE_DIR="$PREFIX/cache"
export UV_PROJECT_ENVIRONMENT="$release/.venv" UV_PYTHON_PREFERENCE=only-managed
"$PREFIX/bin/uv" sync --directory "$release" --frozen --no-dev --no-editable --python 3.12 --quiet
chown -R root:root "$release"
chmod -R go-w "$release"
"$release/.venv/bin/jobfinder" --help >/dev/null

say "Configuration"
for file in "$release"/config/*.yaml; do
    name=$(basename "$file")
    if [ ! -e "$CONFIG_DIR/$name" ]; then
        install -m 640 -o root -g "$SERVICE_USER" "$file" "$CONFIG_DIR/$name"
        note "created $CONFIG_DIR/$name"
    elif ! cmp -s "$file" "$CONFIG_DIR/$name"; then
        note "kept your $CONFIG_DIR/$name (this release's default: $file)"
    fi
done
if [ ! -e "$ENV_FILE" ]; then
    sed -e "s|^DATA_DIR=.*|DATA_DIR=$DATA_DIR|" \
        -e "s|^JOBFINDER_CONFIG_DIR=.*|JOBFINDER_CONFIG_DIR=$CONFIG_DIR|" \
        "$release/.env.example" > "$ENV_FILE"
    note "created $ENV_FILE: fill in your keys"
fi
chown root:root "$ENV_FILE"
chmod 600 "$ENV_FILE"

say "Activating release"
switch_to "$release"
cat > /usr/local/bin/jobfinder <<WRAPPER
#!/bin/sh
# Run jobfinder as the service user, with the service's environment (installed by
# install.sh). Usage: sudo jobfinder status
exec systemd-run --quiet --wait --pipe --collect \\
    --property=User=$SERVICE_USER --property=Group=$SERVICE_USER \\
    --property=EnvironmentFile=$ENV_FILE --property=WorkingDirectory=$DATA_DIR \\
    $PREFIX/current/.venv/bin/jobfinder "\$@"
WRAPPER
chmod 755 /usr/local/bin/jobfinder

say "systemd units"
install -m 644 "${UNITS[@]/#/$release/deploy/}" "$UNIT_DIR/"
systemctl daemon-reload

say "Self-check"
if /usr/local/bin/jobfinder selfcheck --offline; then
    if [ "$enable" = 1 ]; then
        systemctl enable --now jobfinder.timer jobfinder-backup.timer
        note "timers enabled: systemctl list-timers 'jobfinder*'"
    fi
else
    note "self-check failed: edit $ENV_FILE and $CONFIG_DIR, then run:"
    note "  sudo jobfinder selfcheck && sudo systemctl enable --now jobfinder.timer jobfinder-backup.timer"
fi

say "Pruning old releases (keeping $KEEP_RELEASES)"
active=$(readlink -f "$PREFIX/current")
mapfile -t releases < <(find "$PREFIX/releases" -mindepth 1 -maxdepth 1 -type d | sort)
excess=$(( ${#releases[@]} - KEEP_RELEASES ))
for (( i = 0; i < excess; i++ )); do
    [ "${releases[i]}" = "$active" ] && continue
    rm -rf -- "${releases[i]}"
    note "removed $(basename "${releases[i]}")"
done

say "Done: $(basename "$release")"
cat <<NEXT
    Next steps (first install):
      1. sudoedit $ENV_FILE           # AI key, CONTACT_EMAIL, Telegram, healthcheck
      2. sudoedit $CONFIG_DIR/settings.yaml $CONFIG_DIR/sources.yaml
      3. sudo install -o $SERVICE_USER -m 600 your-cv.pdf $DATA_DIR/cv-upload.pdf
         sudo jobfinder profile import $DATA_DIR/cv-upload.pdf
      4. sudo jobfinder selfcheck && sudo jobfinder notify --test
      5. sudo systemctl enable --now jobfinder.timer jobfinder-backup.timer
NEXT
