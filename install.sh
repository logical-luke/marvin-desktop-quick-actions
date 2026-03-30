#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONFIG_DIR="$HOME/.config/marvin-widget"
CONFIG_FILE="$CONFIG_DIR/config.json"
BIN_DIR="$HOME/.local/bin"

echo "=== Amazing Marvin Desktop Widget - Install ==="

# 1. Config
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG_FILE" ]; then
    read -rp "Enter your Amazing Marvin API token: " API_TOKEN
    cat > "$CONFIG_FILE" <<EOF
{
    "api_token": "$API_TOKEN"
}
EOF
    chmod 600 "$CONFIG_FILE"
    echo "Config saved to $CONFIG_FILE"
else
    echo "Config already exists at $CONFIG_FILE"
fi

# 2. Symlink launchers
mkdir -p "$BIN_DIR"
ln -sf "$SCRIPT_DIR/marvin-quick-add.sh" "$BIN_DIR/marvin-quick-add.sh"
ln -sf "$SCRIPT_DIR/marvin-input.sh" "$BIN_DIR/marvin-input.sh"
echo "Symlinked launchers to $BIN_DIR"

# 3. Register GNOME keyboard shortcuts
# Ctrl+Alt+A — quick-add input form (ephemeral)
KPATH_A="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-marvin-input/"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" name "Marvin Quick Add Input"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" command "$SCRIPT_DIR/marvin-input.sh"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_A" binding "<Ctrl><Alt>a"

# Ctrl+Alt+S — full task widget (persistent, toggle)
KPATH_S="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom-marvin/"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" name "Marvin Task Widget"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" command "$SCRIPT_DIR/marvin-quick-add.sh"
gsettings set "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KPATH_S" binding "<Ctrl><Alt>s"

# Ensure both paths are in the keybinding list
EXISTING=$(gsettings get org.gnome.settings-daemon.plugins.media-keys custom-keybindings)
UPDATED="$EXISTING"
for P in "$KPATH_A" "$KPATH_S"; do
    if ! echo "$UPDATED" | grep -q "$(basename "$P")"; then
        UPDATED=$(echo "$UPDATED" | sed "s|]$|, '$P']|" | sed "s|\[, |[|")
    fi
done
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "$UPDATED"
echo "Registered shortcuts: Ctrl+Alt+A (input) and Ctrl+Alt+S (widget)"

# 4. Install desktop entry and autostart
APPS_DIR="$HOME/.local/share/applications"
AUTOSTART_DIR="$HOME/.config/autostart"
mkdir -p "$APPS_DIR" "$AUTOSTART_DIR"

# Desktop entry (for dash icon)
sed "s|Exec=.*|Exec=$SCRIPT_DIR/marvin-quick-add.sh|;s|Icon=.*|Icon=$SCRIPT_DIR/icon.png|" \
    "$SCRIPT_DIR/marvin-widget.desktop" > "$APPS_DIR/marvin-widget.desktop"

# Autostart entry
cat > "$AUTOSTART_DIR/marvin-widget.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Marvin Tasks
Comment=Amazing Marvin floating task widget
Exec=/usr/bin/python3 $SCRIPT_DIR/marvin_widget.py
Icon=$SCRIPT_DIR/icon.png
Terminal=false
StartupNotify=false
StartupWMClass=marvin-tasks
X-GNOME-Autostart-enabled=true
EOF
echo "Autostart entry created"

echo ""
echo "=== Done! ==="
echo "  Ctrl+Alt+A — Quick-add input (add a task and dismiss)"
echo "  Ctrl+Alt+S — Toggle task widget (persistent sidebar)"
