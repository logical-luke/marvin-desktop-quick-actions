#!/bin/bash
# Setup GNOME keyboard shortcuts for Marvin widgets
# Run from host: bash setup-shortcuts.sh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$HOME/.cache/marvin-widget.pid"

# Ctrl+Alt+A — Quick-add input (ephemeral)
KPATH_A="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-marvin-input/"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" name "Marvin Quick Add Input"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" command "$SCRIPT_DIR/marvin-input.sh"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" binding "<Ctrl><Alt>a"

# Ctrl+Alt+S — Toggle task widget (starts it if not running)
KPATH_S="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-marvin/"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" name "Marvin Task Widget"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" command "$SCRIPT_DIR/marvin-quick-add.sh"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" binding "<Ctrl><Alt>s"

# Ctrl+Alt+W — Toggle time tracking widget
KPATH_W="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-marvin-time/"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_W" name "Marvin Time Widget"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_W" command "bash -c 'kill -SIGRTMIN \$(cat $PIDFILE) 2>/dev/null'"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_W" binding "<Ctrl><Alt>w"

# Register all three paths
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['$KPATH_A', '$KPATH_S', '$KPATH_W']"

echo "Done. Shortcuts registered:"
echo "  Ctrl+Alt+A → Quick Add Input"
echo "  Ctrl+Alt+S → Toggle Task Widget"
echo "  Ctrl+Alt+W → Toggle Time Widget"
