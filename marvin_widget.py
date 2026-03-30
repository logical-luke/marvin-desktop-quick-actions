#!/usr/bin/python3
"""Amazing Marvin Desktop Widget — persistent floating task list with tick-to-complete."""

import gi
import json
import urllib.request
import urllib.error
import threading
import datetime
import signal
import os
import sys

os.environ["GDK_BACKEND"] = "x11"  # Force XWayland so move/keep_above/stick work on GNOME

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, Gdk, GLib, Pango
from gi.repository import AyatanaAppIndicator3 as AppIndicator3

API_BASE = "https://serv.amazingmarvin.com/api"
WIDGET_WIDTH = 340
TASK_POLL_MS = 60_000       # Poll tasks every 60s
CATEGORY_POLL_MS = 300_000  # Poll categories every 5min

PIDFILE = os.path.expanduser("~/.cache/marvin-widget.pid")
CONFIG_PATH = os.path.expanduser("~/.config/marvin-widget/config.json")


# ── Config ───────────────────────────────────────────────────────────────────

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_tokens():
    config = load_config()
    api_token = config.get("api_token", "") or os.environ.get("MARVIN_API_TOKEN", "")
    full_token = config.get("full_access_token", "") or os.environ.get("MARVIN_FULL_TOKEN", "")
    if not api_token:
        print(f"Error: No API token. Set 'api_token' in {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    return api_token, full_token


def get_api_token():
    config = load_config()
    token = config.get("api_token", "")
    if not token:
        token = os.environ.get("MARVIN_API_TOKEN", "")
    if not token:
        print(f"Error: No API token. Set 'api_token' in {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    return token


# ── API helpers ──────────────────────────────────────────────────────────────

class MarvinAPI:
    def __init__(self, token, full_token=""):
        self.token = token
        self.full_token = full_token

    def _request(self, method, path, data=None, use_full_token=False):
        headers = {"Content-Type": "application/json"}
        if use_full_token and self.full_token:
            headers["X-Full-Access-Token"] = self.full_token
        else:
            headers["X-API-Token"] = self.token
        req = urllib.request.Request(
            f"{API_BASE}/{path}",
            data=json.dumps(data).encode() if data else None,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def get_categories(self):
        return self._request("GET", "categories")

    def get_labels(self):
        return self._request("GET", "labels")

    def get_today_tasks(self):
        today = datetime.date.today().isoformat()
        return self._request("GET", f"todayItems?date={today}")

    def get_done_today(self):
        today = datetime.date.today().isoformat()
        return self._request("GET", f"doneItems?date={today}")

    def mark_done(self, task_id):
        return self._request("POST", "markDone", {
            "itemId": task_id,
            "doneAt": int(datetime.datetime.now().timestamp() * 1000),
        })

    def mark_undone(self, task_id):
        return self._request("POST", "doc/update", {
            "itemId": task_id,
            "setters": [
                {"key": "done", "val": False},
                {"key": "doneAt", "val": 0},
            ],
        }, use_full_token=True)

    def update_rank(self, task_id, new_rank):
        return self._request("POST", "doc/update", {
            "itemId": task_id,
            "setters": [{"key": "rank", "val": new_rank}],
        }, use_full_token=True)


# ── Data store with polling ──────────────────────────────────────────────────

class DataStore:
    def __init__(self, api):
        self.api = api
        self.categories = []
        self.labels = []
        self.tasks = []
        self.done_tasks = []
        self.category_map = {}
        self.tasks_loaded = False
        self.on_update = None

    def start_polling(self):
        self._poll_categories()
        self._poll_tasks()
        GLib.timeout_add(CATEGORY_POLL_MS, self._poll_categories)
        GLib.timeout_add(TASK_POLL_MS, self._poll_tasks)

    def refresh_tasks_now(self):
        self._poll_tasks()

    def reorder_tasks(self, task_id, new_index):
        """Move a task to a new position and update ranks via API."""
        incomplete = [t for t in self.tasks if not t.get("done")]
        old_index = next((i for i, t in enumerate(incomplete) if t.get("_id") == task_id), None)
        if old_index is None or old_index == new_index:
            return

        # Reorder locally
        task = incomplete.pop(old_index)
        incomplete.insert(new_index, task)

        # Update local tasks list (keep done tasks at end)
        done = [t for t in self.tasks if t.get("done")]
        self.tasks = incomplete + done
        if self.on_update:
            self.on_update()

        # Update ranks via API in background
        def do_update():
            import time
            for i, t in enumerate(incomplete):
                t["rank"] = i
                try:
                    self.api.update_rank(t["_id"], i)
                    time.sleep(1)  # Rate limit
                except Exception as e:
                    print(f"Failed to update rank for {t.get('title','')}: {e}", file=sys.stderr)

        threading.Thread(target=do_update, daemon=True).start()

    def mark_task_done(self, task_id):
        # Optimistic update: move from tasks to done_tasks
        task = next((t for t in self.tasks if t.get("_id") == task_id), None)
        if task:
            task["done"] = True
            task["doneAt"] = int(datetime.datetime.now().timestamp() * 1000)
            self.tasks = [t for t in self.tasks if t.get("_id") != task_id]
            self.done_tasks.insert(0, task)
        if self.on_update:
            self.on_update()

        def do_mark():
            try:
                self.api.mark_done(task_id)
            except Exception as e:
                print(f"Failed to mark done: {e}", file=sys.stderr)
                GLib.idle_add(self._poll_tasks)

        threading.Thread(target=do_mark, daemon=True).start()

    def mark_task_undone(self, task_id):
        # Optimistic update: move from done_tasks back to tasks
        task = next((t for t in self.done_tasks if t.get("_id") == task_id), None)
        if task:
            task["done"] = False
            task["doneAt"] = 0
            self.done_tasks = [t for t in self.done_tasks if t.get("_id") != task_id]
            self.tasks.append(task)
        if self.on_update:
            self.on_update()

        def do_undone():
            try:
                self.api.mark_undone(task_id)
            except Exception as e:
                print(f"Failed to mark undone: {e}", file=sys.stderr)
                GLib.idle_add(self._poll_tasks)

        threading.Thread(target=do_undone, daemon=True).start()

    def _poll_categories(self):
        def fetch():
            try:
                cats = self.api.get_categories()
                labels = self.api.get_labels()
                GLib.idle_add(self._update_categories, cats, labels)
            except Exception as e:
                print(f"Categories fetch failed: {e}", file=sys.stderr)

        threading.Thread(target=fetch, daemon=True).start()
        return True

    def _poll_tasks(self):
        def fetch():
            try:
                tasks = self.api.get_today_tasks()
                import time
                time.sleep(3)  # Rate limit between reads
                done = self.api.get_done_today()
                GLib.idle_add(self._update_tasks, tasks, done)
            except Exception as e:
                print(f"Tasks fetch failed: {e}", file=sys.stderr)

        threading.Thread(target=fetch, daemon=True).start()
        return True

    def _update_categories(self, cats, labels):
        self.categories = [
            {"title": c["title"], "color": c.get("color", "#888"), "id": c["_id"]}
            for c in cats
        ]
        self.labels = [
            {"title": l["title"], "color": l.get("color", "#888"), "id": l["_id"]}
            for l in labels
        ]
        self.category_map = {c["id"]: c for c in self.categories}
        self.category_name_map = {c["title"].lower(): c for c in self.categories}
        if self.on_update:
            self.on_update()

    def _update_tasks(self, tasks, done=None):
        self.tasks = tasks
        if done is not None:
            self.done_tasks = done
        self.tasks_loaded = True
        if self.on_update:
            self.on_update()


import re

_SHORTCUT_PATTERNS = [
    r'##\S+',                          # ##goal
    r'#\S+',                           # #project
    r'@\S+',                           # @label
    r'\+\S+',                          # +tomorrow
    r'~\d+[hm]\S*',                    # ~30m, ~1h30m
    r'!\S+',                           # !morning
    r'\*p[0-3]',                       # *p1
    r'\*[Rr]eward',                    # *Reward
    r'\$\d+',                          # $5
    r'&\S+(?:\s+\S+)?',               # &Next Week
    r'\bdue\s+\S+(?:\s+\S+)?',        # due next week
    r'\bstarts\s+\S+(?:\s+\S+)?',     # starts next month
    r'\bends\s+\S+(?:\s+\S+)?',       # ends next month
    r'\breview\s+\S+',                 # review 2026-04-01
    r'\|\d+d',                         # |5d
    r'=\d+d',                          # =5d
    r'--\S.*',                         # --note text
]
_SHORTCUT_RE = re.compile('|'.join(f'(?:{p})' for p in _SHORTCUT_PATTERNS))


def _clean_title(title):
    """Strip shortcut syntax from a task title for display."""
    cleaned = _SHORTCUT_RE.sub('', title)
    return ' '.join(cleaned.split()).strip()


def _parse_color(hex_color):
    rgba = Gdk.RGBA()
    rgba.parse(hex_color or "#888888")
    return rgba


# ── Main widget ──────────────────────────────────────────────────────────────

class MarvinWidget(Gtk.Window):
    def __init__(self, store):
        super().__init__(title="Marvin Tasks")
        self.set_wmclass("marvin-tasks", "marvin-tasks")
        self.store = store
        self.store.on_update = self._on_data_updated

        self.set_default_size(WIDGET_WIDTH, 400)
        self.set_decorated(False)
        self.set_resizable(True)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(False)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        self.set_icon_from_file(icon_path)
        self.stick()  # All workspaces

        # Position bottom-right of screen
        screen = Gdk.Screen.get_default()
        monitor_geo = screen.get_monitor_geometry(screen.get_primary_monitor())
        self.move(
            monitor_geo.x + monitor_geo.width - WIDGET_WIDTH - 16,
            monitor_geo.y + monitor_geo.height - 400 - 100,
        )

        self._initial_load = True
        self._apply_css()
        self._build_ui()

        self.connect("delete-event", self._on_delete)

    def _apply_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: #121317;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Roboto, Helvetica, Arial, sans-serif;
            }
            .header-bar {
                background-color: #5b9dff;
                padding: 14px 16px;
            }
            .header-title {
                color: #ffffff;
                font-size: 15px;
                font-weight: bold;
            }
            .header-btn {
                color: rgba(255,255,255,0.8);
                padding: 0;
                min-width: 24px;
                min-height: 24px;
                background: none;
                border: none;
            }
            .header-btn:hover {
                color: #ffffff;
            }
            .task-card {
                background-color: #202125;
                border-radius: 9px;
                padding: 17px 17px 17px 12px;
                border: none;
            }
            .task-card:hover {
                background-color: #3b3b3b;
            }
            .task-title {
                color: #ffffff;
                font-size: 14px;
            }
            .task-check {
                color: rgba(255,255,255,0.3);
                font-size: 18px;
                padding: 0;
                min-width: 20px;
                min-height: 20px;
                border: 2px solid rgba(255,255,255,0.3);
                border-radius: 50%;
                background: transparent;
            }
            .task-check:hover {
                border-color: #5bdb66;
                color: transparent;
            }
            .done-check {
                color: #5bdb66;
                font-size: 16px;
                padding: 0;
                min-width: 22px;
                min-height: 22px;
                border: none;
                background: transparent;
            }
            .done-check:hover {
                color: #808185;
            }
            .done-title {
                color: #808185;
                font-size: 13px;
                font-style: italic;
            }
            .completed-header {
                color: #ffffff;
                font-size: 14px;
                font-weight: bold;
                padding: 8px 0;
            }
            .completed-toggle {
                background: none;
                border: none;
                color: #ffffff;
                font-size: 14px;
                font-weight: bold;
                padding: 8px 0;
            }
            .completed-toggle:hover {
                color: #5b9dff;
            }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Blue header bar (like Marvin) with drag support
        header = Gtk.EventBox()
        header.get_style_context().add_class("header-bar")
        header.connect("button-press-event", self._on_header_press)
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title = Gtk.Label(label="Today")
        title.get_style_context().add_class("header-title")
        title.set_halign(Gtk.Align.START)
        header_box.pack_start(title, True, True, 0)
        self.header_title = title
        # Refresh button
        refresh_btn = Gtk.Button(label="\u21bb")
        refresh_btn.get_style_context().add_class("header-btn")
        refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
        refresh_btn.connect("clicked", lambda b: self.store.refresh_tasks_now())
        header_box.pack_end(refresh_btn, False, False, 0)
        # Hide button
        hide_btn = Gtk.Button(label="\u2013")
        hide_btn.get_style_context().add_class("header-btn")
        hide_btn.set_relief(Gtk.ReliefStyle.NONE)
        hide_btn.connect("clicked", lambda b: self.hide())
        header_box.pack_end(hide_btn, False, False, 4)
        header.add(header_box)
        main_box.pack_start(header, False, False, 0)

        # Scrollable content area
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_start(14)
        content.set_margin_end(14)
        content.set_margin_top(16)
        content.set_margin_bottom(14)

        # Active task list
        self.task_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.pack_start(self.task_list, False, False, 0)

        # Completed section toggle
        self.completed_expanded = False
        self.completed_btn = Gtk.Button()
        self.completed_btn.get_style_context().add_class("completed-toggle")
        self.completed_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.completed_btn.set_halign(Gtk.Align.START)
        self.completed_btn.set_margin_top(16)
        self.completed_btn.connect("clicked", self._toggle_completed)
        self.completed_btn.set_no_show_all(True)
        content.pack_start(self.completed_btn, False, False, 0)

        # Completed task list (hidden by default)
        self.done_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.done_list.set_margin_top(8)
        self.done_list.set_no_show_all(True)
        content.pack_start(self.done_list, False, False, 0)

        scrolled.add(content)
        main_box.pack_start(scrolled, True, True, 0)

        self.add(main_box)

    def _on_header_press(self, widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    # ── Task list rendering ──────────────────────────────────────────────

    def _on_data_updated(self):
        if not self.store.tasks_loaded:
            return  # Wait for tasks to load before rendering
        self._initial_load = False
        self._render_tasks()

    def _render_tasks(self):
        # Active tasks
        for child in self.task_list.get_children():
            self.task_list.remove(child)

        if self._initial_load:
            self.header_title.set_text("Loading...")
            spinner = Gtk.Spinner()
            spinner.start()
            spinner.set_margin_top(40)
            spinner.set_margin_bottom(40)
            spinner.set_halign(Gtk.Align.CENTER)
            self.task_list.pack_start(spinner, False, False, 0)
            self.task_list.show_all()
            return

        incomplete = [t for t in self.store.tasks if not t.get("done")]
        self.header_title.set_text(f"Today \u00B7 {len(incomplete)} tasks")

        if not incomplete:
            empty = Gtk.Label(label="All done for today!")
            empty.set_margin_top(20)
            empty.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
            self.task_list.pack_start(empty, False, False, 0)
        else:
            for i, task in enumerate(incomplete):
                row = self._make_task_row(task, i)
                self.task_list.pack_start(row, False, False, 0)

        self.task_list.show_all()

        # Completed section
        done = self.store.done_tasks
        if done:
            arrow = "\u25BC" if self.completed_expanded else "\u25B6"
            self.completed_btn.set_label(f"{arrow} Completed Today ({len(done)})")
            self.completed_btn.show()
        else:
            self.completed_btn.hide()
            self.done_list.hide()

        self._render_done_tasks()

    def _make_task_row(self, task, index):
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("task-card")
        frame.task_id = task.get("_id")
        frame.task_index = index

        # Drag source
        frame.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.MOVE
        )
        frame.drag_source_add_text_targets()
        frame.connect("drag-data-get", self._on_drag_data_get)
        frame.connect("drag-begin", self._on_drag_begin)

        # Drop target
        frame.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.MOVE)
        frame.drag_dest_add_text_targets()
        frame.connect("drag-data-received", self._on_drag_data_received)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Clickable checkbox — thin circle outline
        check_btn = Gtk.Button(label=" ")
        check_btn.get_style_context().add_class("task-check")
        check_btn.set_relief(Gtk.ReliefStyle.NONE)
        check_btn.set_valign(Gtk.Align.CENTER)
        task_id = task.get("_id")
        check_btn.connect("clicked", lambda b, tid=task_id: self._on_task_check(tid))
        hbox.pack_start(check_btn, False, False, 0)

        # Title
        title_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        title_label.get_style_context().add_class("task-title")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_line_wrap(True)
        title_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_xalign(0)
        hbox.pack_start(title_label, True, True, 0)

        vbox.pack_start(hbox, False, False, 0)

        # Category badge — colored pill like Marvin
        parent_id = task.get("parentId")
        cat = self._resolve_category(parent_id)
        if cat:
            badge = Gtk.Label()
            color = cat["color"]
            title = GLib.markup_escape_text(cat["title"])
            badge.set_markup(f'<span font_size="x-small" weight="bold">#{title}</span>')
            badge_css = Gtk.CssProvider()
            badge_css.load_from_data(
                f".cat-badge {{ background-color: {color}; color: #ffffff; "
                f"border-radius: 3px; padding: 2px 8px; }}".encode()
            )
            badge.get_style_context().add_provider(badge_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            badge.get_style_context().add_class("cat-badge")
            badge.set_halign(Gtk.Align.START)
            badge.set_margin_start(28)
            badge.set_margin_top(2)
            vbox.pack_start(badge, False, False, 0)

        frame.add(vbox)
        return frame

    def _resolve_category(self, parent_id):
        """Resolve parentId to category — handles both IDs and #Name shortcuts."""
        if not parent_id:
            return None
        cat = self.store.category_map.get(parent_id)
        if cat:
            return cat
        # parentId might be "#Name" from shortcut syntax
        if parent_id.startswith("#"):
            name = parent_id[1:].lower()
            return self.store.category_name_map.get(name)
        return None

    def _toggle_completed(self, btn):
        self.completed_expanded = not self.completed_expanded
        done = self.store.done_tasks
        arrow = "\u25BC" if self.completed_expanded else "\u25B6"
        self.completed_btn.set_label(f"{arrow} Completed Today ({len(done)})")
        if self.completed_expanded:
            self.done_list.set_no_show_all(False)
            self._render_done_tasks()
            self.done_list.show_all()
        else:
            self.done_list.hide()
            self.done_list.set_no_show_all(True)

    def _render_done_tasks(self):
        for child in self.done_list.get_children():
            self.done_list.remove(child)

        if not self.completed_expanded:
            return

        for task in self.store.done_tasks:
            row = self._make_done_row(task)
            self.done_list.pack_start(row, False, False, 0)

        self.done_list.show_all()

    def _make_done_row(self, task):
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("task-card")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Green checkmark — click to uncomplete
        check_btn = Gtk.Button(label="\u2714")
        check_btn.get_style_context().add_class("done-check")
        check_btn.set_relief(Gtk.ReliefStyle.NONE)
        check_btn.set_valign(Gtk.Align.CENTER)
        task_id = task.get("_id")
        check_btn.connect("clicked", lambda b, tid=task_id: self._on_task_uncheck(tid))
        hbox.pack_start(check_btn, False, False, 0)

        # Title (grayed out)
        title_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        title_label.get_style_context().add_class("done-title")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_line_wrap(True)
        title_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_xalign(0)
        hbox.pack_start(title_label, True, True, 0)

        vbox.pack_start(hbox, False, False, 0)

        # Category badge
        parent_id = task.get("parentId")
        cat = self._resolve_category(parent_id)
        if cat:
            badge = Gtk.Label()
            color = cat["color"]
            title = GLib.markup_escape_text(cat["title"])
            badge.set_markup(f'<span font_size="x-small" weight="bold">#{title}</span>')
            badge_css = Gtk.CssProvider()
            badge_css.load_from_data(
                f".cat-badge {{ background-color: {color}; color: #ffffff; "
                f"border-radius: 3px; padding: 2px 8px; }}".encode()
            )
            badge.get_style_context().add_provider(badge_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            badge.get_style_context().add_class("cat-badge")
            badge.set_halign(Gtk.Align.START)
            badge.set_margin_start(28)
            badge.set_margin_top(2)
            vbox.pack_start(badge, False, False, 0)

        frame.add(vbox)
        return frame

    def _on_task_uncheck(self, task_id):
        self.store.mark_task_undone(task_id)

    # ── Drag and drop ──────────────────────────────────────────────────

    def _on_drag_begin(self, widget, context):
        # Make the dragged row semi-transparent
        widget.set_opacity(0.5)

    def _on_drag_data_get(self, widget, context, selection, info, time):
        selection.set_text(widget.task_id, -1)

    def _on_drag_data_received(self, widget, context, x, y, selection, info, time):
        source_id = selection.get_text()
        target_index = widget.task_index
        # Reset opacity on all rows
        for child in self.task_list.get_children():
            child.set_opacity(1.0)
        if source_id and source_id != widget.task_id:
            self.store.reorder_tasks(source_id, target_index)

    def _on_task_check(self, task_id):
        self.store.mark_task_done(task_id)

    # ── Visibility ───────────────────────────────────────────────────────

    def _reposition(self):
        screen = Gdk.Screen.get_default()
        geo = screen.get_monitor_geometry(screen.get_primary_monitor())
        self.move(
            geo.x + geo.width - WIDGET_WIDTH - 16,
            geo.y + geo.height - 400 - 100,
        )

    def toggle_visibility(self):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self._reposition()
            self.set_keep_above(True)
            self.stick()
            self.present()

    def _on_delete(self, widget, event):
        # Hide instead of destroying — keeps the app running in tray
        self.hide()
        return True  # Prevent actual window destruction


# ── Tray indicator ───────────────────────────────────────────────────────────

def create_indicator(widget):
    icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
    indicator = AppIndicator3.Indicator.new(
        "marvin-widget",
        icon_path,
        AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
    )
    indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
    indicator.set_title("Amazing Marvin")

    menu = Gtk.Menu()

    show_item = Gtk.MenuItem(label="Show / Hide Widget")
    show_item.connect("activate", lambda _: widget.toggle_visibility())
    menu.append(show_item)

    refresh_item = Gtk.MenuItem(label="Refresh Tasks")
    refresh_item.connect("activate", lambda _: widget.store.refresh_tasks_now())
    menu.append(refresh_item)

    menu.append(Gtk.SeparatorMenuItem())

    quit_item = Gtk.MenuItem(label="Quit")
    quit_item.connect("activate", lambda _: Gtk.main_quit())
    menu.append(quit_item)

    menu.show_all()
    indicator.set_menu(menu)
    return indicator


# ── Single instance ──────────────────────────────────────────────────────────

def ensure_single_instance():
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            os.kill(pid, signal.SIGUSR1)
            sys.exit(0)
        except (ProcessLookupError, ValueError):
            pass

    os.makedirs(os.path.dirname(PIDFILE), exist_ok=True)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def cleanup_pidfile():
    try:
        os.remove(PIDFILE)
    except OSError:
        pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ensure_single_instance()
    import atexit
    atexit.register(cleanup_pidfile)

    api_token, full_token = get_tokens()
    api = MarvinAPI(api_token, full_token)
    store = DataStore(api)

    widget = MarvinWidget(store)
    widget._render_tasks()  # Show spinner before data loads
    widget.show_all()

    create_indicator(widget)
    store.start_polling()

    def on_sigusr1(signum, frame):
        GLib.idle_add(widget.toggle_visibility)

    def on_sigusr2(signum, frame):
        GLib.idle_add(store.refresh_tasks_now)

    signal.signal(signal.SIGUSR1, on_sigusr1)
    signal.signal(signal.SIGUSR2, on_sigusr2)

    Gtk.main()


if __name__ == "__main__":
    main()
