#!/usr/bin/python3.13
"""Amazing Marvin Quick-Add — lightweight floating input with autocomplete."""

import gi
import json
import urllib.request
import threading
import signal
import os
import sys
import subprocess
import base64
import tempfile
import shutil
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

os.environ["GDK_BACKEND"] = "x11"  # Force XWayland so move() works on GNOME Wayland

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, Gdk, GLib, Pango, Gio

API_BASE = "https://serv.amazingmarvin.com/api"
CONFIG_PATH = os.path.expanduser("~/.config/marvin-widget/config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_token():
    config = load_config()
    token = config.get("api_token", "") or os.environ.get("MARVIN_API_TOKEN", "")
    if not token:
        print(f"Error: No API token. Set 'api_token' in {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    return token


API_TOKEN = None  # Set in main()
ANTHROPIC_API_KEY = None  # Set in main()


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

        self.set_default_size(600, 48)
        self.set_size_request(600, -1)
        self.set_resizable(True)
        self.set_gravity(Gdk.Gravity.SOUTH)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_decorated(False)

        # Position bottom-center (grows upward)
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geo = monitor.get_geometry()
        self._monitor_geo = geo
        self._bottom_margin = 100  # pixels from bottom of screen (matches main widget)
        self._reposition_window(48)
        self.set_keep_above(True)

        self._apply_css(Gdk.Screen.get_default())

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

        # Screenshot button
        screenshot_btn = Gtk.Button(label="\U0001f4f7")
        screenshot_btn.set_tooltip_text("Screenshot OCR (Ctrl+Shift+S)")
        screenshot_btn.get_style_context().add_class("screenshot-btn")
        screenshot_btn.connect("clicked", lambda b: self._on_screenshot_ocr())
        entry_row.pack_end(screenshot_btn, False, False, 0)

        # Container for metadata tag badges
        self.tags_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        entry_row.pack_end(self.tags_box, False, False, 0)

        # Notes field (Ctrl+N to toggle) — above entry so it expands upward
        self.notes_scroll = Gtk.ScrolledWindow()
        self.notes_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.notes_scroll.set_min_content_height(60)
        self.notes_scroll.set_max_content_height(120)
        self.notes_scroll.set_propagate_natural_height(True)
        self.notes_scroll.set_no_show_all(True)
        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.get_style_context().add_class("notes-field")
        self.notes_view.connect("key-press-event", self._on_notes_key_press)
        buf = self.notes_view.get_buffer()
        buf.connect("changed", self._on_notes_buffer_changed)
        self.notes_scroll.add(self.notes_view)
        box.pack_start(self.notes_scroll, False, False, 0)

        self.status_label = Gtk.Label()
        self.status_label.get_style_context().add_class("status-label")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_label.set_no_show_all(True)
        box.pack_start(self.status_label, False, False, 0)

        box.pack_start(entry_row, False, False, 0)

        self.add(box)

        self.autocomplete = AutocompletePopup(self)

        self.connect("delete-event", lambda w, e: self.hide() or True)

        self._base_height = None  # captured after first render

        # Fetch categories/labels in background
        self._fetch_autocomplete_data()

        # Show default "today" tag
        self._ensure_default_today_tag()

        # Capture actual base height after first render
        def _capture_base_height():
            self._base_height = self.get_allocated_height()
            return False
        GLib.idle_add(_capture_base_height)

    def _ensure_default_today_tag(self):
        """Add a default 'scheduled today' tag if no date tag is present."""
        import datetime as _dt
        has_date = any(
            s.startswith("+") or s.startswith("due ") or s.startswith("starts ") or s.startswith("ends ")
            for s, _, _ in self.metadata_tags
        )
        if not has_date:
            self.metadata_tags.append(("+today", "today", "#89b4fa"))
            self._render_tags()

    def _apply_css(self, screen):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: #1e1e2e;
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
            .notes-field text {
                background-color: #313244;
                color: #cdd6f4;
                caret-color: #cdd6f4;
            }
            .notes-field {
                border: 1px solid #555;
                border-radius: 6px;
                padding: 2px;
                font-size: 13px;
            }
            .notes-field:focus-within {
                border-color: #89b4fa;
            }
            .screenshot-btn {
                background: transparent;
                border: 1px solid #555;
                border-radius: 6px;
                padding: 4px 8px;
                color: #cdd6f4;
                font-size: 14px;
                min-width: 0;
                min-height: 0;
            }
            .screenshot-btn:hover {
                background-color: #45475a;
                border-color: #89b4fa;
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
                tag_display = label
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

        # Replace existing tag of same type (category, date, etc.)
        # These triggers should only have one active tag at a time
        UNIQUE_TRIGGERS = {
            "#": lambda s: s.startswith("#") and not s.startswith("##"),
            "+": lambda s: s.startswith("+"),
            "due": lambda s: s.startswith("due "),
            "starts": lambda s: s.startswith("starts "),
            "ends": lambda s: s.startswith("ends "),
        }
        matcher = UNIQUE_TRIGGERS.get(trigger)
        if matcher:
            self.metadata_tags = [(s, d, c) for s, d, c in self.metadata_tags if not matcher(s)]

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

    def _on_notes_buffer_changed(self, buf):
        """Placeholder handled via CSS if needed — stub for future use."""
        pass

    def _on_notes_key_press(self, widget, event):
        state = event.state & Gtk.accelerator_get_default_mod_mask()
        # Ctrl+N: toggle notes off
        if event.keyval == Gdk.KEY_n and state == Gdk.ModifierType.CONTROL_MASK:
            self._toggle_notes()
            return True
        # Ctrl+Shift+S: screenshot
        if (event.keyval in (Gdk.KEY_s, Gdk.KEY_S) and
                state == (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)):
            self._on_screenshot_ocr()
            return True
        # Tab: return focus to entry
        if event.keyval == Gdk.KEY_Tab and state == 0:
            self.entry.grab_focus()
            return True
        # Escape: hide notes, return to entry
        if event.keyval == Gdk.KEY_Escape:
            self.notes_scroll.hide()
            self._resize_window()
            self.entry.grab_focus()
            return True
        return False

    def _toggle_notes(self):
        visible = self.notes_scroll.get_visible()
        if visible:
            self.notes_scroll.hide()
            self.entry.grab_focus()
        else:
            self.notes_view.show()
            self.notes_scroll.show()
            self.notes_view.grab_focus()
        self._resize_window()

    def _reposition_window(self, height):
        """Position window so bottom edge stays above the taskbar."""
        geo = self._monitor_geo
        x = geo.x + (geo.width - 600) // 2
        y = geo.y + geo.height - self._bottom_margin - height
        self.move(x, y)

    def _resize_window(self):
        def do_resize():
            base = self._base_height or 48
            notes_vis = self.notes_scroll.get_visible()
            status_vis = self.status_label.get_visible()
            h = base
            notes_h = 0
            status_h = 0
            if notes_vis:
                notes_h = self.notes_scroll.get_preferred_height()[1] + 4
                h += notes_h
            if status_vis:
                status_h = self.status_label.get_preferred_height()[1] + 4
                h += status_h
            geo = self._monitor_geo
            x = geo.x + (geo.width - 600) // 2
            y = geo.y + geo.height - self._bottom_margin - h
            # Force GTK to accept the smaller size
            self.set_size_request(600, h)
            gdk_win = self.get_window()
            if gdk_win:
                gdk_win.move_resize(x, y, 600, h)
            else:
                self.resize(600, h)
                self._reposition_window(h)
            return False
        GLib.idle_add(do_resize)

    def clean(self):
        """Clear all content: entry, notes, metadata tags, and status."""
        self.entry.set_text("")
        self.notes_view.get_buffer().set_text("")
        self.notes_scroll.hide()
        self.metadata_tags.clear()
        self._render_tags()
        self.status_label.hide()
        self._ensure_default_today_tag()
        self._resize_window()
        self.entry.grab_focus()

    def toggle_visibility(self):
        """Toggle window visibility. Only hide if widget is focused, otherwise bring to front."""
        if self.get_visible() and self.is_active():
            self.hide()
        elif self.get_visible():
            # Visible but not focused — bring to front
            self.present_with_time(Gdk.CURRENT_TIME)
            self.set_keep_above(True)
            gdk_win = self.get_window()
            if gdk_win:
                gdk_win.focus(Gdk.CURRENT_TIME)
            self.entry.grab_focus()
        else:
            # Hidden — show it
            self._notes_was_visible = self.notes_scroll.get_visible()
            self.show_all()
            self.status_label.hide()
            if not self._notes_was_visible:
                self.notes_scroll.hide()
            self.present_with_time(Gdk.CURRENT_TIME)
            self.set_keep_above(True)
            gdk_win = self.get_window()
            if gdk_win:
                gdk_win.focus(Gdk.CURRENT_TIME)
            self._resize_window()
            self.entry.grab_focus()

    def _build_submit_title(self):
        """Build the full title with shortcut strings appended for the API."""
        title = self.entry.get_text().strip()
        for shortcut_str, _, _ in self.metadata_tags:
            title += " " + shortcut_str
        return title

    def _on_entry_key_press(self, widget, event):
        state = event.state & Gtk.accelerator_get_default_mod_mask()

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

        # Ctrl+N: toggle notes field
        if event.keyval == Gdk.KEY_n and state == Gdk.ModifierType.CONTROL_MASK:
            self._toggle_notes()
            return True

        # Ctrl+Shift+S: screenshot OCR
        if (event.keyval in (Gdk.KEY_s, Gdk.KEY_S) and
                state == (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK)):
            self._on_screenshot_ocr()
            return True

        if event.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        return False

    # ── Submit ───────────────────────────────────────────────────────────

    def _on_submit(self, widget):
        if self.autocomplete.get_visible():
            return

        text = self.entry.get_text().strip()

        # Handle /clean command
        if text.lower() == "/clean":
            self.clean()
            return

        full_title = self._build_submit_title()
        if not full_title.strip():
            return

        # Extract note text
        buf = self.notes_view.get_buffer()
        note_text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

        self._last_display_title = self.entry.get_text().strip()
        self.entry.set_sensitive(False)
        self._set_status("Adding...", "#a6adc8")

        def do_add():
            try:
                payload = {"title": full_title}
                if note_text:
                    payload["note"] = note_text
                api_post("addTask", payload)
                GLib.idle_add(self._on_task_added, full_title)
            except Exception as e:
                GLib.idle_add(self._on_task_error, str(e))

        threading.Thread(target=do_add, daemon=True).start()

    def _on_task_added(self, title):
        display = self._last_display_title or title
        self._set_status(f"Added: {display}", "#a6e3a1")
        # Signal the widget with the actual display title and tags before clearing
        self._notify_widget_refresh(display, list(self.metadata_tags))
        self.entry.set_text("")
        self.metadata_tags.clear()
        self._render_tags()
        self.notes_view.get_buffer().set_text("")
        self.notes_scroll.hide()
        self._ensure_default_today_tag()
        self._resize_window()
        self.entry.set_sensitive(True)
        self.entry.grab_focus()
        GLib.timeout_add(3000, self._clear_status)

    def _clear_status(self):
        self.status_label.hide()
        return False

    def _notify_widget_refresh(self, display_title, tags):
        """Write optimistic task data and send SIGUSR2 to the widget."""
        import json as _json
        import datetime as _dt
        import uuid

        parent_id = "unassigned"
        for shortcut_str, _, _ in tags:
            if shortcut_str.startswith("#") and not shortcut_str.startswith("##"):
                parent_id = shortcut_str
                break

        placeholder = {
            "_id": f"_pending_{uuid.uuid4().hex[:8]}",
            "title": display_title,
            "parentId": parent_id,
            "day": _dt.date.today().isoformat(),
            "done": False,
        }

        cache_dir = os.path.expanduser("~/.cache/marvin-quick-add")
        os.makedirs(cache_dir, exist_ok=True)
        task_file = os.path.join(cache_dir, f"task_{uuid.uuid4().hex[:8]}.json")
        try:
            with open(task_file, "w") as f:
                _json.dump(placeholder, f)
        except OSError:
            pass

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
        self._resize_window()

    # ── Screenshot OCR ──────────────────────────────────────────────────

    def _on_screenshot_ocr(self):
        if not ANTHROPIC_API_KEY:
            self._set_status("No anthropic_api_key in config.json", "#f38ba8")
            return
        logging.info("Screenshot: hiding window for capture")
        self.hide()
        GLib.timeout_add(200, self._do_screenshot)

    def _do_screenshot(self):
        self._screenshot_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        self._screenshot_tmp.close()
        self._try_portal_screenshot()
        return False

    def _try_portal_screenshot(self):
        """Use xdg-desktop-portal Screenshot (works on Wayland)."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            self._portal_bus = bus
            token = f"marvin_{os.getpid()}"

            # The portal rewrites the handle using the sender's unique name,
            # replacing ":" with "" and "." with "_".
            unique = bus.get_unique_name()  # e.g. ":1.234"
            sender_part = unique.lstrip(":").replace(".", "_")
            handle = f"/org/freedesktop/portal/desktop/request/{sender_part}/{token}"
            logging.info(f"Screenshot: subscribing to portal signal at {handle}")

            self._portal_sub = bus.signal_subscribe(
                "org.freedesktop.portal.Desktop",
                "org.freedesktop.portal.Request",
                "Response",
                handle,
                None,
                Gio.DBusSignalFlags.NO_MATCH_RULE,
                self._on_portal_response,
                None,
            )

            result = bus.call_sync(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                GLib.Variant("(sa{sv})", ("", {
                    "interactive": GLib.Variant("b", True),
                    "handle_token": GLib.Variant("s", token),
                })),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                -1,
                None,
            )
            actual_handle = result.unpack()[0]
            logging.info(f"Screenshot: portal returned handle {actual_handle}")

            # If the portal returned a different handle, re-subscribe
            if actual_handle != handle:
                logging.info(f"Screenshot: re-subscribing to actual handle {actual_handle}")
                bus.signal_unsubscribe(self._portal_sub)
                self._portal_sub = bus.signal_subscribe(
                    "org.freedesktop.portal.Desktop",
                    "org.freedesktop.portal.Request",
                    "Response",
                    actual_handle,
                    None,
                    Gio.DBusSignalFlags.NO_MATCH_RULE,
                    self._on_portal_response,
                    None,
                )

            self._portal_timeout_id = GLib.timeout_add(30000, self._on_portal_timeout)
        except Exception as e:
            logging.error(f"Screenshot: portal failed: {e}", exc_info=True)
            self._fallback_screenshot_tools()

    def _on_portal_response(self, bus, sender, path, iface, signal, params, user_data):
        logging.info(f"Screenshot: portal response received on {path}")
        bus.signal_unsubscribe(self._portal_sub)
        if hasattr(self, "_portal_timeout_id"):
            GLib.source_remove(self._portal_timeout_id)

        response, results = params.unpack()
        logging.info(f"Screenshot: portal response={response}, results={results}")

        if response != 0:
            logging.warning(f"Screenshot: portal cancelled (response={response})")
            GLib.idle_add(self._on_screenshot_cancelled)
            self._cleanup_screenshot_tmp()
            return

        uri = results.get("uri", "")
        if not uri:
            logging.warning("Screenshot: portal returned empty URI")
            GLib.idle_add(self._on_screenshot_cancelled)
            self._cleanup_screenshot_tmp()
            return

        from urllib.parse import unquote, urlparse
        parsed = urlparse(uri)
        filepath = unquote(parsed.path) if parsed.scheme == "file" else uri
        logging.info(f"Screenshot: captured to {filepath}")
        try:
            with open(filepath, "rb") as f:
                img_data = base64.b64encode(f.read()).decode()
            logging.info(f"Screenshot: image loaded ({len(img_data)} bytes b64), sending to Haiku")
            # Show window immediately with processing status
            GLib.idle_add(self._show_and_status, "Analyzing screenshot...", "#a6adc8")
            threading.Thread(target=self._call_haiku_ocr, args=(img_data,), daemon=True).start()
        except Exception as e:
            logging.error(f"Screenshot: failed to read image: {e}", exc_info=True)
            GLib.idle_add(self._on_screenshot_error, str(e))
        self._cleanup_screenshot_tmp()

    def _on_portal_timeout(self):
        logging.warning("Screenshot: portal timeout (30s), giving up")
        if hasattr(self, "_portal_sub"):
            self._portal_bus.signal_unsubscribe(self._portal_sub)
        self._on_screenshot_cancelled()
        self._cleanup_screenshot_tmp()
        return False

    def _fallback_screenshot_tools(self):
        """Try CLI screenshot tools as fallback."""
        def capture():
            tmp_path = self._screenshot_tmp.name
            commands = [
                ["gnome-screenshot", "-a", "-f", tmp_path],
                ["scrot", "-s", tmp_path],
                ["maim", "-s", tmp_path],
                ["import", tmp_path],
            ]
            for cmd in commands:
                if shutil.which(cmd[0]) is None:
                    continue
                logging.info(f"Screenshot: trying fallback {cmd[0]}")
                try:
                    result = subprocess.run(cmd, timeout=30, capture_output=True)
                    if result.returncode == 0 and os.path.exists(tmp_path) and os.path.getsize(tmp_path) > 0:
                        with open(tmp_path, "rb") as f:
                            img_data = base64.b64encode(f.read()).decode()
                        logging.info(f"Screenshot: {cmd[0]} succeeded")
                        GLib.idle_add(self._show_and_status, "Analyzing screenshot...", "#a6adc8")
                        self._call_haiku_ocr(img_data)
                        return
                    else:
                        logging.warning(f"Screenshot: {cmd[0]} failed (rc={result.returncode})")
                except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                    logging.warning(f"Screenshot: {cmd[0]} error: {e}")
                    continue
            logging.error("Screenshot: no screenshot tool available")
            GLib.idle_add(self._on_screenshot_error, "No screenshot tool available")
            self._cleanup_screenshot_tmp()

        threading.Thread(target=capture, daemon=True).start()

    def _cleanup_screenshot_tmp(self):
        try:
            os.unlink(self._screenshot_tmp.name)
        except OSError:
            pass

    def _call_haiku_ocr(self, img_base64):
        config = load_config()
        category_hints = config.get("category_hints", {})
        user_names = config.get("user_names", [])

        cat_lines = []
        for c in self.categories:
            hint = category_hints.get(c["title"], "")
            if hint:
                cat_lines.append(f'- {c["title"]}: {hint}')
            else:
                cat_lines.append(f'- {c["title"]}')
        cat_block = "\n".join(cat_lines) if cat_lines else "none loaded"

        user_context = ""
        if user_names:
            names = ", ".join(user_names)
            user_context = (
                f"IMPORTANT: The user creating this task is: {names}. "
                "Any of these names refer to the SAME person — the user themselves. "
                "Create tasks from THEIR perspective (first person). "
                "Do NOT create tasks like 'Review X's PR' when X is the user — "
                "instead focus on what the user needs from others or what they need to do next.\n\n"
            )

        prompt_text = (
            f"{user_context}"
            "Analyze this screenshot and create an actionable task from it. "
            "If it's a chat or conversation, use older messages for context but create the task "
            "based on the MOST RECENT messages — what needs action NOW. "
            "Think about what the user likely needs to DO based on what you see. "
            "For example: if it's an error message, the task is to fix it; "
            "if it's a chat message, the task is to follow up or respond to the latest message; "
            "if it's a document, the task might be to review or complete it.\n\n"
            f"Available project categories (use ONLY one of these exact names, or null if none fit):\n{cat_block}\n\n"
            "Return ONLY valid JSON (no markdown, no code fences):\n"
            '{"title": "short actionable task title", '
            '"notes": "relevant details extracted from the screenshot", '
            '"category": "exact category name or null"}'
        )

        payload = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_base64,
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }],
        }
        try:
            logging.info("OCR: calling Haiku API...")
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
            logging.info(f"OCR: raw API response: {raw[:300]}")
            result = json.loads(raw)

            if result.get("type") == "error":
                err_msg = result.get("error", {}).get("message", "Unknown API error")
                logging.error(f"OCR: API error response: {err_msg}")
                GLib.idle_add(self._on_screenshot_error, err_msg)
                return

            text = result["content"][0]["text"].strip()
            logging.info(f"OCR: Haiku text: {text[:200]}")

            # Strip markdown code fences if present
            import re
            cleaned = re.sub(r'^```(?:json)?\s*', '', text)
            cleaned = re.sub(r'\s*```$', '', cleaned).strip()

            parsed = json.loads(cleaned)
            title = parsed.get("title", "").strip()
            notes = parsed.get("notes", "").strip()
            category = parsed.get("category")
            logging.info(f"OCR: title={title!r}, category={category!r}")
            GLib.idle_add(self._on_ocr_result, title, notes, category)
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            logging.error(f"OCR: HTTP {e.code}: {body[:300]}")
            GLib.idle_add(self._on_screenshot_error, f"API HTTP {e.code}")
        except Exception as e:
            logging.error(f"OCR: error: {e}", exc_info=True)
            GLib.idle_add(self._on_screenshot_error, str(e))

    def _on_ocr_result(self, title, notes, category):
        self._reshow_window()
        self.entry.handler_block_by_func(self._on_entry_changed)
        self.entry.set_text(title)
        self.entry.set_position(len(title))
        self.entry.handler_unblock_by_func(self._on_entry_changed)
        if notes:
            self.notes_view.get_buffer().set_text(notes)
            self.notes_view.show()
            self.notes_scroll.show()
        # Auto-add category tag if matched
        if category:
            cat = next((c for c in self.categories if c["title"].lower() == category.lower()), None)
            if cat:
                shortcut_str = f"#{cat['title']}"
                tag_display = f"in {cat['title']}"
                color = cat.get("color", "#888")
                self.metadata_tags.append((shortcut_str, tag_display, color))
                self._render_tags()
        self._set_status("Task created from screenshot", "#a6e3a1")
        self._resize_window()
        self.entry.grab_focus()

    def _reshow_window(self):
        """Bring the window back after hiding for screenshot."""
        self.show_all()
        self.status_label.hide()
        self.present_with_time(Gdk.CURRENT_TIME)
        self.set_keep_above(True)

    def _show_and_status(self, text, color):
        self._reshow_window()
        self._set_status(text, color)

    def _on_screenshot_cancelled(self):
        self._reshow_window()
        self._set_status("Screenshot cancelled", "#a6adc8")
        self.entry.grab_focus()

    def _on_screenshot_error(self, error):
        self._reshow_window()
        self._set_status(f"Error: {error}", "#f38ba8")
        self.entry.grab_focus()


PIDFILE = os.path.expanduser("~/.cache/marvin-quick-add.pid")


def ensure_single_instance():
    """If already running, signal it to toggle visibility, then exit."""
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # Check alive
            os.kill(pid, signal.SIGUSR1)  # Toggle visibility
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
    global API_TOKEN, ANTHROPIC_API_KEY
    API_TOKEN = load_token()
    ANTHROPIC_API_KEY = load_config().get("anthropic_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")

    ensure_single_instance()
    import atexit
    atexit.register(cleanup_pidfile)

    win = QuickAddWindow()
    win.show_all()
    win.status_label.hide()
    win.present_with_time(Gdk.CURRENT_TIME)
    win.entry.grab_focus()

    def on_sigusr1(signum, frame):
        GLib.idle_add(win.toggle_visibility)

    signal.signal(signal.SIGUSR1, on_sigusr1)

    Gtk.main()


if __name__ == "__main__":
    main()
