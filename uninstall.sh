#!/usr/bin/env bash
# Remove Shreder's launchers and integration. Settings and history are kept unless --purge is given.
set -euo pipefail
APP_ID="io.github.shreder.Shreder"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
systemctl --user disable --now shreder-scheduler.timer 2>/dev/null || true
rm -f "$CONF/systemd/user/shreder-scheduler.timer" "$CONF/systemd/user/shreder-scheduler.service"
systemctl --user daemon-reload 2>/dev/null || true
rm -f "$HOME/.local/bin/shreder" "$HOME/.local/bin/shreder-cli"
rm -f "$DATA/applications/$APP_ID.desktop" "$DATA/icons/hicolor/scalable/apps/$APP_ID.svg"
rm -f "$DATA/nautilus/scripts/Shred with Shreder"
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$CONF/shreder" "$DATA/shreder"
fi
echo "Shreder removed."
