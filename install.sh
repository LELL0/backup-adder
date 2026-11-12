#!/bin/sh
# Install Backup Adder for the current user: app-menu entry, lellx-add command, state folder.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.local/share/applications" "$HOME/.local/bin" "$DIR/state"
chmod +x "$DIR/backup_adder.py" "$DIR/lellx-add"
sed "s|@DIR@|$DIR|g" "$DIR/backup-adder.desktop.in" > "$HOME/.local/share/applications/lellx-backup-adder.desktop"
ln -sf "$DIR/lellx-add" "$HOME/.local/bin/lellx-add"
command -v update-desktop-database >/dev/null && update-desktop-database "$HOME/.local/share/applications" || true
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1')" \
  || echo "WARNING: install python3-gi, gir1.2-gtk-4.0 and gir1.2-adw-1"
[ -f "$DIR/state/index.db" ] || echo "Note: no index yet. Connect the drive (uses /_INDEX) or run Menu > Rescan drive index."
echo "Installed. Open 'Backup Adder' from the app menu, or run: lellx-add --help"
