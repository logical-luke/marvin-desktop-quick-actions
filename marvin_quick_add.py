#!/usr/bin/python3
"""Amazing Marvin Quick-Add — lightweight floating input with autocomplete."""

import gi
import json
import urllib.request
import threading
import signal
import os
import sys

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango

API_BASE = "https://serv.amazingmarvin.com/api"
CONFIG_PATH = os.path.expanduser("~/.config/marvin-widget/config.json")


def load_token():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f).get("api_token", "")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    token = os.environ.get("MARVIN_API_TOKEN", "")
    if not token:
        print(f"Error: No API token. Set 'api_token' in {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    return token


API_TOKEN = None  # Set in main()


def api_get(path):
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        headers={"X-API-Token": API_TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def api_post(path, data):
    req = urllib.request.Request(
        f"{API_BASE}/{path}",
        data=json.dumps(data).encode(),
        headers={"X-API-Token": API_TOKEN, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _parse_color(hex_color):
    rgba = Gdk.RGBA()
    rgba.parse(hex_color or "#888888")
    return rgba


# ── Autocomplete popup ───────────────────────────────────────────────────────

class AutocompletePopup(Gtk.Window):
    def __init__(self, parent_window):
        super().__init__(type=Gtk.WindowType.POPUP)
        self.set_transient_for(parent_window)
        self.set_type_hint(Gdk.WindowTypeHint.COMBO)
        self.set_keep_above(True)
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.listbox.connect("row-activated", self._on_row_activated)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_max_content_height(200)
        scrolled.set_propagate_natural_height(True)
        scrolled.add(self.listbox)
        self.add(scrolled)

        self.items = []
        self.callback = None

    def populate(self, items, callback):
        self.items = items
        self.callback = callback
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        for title, color in items:
            row = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_top(4)
            hbox.set_margin_bottom(4)
            hbox.set_margin_start(10)
            hbox.set_margin_end(10)
            dot = Gtk.Label(label="\u25CF")
            dot.override_color(Gtk.StateFlags.NORMAL, _parse_color(color))
            hbox.pack_start(dot, False, False, 0)
            label = Gtk.Label(label=title)
            label.set_halign(Gtk.Align.START)
            hbox.pack_start(label, True, True, 0)
            row.add(hbox)
            row.show_all()
            self.listbox.add(row)
        if items:
            self.listbox.select_row(self.listbox.get_row_at_index(0))

    def _on_row_activated(self, listbox, row):
        if self.callback and row:
            idx = row.get_index()
            if 0 <= idx < len(self.items):
                self.callback(self.items[idx][0])

    def select_next(self):
        row = self.listbox.get_selected_row()
        if row:
            nxt = self.listbox.get_row_at_index(row.get_index() + 1)
            if nxt:
                self.listbox.select_row(nxt)

    def select_prev(self):
        row = self.listbox.get_selected_row()
        if row and row.get_index() > 0:
            prev = self.listbox.get_row_at_index(row.get_index() - 1)
            if prev:
                self.listbox.select_row(prev)

    def confirm_selection(self):
        row = self.listbox.get_selected_row()
        if row:
            self._on_row_activated(self.listbox, row)
            return True
        return False

    def position_below(self, entry):
        win = entry.get_window()
        if not win:
            return
        _, ex, ey = win.get_origin()
        alloc = entry.get_allocation()
        self.move(ex, ey + alloc.height + 2)
        self.set_size_request(alloc.width, -1)


# ── Quick-add window ─────────────────────────────────────────────────────────

class QuickAddWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Marvin Quick Add")

        self.categories = []
        self.labels = []
        self.metadata_tags = []  # List of (trigger, value, color) for visual badges

        self.set_default_size(500, 48)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_decorated(False)
        self.set_resizable(False)

        # Position top-center
        screen = Gdk.Screen.get_default()
        monitor = screen.get_primary_monitor()
        geo = screen.get_monitor_geometry(monitor)
        self.move(geo.x + (geo.width - 500) // 2, geo.y + 80)

        self._apply_css(screen)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        # Entry row with tags on the right
        entry_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.entry = Gtk.Entry()
        self.entry.set_placeholder_text("What do you want to get done?")
        self.entry.get_style_context().add_class("quick-add-entry")
        self.entry.set_hexpand(True)
        self.entry.connect("activate", self._on_submit)
        self.entry.connect("changed", self._on_entry_changed)
        self.entry.connect("key-press-event", self._on_entry_key_press)
        entry_row.pack_start(self.entry, True, True, 0)

        # Container for metadata tag badges
        self.tags_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        entry_row.pack_end(self.tags_box, False, False, 0)

        box.pack_start(entry_row, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.get_style_context().add_class("status-label")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_no_show_all(True)
        box.pack_start(self.status_label, False, False, 0)

        self.add(box)

        self.autocomplete = AutocompletePopup(self)

        self.connect("focus-out-event", self._on_focus_out)
        self.connect("delete-event", lambda w, e: Gtk.main_quit() or True)

        # Fetch categories/labels in background
        self._fetch_autocomplete_data()

    def _apply_css(self, screen):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: #1e1e2e;
                border-radius: 12px;
                border: 1px solid #444;
            }
            .quick-add-entry {
                background-color: #313244;
                color: #cdd6f4;
                border: 1px solid #555;
                border-radius: 8px;
                padding: 8px 12px;
                font-size: 15px;
                caret-color: #cdd6f4;
            }
            .quick-add-entry:focus {
                border-color: #89b4fa;
            }
            .status-label {
                font-size: 12px;
            }
            .meta-tag {
                background-color: #f38ba8;
                color: #ffffff;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 12px;
                font-weight: bold;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            screen, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    DURATION_HINTS = [
        ("5m", "#f9e2af"),
        ("10m", "#f9e2af"),
        ("15m", "#f9e2af"),
        ("30m", "#f9e2af"),
        ("45m", "#f9e2af"),
        ("1h", "#f9e2af"),
        ("1h30m", "#f9e2af"),
        ("2h", "#f9e2af"),
        ("3h", "#f9e2af"),
    ]
    SECTION_HINTS = [
        ("Morning", "#f9e2af"),
        ("Afternoon", "#fab387"),
        ("Evening", "#cba6f7"),
    ]
    PRIORITY_HINTS = [
        ("*p1", "#f9e2af"),   # yellow
        ("*p2", "#fab387"),   # orange
        ("*p3", "#f38ba8"),   # red
        ("*p0", "#585b70"),   # none
        ("*Reward", "#a6e3a1"),
    ]
    PLAN_HINTS = [
        ("Next Week", "#cba6f7"),
        ("Next Month", "#cba6f7"),
    ]

    @staticmethod
    def _build_date_hints():
        """Build dynamic date hints with actual dates like Marvin."""
        import datetime
        today = datetime.date.today()
        hints = []
        c = "#89b4fa"

        hints.append((f"Today ({today.strftime('%-m/%-d/%Y')})", "today", c))
        tom = today + datetime.timedelta(days=1)
        hints.append((f"Tomorrow ({tom.strftime('%-m/%-d/%Y')})", "tomorrow", c))

        # Next few weekdays
        for delta in range(2, 8):
            d = today + datetime.timedelta(days=delta)
            name = d.strftime("%A")
            hints.append((f"{name} ({d.strftime('%-m/%-d/%Y')})", name.lower(), c))

        week = today + datetime.timedelta(weeks=1)
        hints.append((f"In a week ({week.strftime('%-m/%-d/%Y')})", "next week", c))

        # Next month
        if today.month == 12:
            nm = today.replace(year=today.year + 1, month=1, day=1)
        else:
            nm = today.replace(month=today.month + 1, day=1)
        hints.append((f"{nm.strftime('%B')} ({nm.strftime('%-m/%-d/%Y')})", "next month", c))

        # Specific date 2 weeks out
        d2w = today + datetime.timedelta(weeks=2)
        hints.append((f"{d2w.strftime('%-m/%-d/%Y')} ({d2w.strftime('%A')})", d2w.isoformat(), c))

        hints.append(("none", "none", "#585b70"))
        return hints

    def _fetch_autocomplete_data(self):
        def fetch():
            try:
                cats = api_get("categories")
                labels = api_get("labels")
                GLib.idle_add(self._on_data_loaded, cats, labels)
            except Exception:
                pass

        threading.Thread(target=fetch, daemon=True).start()

    def _on_data_loaded(self, cats, labels):
        self.categories = [
            {"title": c["title"], "color": c.get("color", "#888")}
            for c in cats
        ]
        self.labels = [
            {"title": l["title"], "color": l.get("color", "#888")}
            for l in labels
        ]

    # ── Autocomplete ─────────────────────────────────────────────────────

    def _on_entry_changed(self, entry):
        text = entry.get_text()
        cursor = entry.get_position()

        result = self._find_trigger(text, cursor)
        if not result:
            self.autocomplete.hide()
            return

        trigger, query, token_start = result
        matches = self._get_matches(trigger, query)

        if matches:
            self.autocomplete.populate(
                matches,
                lambda t, tr=trigger, ts=token_start: self._complete_token(tr, t, ts),
            )
            self.autocomplete.position_below(self.entry)
            self.autocomplete.show_all()
        else:
            self.autocomplete.hide()

    def _find_trigger(self, text, cursor):
        before = text[:cursor]

        # Word-based triggers: "due ", "starts ", "ends ", "review "
        for keyword in ("due ", "starts ", "ends ", "review "):
            idx = before.rfind(keyword)
            if idx >= 0 and (idx == 0 or before[idx - 1] == " "):
                query = before[idx + len(keyword):]
                return keyword.strip(), query, idx

        # Plan: &
        for trigger in ("##", "#", "@", "+", "~", "!", "*", "&"):
            idx = before.rfind(trigger)
            if idx >= 0 and (idx == 0 or before[idx - 1] == " "):
                query = before[idx + len(trigger):]
                return trigger, query, idx

        return None

    def _get_matches(self, trigger, query):
        q = query.lower()

        if trigger == "#":
            return [
                (c["title"], c["color"])
                for c in self.categories
                if q in c["title"].lower()
            ]
        if trigger == "##":
            return []
        if trigger == "@":
            return [
                (l["title"], l["color"])
                for l in self.labels
                if q in l["title"].lower()
            ]
        if trigger == "+" or trigger in ("due", "starts", "ends", "review"):
            date_hints = self._build_date_hints()
            return [(display, c) for display, val, c in date_hints if q in display.lower() or q in val.lower()]
        if trigger == "~":
            return [(t, c) for t, c in self.DURATION_HINTS if q in t.lower()]
        if trigger == "!":
            return [(t, c) for t, c in self.SECTION_HINTS if q in t.lower()]
        if trigger == "*":
            return [(t, c) for t, c in self.PRIORITY_HINTS if q in t.lower()]
        if trigger == "&":
            return [(t, c) for t, c in self.PLAN_HINTS if q in t.lower()]

        return []

    def _complete_token(self, trigger, selected, token_start):
        text = self.entry.get_text()
        cursor = self.entry.get_position()

        # For date hints, extract the value (e.g. "Tomorrow (3/31/2026)" -> "tomorrow")
        date_hints = self._build_date_hints()
        date_val = None
        for display, val, _ in date_hints:
            if display == selected:
                date_val = val
                break

        # Build the shortcut string for the API
        if trigger in ("due", "starts", "ends", "review"):
            shortcut_str = f"{trigger} {date_val or selected}"
        elif trigger == "*" and selected.startswith("*"):
            shortcut_str = selected
        elif trigger == "+" and date_val:
            shortcut_str = f"+{date_val}"
        else:
            shortcut_str = f"{trigger}{selected}"

        # Determine tag display and color
        color = "#f38ba8"  # default pink
        if trigger == "#":
            tag_display = f"in {selected}"
            cat = next((c for c in self.categories if c["title"] == selected), None)
            if cat:
                color = cat["color"]
        elif trigger == "@":
            tag_display = f"@{selected}"
            lbl = next((l for l in self.labels if l["title"] == selected), None)
            if lbl:
                color = lbl["color"]
        elif trigger in ("+", "due", "starts", "ends", "review"):
            label = date_val or selected
            if trigger == "+":
                tag_display = f"scheduled {label}"
            else:
                tag_display = f"{trigger} {label}"
            color = "#89b4fa"
        elif trigger == "~":
            tag_display = f"~{selected}"
            color = "#f9e2af"
        elif trigger == "!":
            tag_display = f"!{selected}"
            color = "#fab387"
        elif trigger == "*":
            tag_display = selected
            color = "#f38ba8"
        elif trigger == "&":
            tag_display = f"&{selected}"
            color = "#cba6f7"
        else:
            tag_display = shortcut_str

        # Remove the trigger text from input
        after = text[cursor:]
        new_text = text[:token_start].rstrip() + " " + after.lstrip()
        new_text = " ".join(new_text.split())  # collapse whitespace
        self.entry.handler_block_by_func(self._on_entry_changed)
        self.entry.set_text(new_text.strip())
        self.entry.set_position(len(new_text.strip()))
        self.entry.handler_unblock_by_func(self._on_entry_changed)

        # Store metadata
        self.metadata_tags.append((shortcut_str, tag_display, color))
        self._render_tags()
        self.autocomplete.hide()

    def _render_tags(self):
        """Render metadata tags as visual badges next to the input."""
        for child in self.tags_box.get_children():
            self.tags_box.remove(child)
        for shortcut_str, display, color in self.metadata_tags:
            badge = Gtk.Label(label=display)
            badge_css = Gtk.CssProvider()
            badge_css.load_from_data(
                f".meta-tag {{ background-color: {color}; color: #ffffff; "
                f"border-radius: 4px; padding: 4px 8px; font-size: 12px; font-weight: bold; }}".encode()
            )
            badge.get_style_context().add_provider(badge_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            badge.get_style_context().add_class("meta-tag")
            self.tags_box.pack_start(badge, False, False, 0)
        self.tags_box.show_all()

    def _build_submit_title(self):
        """Build the full title with shortcut strings appended for the API."""
        title = self.entry.get_text().strip()
        for shortcut_str, _, _ in self.metadata_tags:
            title += " " + shortcut_str
        return title

    def _on_entry_key_press(self, widget, event):
        if self.autocomplete.get_visible():
            if event.keyval == Gdk.KEY_Down:
                self.autocomplete.select_next()
                return True
            elif event.keyval == Gdk.KEY_Up:
                self.autocomplete.select_prev()
                return True
            elif event.keyval in (Gdk.KEY_Tab, Gdk.KEY_Return):
                if self.autocomplete.confirm_selection():
                    return True
            elif event.keyval == Gdk.KEY_Escape:
                self.autocomplete.hide()
                return True

        if event.keyval == Gdk.KEY_Escape:
            Gtk.main_quit()
            return True
        return False

    # ── Submit ───────────────────────────────────────────────────────────

    def _on_submit(self, widget):
        if self.autocomplete.get_visible():
            return
        full_title = self._build_submit_title()
        if not full_title.strip():
            return

        self.entry.set_sensitive(False)
        self._set_status("Adding...", "#a6adc8")

        def do_add():
            try:
                api_post("addTask", {"title": full_title})
                GLib.idle_add(self._on_task_added, full_title)
            except Exception as e:
                GLib.idle_add(self._on_task_error, str(e))

        threading.Thread(target=do_add, daemon=True).start()

    def _on_task_added(self, title):
        display = self.entry.get_text().strip() or title
        self._set_status(f"Added: {display}", "#a6e3a1")
        self.entry.set_text("")
        self.metadata_tags.clear()
        self._render_tags()
        self.entry.set_sensitive(True)
        self.entry.grab_focus()
        # Signal the widget to refresh its task list
        self._notify_widget_refresh()
        GLib.timeout_add(800, Gtk.main_quit)

    @staticmethod
    def _notify_widget_refresh():
        """Send SIGUSR2 to the running widget to trigger a task refresh."""
        widget_pidfile = os.path.expanduser("~/.cache/marvin-widget.pid")
        try:
            with open(widget_pidfile) as f:
                pid = int(f.read().strip())
            os.kill(pid, signal.SIGUSR2)
        except (FileNotFoundError, ProcessLookupError, ValueError):
            pass

    def _on_task_error(self, error):
        self._set_status(f"Error: {error}", "#f38ba8")
        self.entry.set_sensitive(True)
        self.entry.grab_focus()

    def _set_status(self, text, color):
        self.status_label.set_markup(
            f'<span foreground="{color}">{GLib.markup_escape_text(text)}</span>'
        )
        self.status_label.show()

    # ── Focus ────────────────────────────────────────────────────────────

    def _on_focus_out(self, widget, event):
        GLib.timeout_add(200, self._check_focus)
        return False

    def _check_focus(self):
        if not self.is_active() and not self.autocomplete.get_visible():
            Gtk.main_quit()
        return False


PIDFILE = os.path.expanduser("~/.cache/marvin-quick-add.pid")


def ensure_single_instance():
    """If already running, signal it to quit (toggle off), then exit."""
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check alive
            os.kill(pid, signal.SIGUSR1)  # Tell it to close
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass  # Stale PID

    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pidfile():
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


def main():
    global API_TOKEN
    API_TOKEN = load_token()

    ensure_single_instance()
    import atexit
    atexit.register(cleanup_pidfile)

    win = QuickAddWindow()
    win.show_all()
    win.status_label.hide()
    win.entry.grab_focus()

    def on_sigusr1(signum, frame):
        GLib.idle_add(Gtk.main_quit)

    signal.signal(signal.SIGUSR1, on_sigusr1)

    Gtk.main()


if __name__ == "__main__":
    main()
