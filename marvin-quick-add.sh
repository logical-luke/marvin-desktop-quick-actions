#!/bin/bash
# Toggle the full widget (Ctrl+Alt+S)
PIDFILE="$HOME/.cache/marvin-widget.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    kill -USR1 "$(cat "$PIDFILE")"
else
    /usr/bin/python3 "$(dirname "$0")/marvin_widget.py" &
fi
