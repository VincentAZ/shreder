#!/usr/bin/env bash
# Install Shreder for the current user (no root needed).
set -euo pipefail
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
APP_ID="io.github.shreder.Shreder"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

python3 - <<'PY' || { echo "Shreder needs Python 3 with GTK 4 and libadwaita >= 1.7:"; echo "  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1"; exit 1; }
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7), "libadwaita too old"
PY

mkdir -p "$BIN" "$APPS" "$ICONS/scalable/apps" "$ICONS/symbolic/apps"
chmod +x "$HERE/bin/shreder" "$HERE/bin/shreder-cli"
ln -sf "$HERE/bin/shreder" "$BIN/shreder"
ln -sf "$HERE/bin/shreder-cli" "$BIN/shreder-cli"
cp "$HERE/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICONS/scalable/apps/"

cat > "$APPS/$APP_ID.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Shreder
GenericName=Secure File Shredder
Comment=Shred files and wipe free space with DoD 5220.22-M and other standards
Exec=$HERE/bin/shreder %F
Icon=$APP_ID
Terminal=false
Categories=Utility;Security;GTK;
Keywords=shred;wipe;erase;delete;secure;dod;gutmann;privacy;
StartupNotify=true
DESKTOP

if [ "${1:-}" != "--no-files-action" ]; then
  python3 -c "import sys; sys.path.insert(0, '$HERE'); from shreder import system; e = system.install_file_manager_action(True); sys.exit(e and print(e))"
fi

command -v update-desktop-database >/dev/null && update-desktop-database -q "$APPS" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t -f "$ICONS" || true

echo "Shreder installed."
echo "  App menu:  Shreder"
echo "  Terminal:  shreder   (GUI)   ·   shreder-cli --help   (command line)"
echo "  Files app: right-click → Scripts → Shred with Shreder"
