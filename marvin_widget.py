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
WIDGET_WIDTH = 380
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


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def get_hidden_tasks():
    return set(load_config().get("hidden_tasks", []))


def set_hidden_tasks(hidden_set):
    config = load_config()
    config["hidden_tasks"] = list(hidden_set)
    save_config(config)


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
            body = resp.read().decode().strip()
            return json.loads(body) if body else None

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

    def add_task(self, title, parent_id=None, day=None):
        data = {"title": title}
        if parent_id:
            data["parentId"] = parent_id
        if day:
            data["day"] = day
        return self._request("POST", "addTask", data)

    def update_task(self, task_id, setters):
        return self._request("POST", "doc/update", {
            "itemId": task_id,
            "setters": [{"key": k, "val": v} for k, v in setters.items()],
        }, use_full_token=True)

    def delete_task(self, task_id):
        return self._request("POST", "doc/delete", {
            "itemId": task_id,
        }, use_full_token=True)

    def get_tracked_item(self):
        """Get the currently tracked task, or None."""
        try:
            return self._request("GET", "trackedItem")
        except urllib.error.HTTPError:
            return None

    def start_tracking(self, task_id):
        return self._request("POST", "track", {"taskId": task_id, "action": "START"})

    def stop_tracking(self, task_id):
        return self._request("POST", "track", {"taskId": task_id, "action": "STOP"})

    def get_times(self, task_ids):
        """Get time tracking data for tasks. Returns list of {taskId, times}."""
        return self._request("POST", "tracks", {"taskIds": task_ids})

    def update_rank(self, task_id, new_rank):
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        return self._request("POST", "doc/update", {
            "itemId": task_id,
            "setters": [
                {"key": "dayRank", "val": new_rank},
                {"key": "fieldUpdates.dayRank", "val": now_ms},
            ],
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
        self._reorder_in_progress = False
        self.tracked_task_id = None
        self.on_update = None

    def start_polling(self):
        self._poll_categories()
        self._poll_tasks()
        self._poll_tracked()
        GLib.timeout_add(CATEGORY_POLL_MS, self._poll_categories)
        GLib.timeout_add(TASK_POLL_MS, self._poll_tasks)
        GLib.timeout_add(5000, self._poll_tracked)  # Poll tracked item every 5s

    def refresh_tasks_now(self):
        self._poll_tasks()

    def reorder_tasks(self, task_id, target_task_id, after=False):
        """Move task_id next to target_task_id (before it, or after if after=True).
        If target_task_id is None, move to top (after=False) or bottom (after=True)."""
        # Sort by dayRank first — must match display order
        incomplete = [t for t in self.tasks if not t.get("done")]
        incomplete.sort(key=lambda t: t.get("dayRank", t.get("rank", 0)))

        old_index = next((i for i, t in enumerate(incomplete) if t.get("_id") == task_id), None)
        if old_index is None:
            return

        if target_task_id is None:
            target_index = len(incomplete) - 1 if after else 0
        else:
            target_index = next((i for i, t in enumerate(incomplete) if t.get("_id") == target_task_id), None)
            if target_index is None:
                return

        # Remove from old position
        task = incomplete.pop(old_index)

        # Compute insert position (indices shifted after pop)
        insert_at = target_index if target_index <= old_index else target_index - 1
        if after:
            insert_at += 1
        insert_at = max(0, min(insert_at, len(incomplete)))

        if insert_at == old_index and len(incomplete) > 0:
            # No change needed, put it back
            incomplete.insert(old_index, task)
            return

        incomplete.insert(insert_at, task)

        # Compute new dayRank for the moved task by fitting between neighbors
        def _get_rank(t):
            return t.get("dayRank", t.get("rank", 0))

        if insert_at == 0:
            neighbor_rank = _get_rank(incomplete[1]) if len(incomplete) > 1 else 0
            new_rank = neighbor_rank - 1
        elif insert_at >= len(incomplete) - 1:
            neighbor_rank = _get_rank(incomplete[-2]) if len(incomplete) > 1 else 0
            new_rank = neighbor_rank + 1
        else:
            prev_rank = _get_rank(incomplete[insert_at - 1])
            next_rank = _get_rank(incomplete[insert_at + 1])
            new_rank = (prev_rank + next_rank) / 2

        task["dayRank"] = new_rank

        # Update local tasks list (keep done tasks at end)
        done = [t for t in self.tasks if t.get("done")]
        self.tasks = incomplete + done
        if self.on_update:
            self.on_update()

        # Only update the moved task via API
        self._reorder_in_progress = True
        moved_id = task["_id"]

        def do_update():
            try:
                self.api.update_rank(moved_id, new_rank)
            except Exception as e:
                print(f"Failed to update rank: {e}", file=sys.stderr)
            finally:
                self._reorder_in_progress = False

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
                import time
                tasks = self.api.get_today_tasks()
                time.sleep(3)
                done = self.api.get_done_today()
                GLib.idle_add(self._update_tasks, tasks, done)
            except Exception as e:
                print(f"Tasks fetch failed: {e}", file=sys.stderr)

        threading.Thread(target=fetch, daemon=True).start()
        return True

    def _poll_tracked(self):
        def fetch():
            try:
                tracked = self.api.get_tracked_item()
                new_id = tracked.get("_id") if tracked and isinstance(tracked, dict) else None
                if new_id != self.tracked_task_id:
                    self.tracked_task_id = new_id
                    GLib.idle_add(self._notify_update)
            except Exception as e:
                pass  # Silently ignore tracking poll failures
        threading.Thread(target=fetch, daemon=True).start()
        return True

    def _notify_update(self):
        if self.on_update:
            self.on_update()

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
        if self._reorder_in_progress:
            return
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


# Auto-emoji: keyword patterns → emoji (similar to Marvin's auto-detect)
_EMOJI_KEYWORDS = [
    # Household / errands
    (r'\b(?:pickup|pick up|collect|grab)\b', '\U0001F4E6'),   # package
    (r'\b(?:clean|cleanup|tidy|sweep|mop|vacuum)\b', '\U0001F9F9'),  # broom
    (r'\b(?:filter|filters)\b', '\U0001F4E6'),                # package
    (r'\b(?:cook|cooking|meal|dinner|lunch|breakfast)\b', '\U0001F373'),  # cooking
    (r'\b(?:laundry|wash|washing)\b', '\U0001F9FA'),          # basket
    (r'\b(?:shop|shopping|buy|purchase|order)\b', '\U0001F6D2'),  # cart
    (r'\b(?:grocery|groceries)\b', '\U0001F34E'),             # apple
    (r'\b(?:trash|garbage|rubbish)\b', '\U0001F5D1'),         # wastebasket
    # Work / tech
    (r'\b(?:fix|bug|debug|patch)\b', '\U0001F527'),           # wrench
    (r'\b(?:deploy|release|ship)\b', '\U0001F680'),           # rocket
    (r'\b(?:review|pr|pull request)\b', '\U0001F50D'),        # magnifier
    (r'\b(?:test|testing|tests)\b', '\U0001F9EA'),            # test tube
    (r'\b(?:doc|docs|document|documentation|write up)\b', '\U0001F4DD'),  # memo
    (r'\b(?:design|diagram|wireframe|mockup|sequence)\b', '\U0001F3A8'),  # palette
    (r'\b(?:meeting|sync|standup|stand-up|call)\b', '\U0001F4DE'),  # phone
    (r'\b(?:email|mail|send)\b', '\U0001F4E7'),               # email
    (r'\b(?:setup|set up|install|configure)\b', '\u2699\uFE0F'),  # gear
    (r'\b(?:budget|budgeting|finance|invoice|pay|payment)\b', '\U0001F4B0'),  # money bag
    # Health / fitness
    (r'\b(?:exercise|workout|gym|run|jog|walk)\b', '\U0001F3C3'),  # runner
    (r'\b(?:meditat|yoga|stretch)\b', '\U0001F9D8'),          # meditation
    (r'\b(?:doctor|dentist|appointment|checkup)\b', '\U0001F3E5'),  # hospital
    # Learning
    (r'\b(?:read|reading|book)\b', '\U0001F4D6'),             # book
    (r'\b(?:learn|study|course|tutorial)\b', '\U0001F393'),   # graduation cap
    (r'\b(?:research|investigate|explore)\b', '\U0001F50E'),  # magnifier right
    # Social
    (r'\b(?:call|phone|ring)\b', '\U0001F4F1'),               # mobile
    (r'\b(?:gift|present|birthday)\b', '\U0001F381'),         # gift
    # Chess
    (r'\b(?:chess)\b', '\u265F\uFE0F'),                       # chess pawn
    # General
    (r'\b(?:prepare|prep)\b', '\U0001F4CB'),                  # clipboard
    (r'\b(?:check|verify|audit)\b', '\u2705'),                # check mark
    (r'\b(?:update|upgrade)\b', '\U0001F504'),                # arrows
    (r'\b(?:create|build|make|implement|add)\b', '\U0001F528'),  # hammer
    (r'\b(?:plan|planning|schedule)\b', '\U0001F4C5'),        # calendar
    (r'\b(?:move|migration|migrate)\b', '\U0001F69A'),        # truck
    (r'\b(?:notification|notifications|alert)\b', '\U0001F514'),  # bell
    (r'\b(?:feature|features)\b', '\u2728'),                  # sparkles
    (r'\b(?:cleanup|refactor)\b', '\U0001F9F9'),              # broom
]
_EMOJI_PATTERNS = [(re.compile(p, re.IGNORECASE), e) for p, e in _EMOJI_KEYWORDS]


def _auto_emoji(title):
    """Generate auto-detected emojis based on task title keywords."""
    emojis = []
    seen = set()
    for pattern, emoji in _EMOJI_PATTERNS:
        if emoji not in seen and pattern.search(title):
            emojis.append(emoji)
            seen.add(emoji)
            if len(emojis) >= 1:
                break
    return "".join(emojis)


def _get_task_property_icons(task):
    """Return emoji string for task activity properties."""
    icons = []
    starred = task.get("isStarred", 0)
    if starred == 1:
        icons.append("\u2B50")       # star — Important
    elif starred == 2:
        icons.append("\U0001F31F")   # glowing star
    elif starred == 3:
        icons.append("\u2757")       # red exclamation

    frogged = task.get("isFrogged", 0)
    if frogged == 1:
        icons.append("\U0001F438")   # frog
    elif frogged == 2:
        icons.append("\U0001F425")   # baby chick (baby frog)
    elif frogged == 3:
        icons.append("\U0001F409")   # dragon (monster frog)

    urgent = task.get("isUrgent", 0)
    if urgent == 1:
        icons.append("\u26A0\uFE0F") # warning — Urgent
    elif urgent >= 2:
        icons.append("\U0001F525")   # fire — Very urgent

    if task.get("isPhysical"):
        icons.append("\U0001F4AA")   # flexed biceps — Physical

    if task.get("isPinned"):
        icons.append("\U0001F4CC")   # pushpin — Pinned

    return "".join(icons)


def _get_task_property_tooltip(task):
    """Return tooltip text describing task properties."""
    tips = []
    starred = task.get("isStarred", 0)
    if starred == 1: tips.append("Important")
    elif starred == 2: tips.append("Very important")
    elif starred == 3: tips.append("Critical")

    frogged = task.get("isFrogged", 0)
    if frogged == 1: tips.append("Weighing on me")
    elif frogged == 2: tips.append("Small frog")
    elif frogged == 3: tips.append("Crushing weight")

    urgent = task.get("isUrgent", 0)
    if urgent == 1: tips.append("Urgent")
    elif urgent >= 2: tips.append("Very urgent")

    if task.get("isPhysical"): tips.append("Physical")
    if task.get("isPinned"): tips.append("Pinned")

    return ", ".join(tips)


def _format_duration(ms):
    """Format milliseconds into human-readable duration."""
    total_min = int(ms / 60000)
    if total_min < 60:
        return f"{total_min}m"
    hours = total_min // 60
    mins = total_min % 60
    return f"{hours}h{mins}m" if mins else f"{hours}h"


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
        self._hidden_tasks = get_hidden_tasks()
        self._show_hidden = False
        self._tracking_timer_label = None
        self._time_widget = None
        self._apply_css()
        self._build_ui()

        # Tick every second to update tracking timer
        GLib.timeout_add(1000, self._tick_tracking_timer)

        self.connect("delete-event", self._on_delete)

    def _apply_css(self):
        css = Gtk.CssProvider()
        css.load_from_data(b"""
            window {
                background-color: #1a1b1e;
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", Roboto, Helvetica, Arial, sans-serif;
            }
            .header-bar {
                background-color: #5b9dff;
                padding: 18px 16px;
            }
            .header-title {
                color: #ffffff;
                font-size: 15px;
                font-weight: bold;
            }
            .header-btn {
                color: rgba(255,255,255,0.8);
                padding: 0 4px;
                min-width: 26px;
                min-height: 26px;
                background: none;
                border: none;
                font-size: 15px;
            }
            .header-btn:hover {
                color: #ffffff;
            }
            .task-card {
                background-color: #25262b;
                border-radius: 6px;
                padding: 12px 14px 12px 12px;
                border: none;
            }
            .task-card:hover {
                background-color: #2c2d33;
            }
            .task-tracked {
                background-color: #1e3a20;
                border-left: 3px solid #5bdb66;
            }
            .task-tracked:hover {
                background-color: #254228;
            }
            .task-title {
                color: #d4d4d4;
                font-size: 14px;
            }
            .task-check {
                color: transparent;
                font-size: 14px;
                padding: 0;
                min-width: 18px;
                min-height: 18px;
                border: 2px solid rgba(255,255,255,0.25);
                border-radius: 50%;
                background: transparent;
            }
            .task-check:hover {
                border-color: #5bdb66;
            }
            menu, menuitem {
                background-color: #25262b;
                color: #d4d4d4;
                font-size: 13px;
            }
            menuitem:hover {
                background-color: #2c2d33;
            }
            separator {
                background-color: #333;
                min-height: 1px;
            }
            .done-check {
                color: #5bdb66;
                font-size: 14px;
                padding: 0;
                min-width: 18px;
                min-height: 18px;
                border: none;
                background: transparent;
            }
            .done-check:hover {
                color: #808185;
            }
            .done-title {
                color: #808185;
                font-size: 13px;
            }
            .completed-header {
                color: #ffffff;
                font-size: 14px;
                font-weight: bold;
                padding: 8px 0;
            }
            .completed-toggle {
                color: #ffffff;
                font-size: 14px;
                font-weight: bold;
                padding: 8px 0;
            }
            .action-icon {
                background: none;
                border: none;
                color: rgba(255,255,255,0.4);
                padding: 0;
                min-width: 20px;
                min-height: 20px;
                font-size: 12px;
            }
            .action-icon:hover {
                color: #ffffff;
            }
            .prop-icons {
                font-size: 11px;
            }
            .tracking-timer {
                color: #5bdb66;
                font-size: 12px;
                font-weight: bold;
            }
            dialog, dialog .dialog-vbox, dialog box, dialog label, dialog entry {
                background-color: #25262b;
                color: #d4d4d4;
            }
            dialog entry {
                background-color: #1a1b1e;
                color: #d4d4d4;
                border: 1px solid #444;
                border-radius: 4px;
                padding: 6px;
            }
            dialog button, dialog .preset-btn {
                background: #333;
                background-image: none;
                color: #d4d4d4;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px 12px;
                box-shadow: none;
                text-shadow: none;
                -gtk-icon-shadow: none;
                font-size: 13px;
            }
            dialog button:hover, dialog .preset-btn:hover {
                background: #5b9dff;
                background-image: none;
                color: #ffffff;
                border-color: #5b9dff;
            }
            .subtask-entry {
                background: transparent;
                color: #d4d4d4;
                font-size: 13px;
                border: none;
                border-bottom: 1px solid #444;
                padding: 4px 2px;
                caret-color: #5b9dff;
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
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label="Today")
        title.get_style_context().add_class("header-title")
        title.set_halign(Gtk.Align.START)
        title.set_margin_start(8)
        header_box.pack_start(title, True, True, 0)
        self.header_title = title
        # Refresh button / spinner
        self.refresh_btn = Gtk.Button(label="\u21bb")
        self.refresh_btn.get_style_context().add_class("header-btn")
        self.refresh_btn.set_relief(Gtk.ReliefStyle.NONE)
        self.refresh_btn.set_tooltip_text("Refresh")
        header_box.pack_end(self.refresh_btn, False, False, 0)
        self.refresh_btn.connect("clicked", lambda b: self._on_refresh_click())
        self.refresh_spinner = Gtk.Spinner()
        self.refresh_spinner.set_no_show_all(True)
        header_box.pack_end(self.refresh_spinner, False, False, 0)
        # Hide window button
        hide_btn = Gtk.Button(label="\u2013")
        hide_btn.get_style_context().add_class("header-btn")
        hide_btn.set_relief(Gtk.ReliefStyle.NONE)
        hide_btn.set_tooltip_text("Hide widget")
        hide_btn.connect("clicked", lambda b: self.hide())
        header_box.pack_end(hide_btn, False, False, 0)
        # Restore default size button
        restore_btn = Gtk.Button(label="\u25A1")
        restore_btn.get_style_context().add_class("header-btn")
        restore_btn.set_relief(Gtk.ReliefStyle.NONE)
        restore_btn.set_tooltip_text("Restore default size")
        restore_btn.connect("clicked", lambda b: self.resize(WIDGET_WIDTH, 400))
        header_box.pack_end(restore_btn, False, False, 0)
        # Toggle hidden tasks visibility
        self._hidden_toggle_btn = Gtk.Button(label="\U0001F441\u0338")
        self._hidden_toggle_btn.get_style_context().add_class("header-btn")
        self._hidden_toggle_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._hidden_toggle_btn.set_tooltip_text("Show hidden tasks")
        self._hidden_toggle_btn.set_no_show_all(True)
        self._hidden_toggle_btn.connect("clicked", lambda b: self._toggle_show_hidden())
        header_box.pack_end(self._hidden_toggle_btn, False, False, 0)
        header.add(header_box)
        main_box.pack_start(header, False, False, 0)

        # Scrollable content area
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_margin_top(8)
        content.set_margin_bottom(8)

        # Active task list
        self.task_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        content.pack_start(self.task_list, False, False, 0)

        # Completed section toggle
        self.completed_expanded = False
        self._completed_box = Gtk.EventBox()
        self._completed_box.set_margin_top(16)
        self._completed_box.set_no_show_all(True)
        self._completed_box.connect("button-press-event", lambda w, e: self._toggle_completed(w))
        self._completed_label = Gtk.Label()
        self._completed_label.get_style_context().add_class("completed-toggle")
        self._completed_label.set_halign(Gtk.Align.START)
        self._completed_box.add(self._completed_label)
        content.pack_start(self._completed_box, False, False, 0)

        # Completed task list (hidden by default)
        self.done_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.done_list.set_margin_top(8)
        self.done_list.set_no_show_all(True)
        content.pack_start(self.done_list, False, False, 0)

        scrolled.add(content)
        main_box.pack_start(scrolled, True, True, 0)

        self.add(main_box)

        # Edge resize — detect mouse near edges and change cursor / start resize
        self.connect("motion-notify-event", self._on_edge_motion)
        self.connect("button-press-event", self._on_edge_press)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK)
        self._resize_edge = None

    _EDGE_SIZE = 6  # px from edge to trigger resize

    def _detect_edge(self, x, y):
        alloc = self.get_allocation()
        w, h = alloc.width, alloc.height
        e = self._EDGE_SIZE
        top = y < e
        bottom = y > h - e
        left = x < e
        right = x > w - e
        if top and left:
            return Gdk.WindowEdge.NORTH_WEST
        if top and right:
            return Gdk.WindowEdge.NORTH_EAST
        if bottom and left:
            return Gdk.WindowEdge.SOUTH_WEST
        if bottom and right:
            return Gdk.WindowEdge.SOUTH_EAST
        if top:
            return Gdk.WindowEdge.NORTH
        if bottom:
            return Gdk.WindowEdge.SOUTH
        if left:
            return Gdk.WindowEdge.WEST
        if right:
            return Gdk.WindowEdge.EAST
        return None

    _EDGE_CURSORS = {
        Gdk.WindowEdge.NORTH: "n-resize",
        Gdk.WindowEdge.SOUTH: "s-resize",
        Gdk.WindowEdge.WEST: "w-resize",
        Gdk.WindowEdge.EAST: "e-resize",
        Gdk.WindowEdge.NORTH_WEST: "nw-resize",
        Gdk.WindowEdge.NORTH_EAST: "ne-resize",
        Gdk.WindowEdge.SOUTH_WEST: "sw-resize",
        Gdk.WindowEdge.SOUTH_EAST: "se-resize",
    }

    def _on_edge_motion(self, widget, event):
        edge = self._detect_edge(event.x, event.y)
        self._resize_edge = edge
        win = self.get_window()
        if win:
            if edge and edge in self._EDGE_CURSORS:
                cursor = Gdk.Cursor.new_from_name(self.get_display(), self._EDGE_CURSORS[edge])
            else:
                cursor = None
            win.set_cursor(cursor)

    def _on_edge_press(self, widget, event):
        if event.button == 1 and self._resize_edge is not None:
            self.begin_resize_drag(
                self._resize_edge, event.button,
                int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _on_header_press(self, widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    # ── Task list rendering ──────────────────────────────────────────────

    def _on_refresh_click(self):
        self.refresh_btn.hide()
        self.refresh_spinner.show()
        self.refresh_spinner.start()
        self.store.refresh_tasks_now()

    def _tick_tracking_timer(self):
        """Update the live tracking timer every second."""
        start_ms = getattr(self, '_tracking_start_ms', 0)
        if start_ms and self.store.tracked_task_id and self._tracking_timer_label:
            now_ms = int(datetime.datetime.now().timestamp() * 1000)
            elapsed = now_ms - start_ms
            mins = int(elapsed / 60000)
            secs = int((elapsed % 60000) / 1000)
            self._tracking_timer_label.set_text(f"\u25CF {mins:02d}:{secs:02d}")
        return True  # keep ticking

    def _on_data_updated(self):
        # Stop refresh spinner
        self.refresh_spinner.stop()
        self.refresh_spinner.hide()
        self.refresh_btn.show()

        if not self.store.tasks_loaded:
            return
        self._initial_load = False
        self._render_tasks()

    def _render_tasks(self):
        # Active tasks
        self._tracking_timer_label = None
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

        all_incomplete = [t for t in self.store.tasks if not t.get("done")]
        all_incomplete.sort(key=lambda t: t.get("dayRank", t.get("rank", 0)))

        hidden_count = sum(1 for t in all_incomplete if t.get("_id") in self._hidden_tasks)
        visible_count = len(all_incomplete) - hidden_count

        header = f"Today \u00B7 {visible_count} tasks"
        if hidden_count:
            header += f" (+{hidden_count} hidden)"
        self.header_title.set_text(header)

        # Update header toggle button visibility
        if hidden_count:
            self._hidden_toggle_btn.show()
            lbl = "\U0001F441" if self._show_hidden else "\U0001F441\u0338"
            self._hidden_toggle_btn.set_label(lbl)
            self._hidden_toggle_btn.set_tooltip_text(
                "Hide hidden tasks" if self._show_hidden else "Show hidden tasks")
        else:
            self._hidden_toggle_btn.hide()

        # Filter tasks for display
        if self._show_hidden:
            display_tasks = all_incomplete
        else:
            display_tasks = [t for t in all_incomplete if t.get("_id") not in self._hidden_tasks]

        if not display_tasks:
            empty = Gtk.Label(label="All done for today!")
            empty.set_margin_top(20)
            empty.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
            self.task_list.pack_start(empty, False, False, 0)
        else:
            for i, task in enumerate(display_tasks):
                is_hidden = task.get("_id") in self._hidden_tasks
                row = self._make_task_row(task, i, is_hidden)
                self.task_list.pack_start(row, False, False, 0)

        self.task_list.show_all()

        # Completed section
        done = self.store.done_tasks
        if done:
            self._completed_label.set_text(f"Completed Today ({len(done)})")
            self._completed_box.set_no_show_all(False)
            self._completed_box.show_all()
        else:
            self._completed_box.set_no_show_all(True)
            self._completed_box.hide()
            self.done_list.hide()

        self._render_done_tasks()

    def _make_task_row(self, task, index, is_hidden=False):
        frame = Gtk.EventBox()
        frame.get_style_context().add_class("task-card")
        is_tracked = task.get("_id") == self.store.tracked_task_id
        if is_tracked:
            frame.get_style_context().add_class("task-tracked")
        if is_hidden:
            frame.set_opacity(0.4)
        frame.task_id = task.get("_id")
        frame.task_index = index

        # Drag source
        frame.drag_source_set(
            Gdk.ModifierType.BUTTON1_MASK, [], Gdk.DragAction.MOVE
        )
        frame.drag_source_add_text_targets()
        frame.connect("drag-data-get", self._on_drag_data_get)
        frame.connect("drag-begin", self._on_drag_begin)

        # Drop target — use DROP only so drag-motion doesn't eat scroll events
        frame.drag_dest_set(Gtk.DestDefaults.DROP, [], Gdk.DragAction.MOVE)
        frame.drag_dest_add_text_targets()
        frame.connect("drag-data-received", self._on_drag_data_received)
        frame.connect("drag-motion", lambda w, ctx, x, y, t: Gdk.drag_status(ctx, Gdk.DragAction.MOVE, t) or True)

        # Right-click context menu
        frame.connect("button-press-event", lambda w, e, t=task: self._on_task_right_click(w, e, t))

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        task_id = task.get("_id")

        # Attribute picker button on left — shown on hover
        attr_btn = Gtk.Button(label="\u2630")  # trigram / hamburger
        attr_btn.get_style_context().add_class("action-icon")
        attr_btn.set_relief(Gtk.ReliefStyle.NONE)
        attr_btn.set_tooltip_text("Set attributes")
        attr_btn.set_no_show_all(True)
        attr_btn.connect("clicked", lambda b, tid=task_id, t=task: self._show_attribute_picker(b, tid, t))
        hbox.pack_start(attr_btn, False, False, 0)

        # Clickable checkbox — thin circle outline
        check_btn = Gtk.Button(label=" ")
        check_btn.get_style_context().add_class("task-check")
        check_btn.set_relief(Gtk.ReliefStyle.NONE)
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.connect("clicked", lambda b, tid=task_id: self._on_task_check(tid))
        hbox.pack_start(check_btn, False, False, 0)

        # Title with property icons and auto-emoji inline after text
        clean = _clean_title(task.get("title", ""))
        props = _get_task_property_icons(task)
        emoji = _auto_emoji(clean)
        suffix = f" {props}{emoji}".rstrip()
        title_label = Gtk.Label(label=f"{clean}{suffix}" if suffix.strip() else clean)
        title_label.get_style_context().add_class("task-title")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_line_wrap(True)
        title_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_xalign(0)
        if props:
            title_label.set_tooltip_text(_get_task_property_tooltip(task))
        hbox.pack_start(title_label, True, True, 0)

        # Live tracking timer (always visible on tracked task)
        if is_tracked:
            timer_label = Gtk.Label()
            timer_label.get_style_context().add_class("tracking-timer")
            timer_label.set_valign(Gtk.Align.CENTER)
            hbox.pack_start(timer_label, False, False, 4)
            self._tracking_timer_label = timer_label

        # Hover action icons (hidden by default, shown on hover)
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions_box.set_no_show_all(True)
        actions_box.set_valign(Gtk.Align.CENTER)

        # Track play/stop with Alt+click (custom start) and Shift+click (edit time)
        dur_ms = task.get("duration", 0) or 0
        dur_tip = f"\nSo far {_format_duration(dur_ms)}" if dur_ms > 0 else ""
        if is_tracked:
            track_btn = Gtk.Button(label="\u23F9")  # stop
            track_btn.set_tooltip_text(f"Stop tracking{dur_tip}")
            track_btn.get_style_context().add_class("action-icon")
            track_btn.set_relief(Gtk.ReliefStyle.NONE)
            track_btn.connect("clicked", lambda b, tid=task_id: self._stop_tracking(tid))
        else:
            track_btn = Gtk.Button(label="\u25B6")  # play
            track_btn.set_tooltip_text(f"Track time\nAlt click to custom start{dur_tip}")
            track_btn.get_style_context().add_class("action-icon")
            track_btn.set_relief(Gtk.ReliefStyle.NONE)
            track_btn.connect("button-press-event",
                lambda b, e, tid=task_id, t=task: self._on_track_btn_press(e, tid, t))
        actions_box.pack_start(track_btn, False, False, 0)

        # Add time entry
        addtime_btn = Gtk.Button(label="\u2295")  # circled plus
        addtime_btn.set_tooltip_text("Add time entry")
        addtime_btn.get_style_context().add_class("action-icon")
        addtime_btn.set_relief(Gtk.ReliefStyle.NONE)
        addtime_btn.connect("clicked", lambda b, tid=task_id, t=task: self._show_add_time_dialog(tid, t))
        actions_box.pack_start(addtime_btn, False, False, 0)

        # Schedule tomorrow
        sched_btn = Gtk.Button(label="\U0001F4C5")  # calendar
        sched_btn.set_tooltip_text("Schedule tomorrow")
        sched_btn.get_style_context().add_class("action-icon")
        sched_btn.set_relief(Gtk.ReliefStyle.NONE)
        tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        sched_btn.connect("clicked", lambda b, tid=task_id, d=tomorrow: self._action_update(tid, {"day": d}))
        actions_box.pack_start(sched_btn, False, False, 0)

        # Add subtask
        sub_btn = Gtk.Button(label="\u2795")  # plus
        sub_btn.set_tooltip_text("Add subtask")
        sub_btn.get_style_context().add_class("action-icon")
        sub_btn.set_relief(Gtk.ReliefStyle.NONE)
        sub_btn.connect("clicked", lambda b, tid=task_id, f=frame: self._show_inline_subtask_entry(tid, f))
        actions_box.pack_start(sub_btn, False, False, 0)

        # Set duration / time estimate
        dur_btn = Gtk.Button(label="\u23F1")  # stopwatch
        dur_btn.set_tooltip_text("Set duration")
        dur_btn.get_style_context().add_class("action-icon")
        dur_btn.set_relief(Gtk.ReliefStyle.NONE)
        dur_btn.connect("clicked", lambda b, tid=task_id, t=task: self._show_set_duration_dialog(tid, t))
        actions_box.pack_start(dur_btn, False, False, 0)

        # Delete
        del_btn = Gtk.Button(label="\U0001F5D1")  # wastebasket
        del_btn.set_tooltip_text("Delete")
        del_btn.get_style_context().add_class("action-icon")
        del_btn.set_relief(Gtk.ReliefStyle.NONE)
        del_btn.connect("clicked", lambda b, tid=task_id: self._action_delete(tid))
        actions_box.pack_start(del_btn, False, False, 0)

        # More (opens context menu)
        more_btn = Gtk.Button(label="\u22EE")  # vertical ellipsis
        more_btn.set_tooltip_text("More actions")
        more_btn.get_style_context().add_class("action-icon")
        more_btn.set_relief(Gtk.ReliefStyle.NONE)
        more_btn.connect("clicked", lambda b, t=task: self._show_context_menu_at(b, t))
        actions_box.pack_start(more_btn, False, False, 0)

        hbox.pack_end(actions_box, False, False, 0)

        # Show/hide actions on hover (ignore child-widget crossings)
        def _on_enter(w, e, ab=actions_box, abtn=attr_btn):
            if e.detail == Gdk.NotifyType.INFERIOR:
                return
            ab.set_no_show_all(False)
            ab.show_all()
            abtn.set_no_show_all(False)
            abtn.show()

        def _on_leave(w, e, ab=actions_box, abtn=attr_btn):
            if e.detail == Gdk.NotifyType.INFERIOR:
                return
            ab.hide()
            abtn.hide()

        frame.connect("enter-notify-event", _on_enter)
        frame.connect("leave-notify-event", _on_leave)

        vbox.pack_start(hbox, False, False, 0)

        # Badges row: category + duration
        badges_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badges_box.set_margin_start(26)
        badges_box.set_margin_top(2)
        has_badges = False

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
            badges_box.pack_start(badge, False, False, 0)
            has_badges = True

        # Duration badge — green pill showing tracked time
        duration_ms = task.get("duration", 0) or 0
        if duration_ms > 0:
            dur_label = Gtk.Label()
            dur_text = _format_duration(duration_ms)
            dur_label.set_markup(f'<span font_size="x-small" weight="bold">\u23F1 {dur_text}</span>')
            dur_css = Gtk.CssProvider()
            dur_css.load_from_data(
                b".dur-badge { background-color: #2d6a30; color: #ffffff; "
                b"border-radius: 3px; padding: 2px 8px; }")
            dur_label.get_style_context().add_provider(dur_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            dur_label.get_style_context().add_class("dur-badge")
            dur_label.set_halign(Gtk.Align.START)
            badges_box.pack_start(dur_label, False, False, 0)
            has_badges = True

        if has_badges:
            vbox.pack_start(badges_box, False, False, 0)

        # Inline subtasks (dict of id -> {_id, title, done, rank})
        subtasks_dict = task.get("subtasks") or {}
        children = sorted(
            [s for s in subtasks_dict.values() if not s.get("done")],
            key=lambda s: s.get("rank", 0)
        )
        if children:
            for child in children:
                sub_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                sub_hbox.set_margin_start(26)
                sub_hbox.set_margin_top(4)

                sub_check = Gtk.Button(label=" ")
                sub_check.get_style_context().add_class("task-check")
                sub_check.set_relief(Gtk.ReliefStyle.NONE)
                sub_check.set_valign(Gtk.Align.CENTER)
                child_id = child.get("_id")
                task_id = task.get("_id")
                sub_check.connect("clicked", lambda b, tid=task_id, cid=child_id: self._on_subtask_check(tid, cid))
                sub_hbox.pack_start(sub_check, False, False, 0)

                sub_title = Gtk.Label(label=_clean_title(child.get("title", "")))
                sub_title.get_style_context().add_class("task-title")
                sub_title.set_halign(Gtk.Align.START)
                sub_title.set_xalign(0)
                sub_hbox.pack_start(sub_title, True, True, 0)

                vbox.pack_start(sub_hbox, False, False, 0)

        # Inline subtask entry placeholder — shown when user clicks "add subtask"
        self._subtask_entry_box = None  # will be created on demand per task

        frame.add(vbox)
        frame._vbox = vbox  # reference for inline subtask entry
        frame._task = task
        return frame

    def _show_inline_subtask_entry(self, task_id, frame):
        """Show an inline text entry for adding a subtask, like Marvin does."""
        vbox = frame._vbox

        # Remove any existing entry row
        for child in vbox.get_children():
            if getattr(child, "_is_subtask_entry", False):
                vbox.remove(child)

        entry_hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        entry_hbox.set_margin_start(26)
        entry_hbox.set_margin_top(4)
        entry_hbox._is_subtask_entry = True

        # Empty circle to match subtask style
        circle = Gtk.Label(label="\u25CB")
        circle.override_color(Gtk.StateFlags.NORMAL, _parse_color("#555555"))
        circle.set_valign(Gtk.Align.CENTER)
        entry_hbox.pack_start(circle, False, False, 0)

        entry = Gtk.Entry()
        entry.set_placeholder_text("Add subtask...")
        entry.get_style_context().add_class("subtask-entry")
        entry.set_has_frame(False)

        def on_activate(e):
            text = e.get_text().strip()
            if text:
                self._action_add_subtask(task_id, text)
                e.set_text("")
                # Re-focus for adding more
                GLib.idle_add(e.grab_focus)

        def on_escape(e, event):
            if event.keyval == Gdk.KEY_Escape:
                vbox.remove(entry_hbox)
                vbox.show_all()
                return True
            return False

        entry.connect("activate", on_activate)
        entry.connect("key-press-event", on_escape)
        entry_hbox.pack_start(entry, True, True, 0)

        vbox.pack_start(entry_hbox, False, False, 0)
        vbox.show_all()
        entry.grab_focus()

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

    def _toggle_show_hidden(self):
        self._show_hidden = not self._show_hidden
        self._render_tasks()

    def _hide_task(self, task_id):
        self._hidden_tasks.add(task_id)
        set_hidden_tasks(self._hidden_tasks)
        self._render_tasks()

    def _unhide_task(self, task_id):
        self._hidden_tasks.discard(task_id)
        set_hidden_tasks(self._hidden_tasks)
        self._render_tasks()

    def _toggle_completed(self, btn):
        self.completed_expanded = not self.completed_expanded
        done = self.store.done_tasks
        lbl = "Hide completed items" if self.completed_expanded else f"Completed Today ({len(done)})"
        self._completed_label.set_text(lbl)
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
        task_id = task.get("_id")

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        # Green checkmark — click to uncomplete
        check_btn = Gtk.Button(label="\u2714")
        check_btn.get_style_context().add_class("done-check")
        check_btn.set_relief(Gtk.ReliefStyle.NONE)
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.connect("clicked", lambda b, tid=task_id: self._on_task_uncheck(tid))
        hbox.pack_start(check_btn, False, False, 0)

        # Title with property icons and auto-emoji inline
        clean = _clean_title(task.get("title", ""))
        props = _get_task_property_icons(task)
        emoji = _auto_emoji(clean)
        suffix = f" {props}{emoji}".rstrip()
        title_label = Gtk.Label(label=f"{clean}{suffix}" if suffix.strip() else clean)
        title_label.get_style_context().add_class("done-title")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_line_wrap(True)
        title_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title_label.set_xalign(0)
        hbox.pack_start(title_label, True, True, 0)

        # Hover action icons
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        actions_box.set_no_show_all(True)
        actions_box.set_valign(Gtk.Align.CENTER)

        # Undo (mark undone)
        undo_btn = Gtk.Button(label="\u21A9")
        undo_btn.set_tooltip_text("Mark undone")
        undo_btn.get_style_context().add_class("action-icon")
        undo_btn.set_relief(Gtk.ReliefStyle.NONE)
        undo_btn.connect("clicked", lambda b, tid=task_id: self._on_task_uncheck(tid))
        actions_box.pack_start(undo_btn, False, False, 0)

        # Delete
        del_btn = Gtk.Button(label="\U0001F5D1")
        del_btn.set_tooltip_text("Delete")
        del_btn.get_style_context().add_class("action-icon")
        del_btn.set_relief(Gtk.ReliefStyle.NONE)
        del_btn.connect("clicked", lambda b, tid=task_id: self._action_delete(tid))
        actions_box.pack_start(del_btn, False, False, 0)

        hbox.pack_end(actions_box, False, False, 0)

        # Show/hide actions on hover
        def _on_enter(w, e, ab=actions_box):
            if e.detail == Gdk.NotifyType.INFERIOR:
                return
            ab.set_no_show_all(False)
            ab.show_all()

        def _on_leave(w, e, ab=actions_box):
            if e.detail == Gdk.NotifyType.INFERIOR:
                return
            ab.hide()

        frame.connect("enter-notify-event", _on_enter)
        frame.connect("leave-notify-event", _on_leave)

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
            badge.set_margin_start(26)
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
        target_id = getattr(widget, "task_id", "")
        # Reset opacity on all rows
        for child in self.task_list.get_children():
            child.set_opacity(1.0)
        if not source_id or source_id == target_id:
            return
        # Drop on bottom half = place after target, top half = place before
        alloc = widget.get_allocation()
        after = y > alloc.height // 2
        self.store.reorder_tasks(source_id, target_id, after=after)

    # ── Right-click context menu ───────────────────────────────────────

    def _on_task_right_click(self, widget, event, task):
        if event.button != 3:  # Right click only
            return False
        menu = Gtk.Menu()
        task_id = task.get("_id")
        title = task.get("title", "")

        # Mark done
        item = Gtk.MenuItem(label="\u2714  Mark done")
        item.connect("activate", lambda w: self._on_task_check(task_id))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Add subtask (inline if frame available, dialog fallback)
        item = Gtk.MenuItem(label="\u2795  Add subtask")
        if hasattr(widget, "_vbox"):
            item.connect("activate", lambda w: self._show_inline_subtask_entry(task_id, widget))
        else:
            item.connect("activate", lambda w: self._show_input_dialog(
                "Add subtask", "", lambda text: self._action_add_subtask(task_id, text)))
        menu.append(item)

        # Edit note
        item = Gtk.MenuItem(label="\U0001F4DD  Edit note")
        item.connect("activate", lambda w: self._show_input_dialog(
            "Edit note", task.get("note", "") or "", lambda text: self._action_update(task_id, {"note": text})))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Move to (category submenu)
        move_item = Gtk.MenuItem(label="#  Move to")
        move_menu = Gtk.Menu()
        for cat in self.store.categories:
            cat_item = Gtk.MenuItem(label=cat["title"])
            cat_id = cat["id"]
            cat_item.connect("activate", lambda w, cid=cat_id: self._action_update(task_id, {"parentId": cid}))
            move_menu.append(cat_item)
        move_item.set_submenu(move_menu)
        menu.append(move_item)

        # Schedule (date submenu)
        sched_item = Gtk.MenuItem(label="\U0001F4C5  Schedule")
        sched_menu = Gtk.Menu()
        today = datetime.date.today()
        for label, delta in [("Today", 0), ("Tomorrow", 1), ("In 2 days", 2),
                             ("Friday", (4 - today.weekday()) % 7 or 7),
                             ("Next week", 7), ("Unschedule", None)]:
            si = Gtk.MenuItem(label=label)
            if delta is not None:
                d = (today + datetime.timedelta(days=delta)).isoformat()
                si.connect("activate", lambda w, day=d: self._action_update(task_id, {"day": day}))
            else:
                si.connect("activate", lambda w: self._action_update(task_id, {"day": "unassigned"}))
            sched_menu.append(si)
        sched_item.set_submenu(sched_menu)
        menu.append(sched_item)

        # Set deadline
        item = Gtk.MenuItem(label="\u23F0  Set deadline")
        deadline_menu = Gtk.Menu()
        for label, delta in [("Tomorrow", 1), ("In 3 days", 3), ("In a week", 7),
                             ("In 2 weeks", 14), ("In a month", 30), ("Remove", None)]:
            di = Gtk.MenuItem(label=label)
            if delta is not None:
                d = (today + datetime.timedelta(days=delta)).isoformat()
                di.connect("activate", lambda w, dd=d: self._action_update(task_id, {"dueDate": dd}))
            else:
                di.connect("activate", lambda w: self._action_update(task_id, {"dueDate": None}))
            deadline_menu.append(di)
        item.set_submenu(deadline_menu)
        menu.append(item)

        # Set duration
        item = Gtk.MenuItem(label="\u23F1  Set duration")
        dur_menu = Gtk.Menu()
        for label, ms in [("5 min", 300000), ("10 min", 600000), ("15 min", 900000),
                          ("30 min", 1800000), ("45 min", 2700000), ("1 hour", 3600000),
                          ("1.5 hours", 5400000), ("2 hours", 7200000), ("3 hours", 10800000),
                          ("Remove", 0)]:
            di = Gtk.MenuItem(label=label)
            di.connect("activate", lambda w, m=ms: self._action_update(task_id, {"timeEstimate": m}))
            dur_menu.append(di)
        item.set_submenu(dur_menu)
        menu.append(item)

        # Edit time entries
        item = Gtk.MenuItem(label="\u23F1  Edit time entries")
        item.connect("activate", lambda w: self._show_time_entries_dialog(task_id, task))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Push to top
        item = Gtk.MenuItem(label="\u2B06  Push to top")
        item.connect("activate", lambda w: self.store.reorder_tasks(task_id, None, after=False))
        menu.append(item)

        # Push to bottom
        item = Gtk.MenuItem(label="\u2B07  Push to bottom")
        item.connect("activate", lambda w: self.store.reorder_tasks(task_id, None, after=True))
        menu.append(item)

        menu.append(Gtk.SeparatorMenuItem())

        # Hide/unhide from widget
        is_hidden = task_id in self._hidden_tasks
        if is_hidden:
            item = Gtk.MenuItem(label="\U0001F441  Show on widget")
            item.connect("activate", lambda w: self._unhide_task(task_id))
        else:
            item = Gtk.MenuItem(label="\U0001F6AB  Hide from widget")
            item.connect("activate", lambda w: self._hide_task(task_id))
        menu.append(item)

        # Delete
        item = Gtk.MenuItem(label="\U0001F5D1  Delete")
        item.connect("activate", lambda w: self._action_delete(task_id))
        menu.append(item)

        menu.show_all()
        menu.popup(None, None, None, None, event.button, event.time)
        return True

    def _show_attribute_picker(self, button, task_id, task):
        """Show a popup menu to set/unset task activity properties."""
        menu = Gtk.Menu()

        # Each attribute: (label, field, values when toggled on, current check)
        attrs = [
            ("\u2B50 Important",       "isStarred",  1, task.get("isStarred", 0) >= 1),
            ("\U0001F525 Urgent",       "isUrgent",   1, task.get("isUrgent", 0) >= 1),
            ("\U0001F525\U0001F525 Very Urgent", "isUrgent", 2, task.get("isUrgent", 0) >= 2),
            ("\U0001F438 Frog",         "isFrogged",  1, task.get("isFrogged", 0) >= 1),
            ("\U0001F409 Monster Frog", "isFrogged",  3, task.get("isFrogged", 0) >= 3),
            ("\U0001F4AA Physical",     "isPhysical", True, bool(task.get("isPhysical"))),
            ("\U0001F4CC Pinned",       "isPinned",   True, bool(task.get("isPinned"))),
        ]

        for label, field, on_val, is_active in attrs:
            item = Gtk.CheckMenuItem(label=label)
            item.set_active(is_active)
            # Toggle: if currently matches on_val, turn off; otherwise set to on_val
            if field in ("isPhysical", "isPinned"):
                off_val = False
            else:
                off_val = 0
            item.connect("toggled", lambda w, f=field, on=on_val, off=off_val:
                self._action_update(task_id, {f: on if w.get_active() else off}))
            menu.append(item)

        menu.show_all()
        menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())

    def _show_context_menu_at(self, button, task):
        """Open context menu positioned at a button (for the more '...' icon)."""
        # Create a synthetic event-like object for the menu builder
        class FakeEvent:
            button = 3
            time = Gtk.get_current_event_time()
        self._on_task_right_click(button, FakeEvent(), task)

    def _show_input_dialog(self, title, default_text, callback):
        dialog = Gtk.Dialog(title=title, transient_for=self, modal=True)
        dialog.set_decorated(False)
        dialog.set_default_size(300, -1)

        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        entry = Gtk.Entry()
        entry.set_text(default_text)
        entry.connect("activate", lambda e: dialog.response(Gtk.ResponseType.OK))
        content.pack_start(entry, False, False, 0)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("OK", Gtk.ResponseType.OK)
        dialog.show_all()

        response = dialog.run()
        text = entry.get_text().strip()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and text:
            callback(text)

    def _action_add_subtask(self, parent_id, title):
        import uuid
        subtask_id = uuid.uuid4().hex[:13]

        # Optimistic: add to local task's subtasks dict
        for t in self.store.tasks:
            if t.get("_id") == parent_id:
                subs = t.get("subtasks") or {}
                existing_ranks = [s.get("rank", 0) for s in subs.values()]
                new_rank = max(existing_ranks, default=0) + 1
                subs[subtask_id] = {
                    "_id": subtask_id,
                    "title": title,
                    "done": False,
                    "rank": new_rank,
                }
                t["subtasks"] = subs
                break
        self._render_tasks()

        def do_add():
            try:
                self.store.api.update_task(parent_id, {
                    f"subtasks.{subtask_id}": {
                        "_id": subtask_id,
                        "title": title,
                        "done": False,
                        "rank": new_rank,
                    }
                })
            except Exception as e:
                print(f"Failed to add subtask: {e}", file=sys.stderr)
                GLib.idle_add(self.store.refresh_tasks_now)

        threading.Thread(target=do_add, daemon=True).start()

    def _action_update(self, task_id, setters):
        # Optimistic: update local task data immediately
        removes_from_today = "day" in setters and setters["day"] != datetime.date.today().isoformat()
        for t in self.store.tasks + self.store.done_tasks:
            if t.get("_id") == task_id:
                for k, v in setters.items():
                    t[k] = v
                break
        if removes_from_today:
            self.store.tasks = [t for t in self.store.tasks if t.get("_id") != task_id]
        self._render_tasks()

        def do_update():
            try:
                self.store.api.update_task(task_id, setters)
            except Exception as e:
                print(f"Failed to update task: {e}", file=sys.stderr)
                GLib.idle_add(self.store.refresh_tasks_now)

        threading.Thread(target=do_update, daemon=True).start()

    def _action_delete(self, task_id):
        # Optimistic remove
        self.store.tasks = [t for t in self.store.tasks if t.get("_id") != task_id]
        self._render_tasks()

        def do_delete():
            try:
                self.store.api.delete_task(task_id)
            except Exception as e:
                print(f"Failed to delete task: {e}", file=sys.stderr)
                GLib.idle_add(self.store.refresh_tasks_now)

        threading.Thread(target=do_delete, daemon=True).start()

    def _on_subtask_check(self, task_id, subtask_id):
        """Mark an inline subtask as done."""
        # Optimistic update
        for t in self.store.tasks:
            if t.get("_id") == task_id:
                subs = t.get("subtasks", {})
                if subtask_id in subs:
                    subs[subtask_id]["done"] = True
                break
        self._render_tasks()

        def do_update():
            try:
                self.store.api.update_task(task_id, {f"subtasks.{subtask_id}.done": True})
            except Exception as e:
                print(f"Failed to mark subtask done: {e}", file=sys.stderr)

        threading.Thread(target=do_update, daemon=True).start()

    def _on_track_btn_press(self, event, task_id, task):
        """Handle click/alt-click on the play button."""
        if event.button != 1:
            return False
        if event.state & Gdk.ModifierType.MOD1_MASK:
            self._show_custom_start_dialog(task_id, task)
        else:
            self._start_tracking(task_id)
        return True

    def _show_set_duration_dialog(self, task_id, task):
        """Show dialog with preset duration buttons + custom input, like Marvin."""
        dialog = Gtk.Dialog(title="Set duration", transient_for=self, modal=True)
        dialog.set_decorated(False)
        dialog.set_default_size(280, -1)

        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title_label = Gtk.Label()
        title_label.set_markup("<b>Set duration</b>")
        title_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#ffffff"))
        content.pack_start(title_label, False, False, 4)

        task_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        task_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
        task_label.set_line_wrap(True)
        content.pack_start(task_label, False, False, 4)

        chosen_ms = [0]

        # Preset buttons in grid
        presets = [
            ("5m", 300000), ("10m", 600000), ("15m", 900000), ("20m", 1200000),
            ("30m", 1800000), ("45m", 2700000), ("1h", 3600000), ("1h 30m", 5400000),
            ("2h", 7200000),
        ]
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)
        grid.set_margin_top(8)
        for idx, (label, ms) in enumerate(presets):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("preset-btn")
            def on_preset(b, m=ms):
                chosen_ms[0] = m
                dialog.response(Gtk.ResponseType.OK)
            btn.connect("clicked", on_preset)
            grid.attach(btn, idx % 4, idx // 4, 1, 1)
        content.pack_start(grid, False, False, 0)

        # Custom input
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_top(8)
        entry = Gtk.Entry()
        entry.set_width_chars(8)
        entry.set_placeholder_text("custom")
        hbox.pack_start(entry, True, True, 0)
        suffix = Gtk.Label(label="m")
        suffix.override_color(Gtk.StateFlags.NORMAL, _parse_color("#d4d4d4"))
        hbox.pack_start(suffix, False, False, 0)
        content.pack_start(hbox, False, False, 0)

        def on_entry_activate(e):
            text = e.get_text().strip()
            if text.isdigit():
                chosen_ms[0] = int(text) * 60000
                dialog.response(Gtk.ResponseType.OK)
        entry.connect("activate", on_entry_activate)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and chosen_ms[0] > 0:
            self._action_update(task_id, {"timeEstimate": chosen_ms[0]})

    def _show_add_time_dialog(self, task_id, task):
        """Show dialog to add a time entry (duration in minutes)."""
        dialog = Gtk.Dialog(title="Add time", transient_for=self, modal=True)
        dialog.set_decorated(False)
        dialog.set_default_size(280, -1)

        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title_label = Gtk.Label()
        title_label.set_markup("<b>Add time entry</b>")
        title_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#ffffff"))
        content.pack_start(title_label, False, False, 4)

        task_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        task_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
        task_label.set_line_wrap(True)
        content.pack_start(task_label, False, False, 4)

        chosen_ms = [0]

        # Preset buttons
        presets = [
            ("5m", 300000), ("10m", 600000), ("15m", 900000), ("20m", 1200000),
            ("30m", 1800000), ("45m", 2700000), ("1h", 3600000), ("1h 30m", 5400000),
        ]
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)
        grid.set_margin_top(8)
        for idx, (label, ms) in enumerate(presets):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("preset-btn")
            def on_preset(b, m=ms):
                chosen_ms[0] = m
                dialog.response(Gtk.ResponseType.OK)
            btn.connect("clicked", on_preset)
            grid.attach(btn, idx % 4, idx // 4, 1, 1)
        content.pack_start(grid, False, False, 0)

        # Custom input
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_top(8)
        entry = Gtk.Entry()
        entry.set_width_chars(8)
        entry.set_placeholder_text("custom")
        hbox.pack_start(entry, True, True, 0)
        suffix = Gtk.Label(label="m")
        suffix.override_color(Gtk.StateFlags.NORMAL, _parse_color("#d4d4d4"))
        hbox.pack_start(suffix, False, False, 0)
        content.pack_start(hbox, False, False, 0)

        def on_entry_activate(e):
            text = e.get_text().strip()
            if text.isdigit():
                chosen_ms[0] = int(text) * 60000
                dialog.response(Gtk.ResponseType.OK)
        entry.connect("activate", on_entry_activate)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK and chosen_ms[0] > 0:
            self._add_time_entry(task_id, chosen_ms[0])

    def _add_time_entry(self, task_id, add_ms):
        """Add a time entry to a task — creates a start/stop pair ending now."""
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        start_ms = now_ms - add_ms

        # Update local task
        for t in self.store.tasks:
            if t.get("_id") == task_id:
                existing_times = t.get("times") or []
                new_times = existing_times + [start_ms, now_ms]
                existing_dur = t.get("duration", 0) or 0
                new_dur = existing_dur + add_ms
                t["times"] = new_times
                t["duration"] = new_dur
                break
        else:
            new_times = [start_ms, now_ms]
            new_dur = add_ms

        if self.store.on_update:
            self.store.on_update()

        def do_update():
            try:
                self.store.api.update_task(task_id, {
                    "times": new_times,
                    "duration": new_dur,
                    "workedOnAt": now_ms,
                    "firstTracked": new_times[0],
                    "fieldUpdates.times": now_ms,
                    "fieldUpdates.duration": now_ms,
                    "fieldUpdates.workedOnAt": now_ms,
                })
            except Exception as e:
                print(f"Failed to add time entry: {e}", file=sys.stderr)
        threading.Thread(target=do_update, daemon=True).start()

    def _show_time_entries_dialog(self, task_id, task):
        """Show Time Tracking dialog with editable entries, like Marvin's Shift+click."""
        dialog = Gtk.Dialog(title="Time Tracking", transient_for=self, modal=True)
        dialog.set_decorated(False)
        dialog.set_default_size(380, -1)

        content = dialog.get_content_area()
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        # Header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        title_label = Gtk.Label()
        title_label.set_markup("\u23F1 <b>Time Tracking</b>")
        title_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#ffffff"))
        header_box.pack_start(title_label, True, True, 0)
        close_btn = Gtk.Button(label="\u2715")
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.get_style_context().add_class("action-icon")
        close_btn.connect("clicked", lambda b: dialog.response(Gtk.ResponseType.CLOSE))
        header_box.pack_end(close_btn, False, False, 0)
        content.pack_start(header_box, False, False, 0)

        # Task name
        task_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        task_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
        task_label.set_line_wrap(True)
        task_label.set_margin_top(4)
        content.pack_start(task_label, False, False, 4)

        # Separator
        sep = Gtk.Separator()
        sep.set_margin_top(8)
        sep.set_margin_bottom(8)
        content.pack_start(sep, False, False, 0)

        # Time entries list
        times = list(task.get("times") or [])
        entries_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        def _ms_to_time_str(ms):
            dt = datetime.datetime.fromtimestamp(ms / 1000)
            return dt.strftime("%-I:%M %p").lower()

        def _ms_to_date_str(ms):
            dt = datetime.datetime.fromtimestamp(ms / 1000)
            return dt.strftime("%m/%d/%Y")

        def _rebuild_entries():
            for child in entries_box.get_children():
                entries_box.remove(child)
            entry_widgets.clear()

            if not times or len(times) < 2:
                empty = Gtk.Label(label="No time tracked yet")
                empty.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
                empty.set_margin_top(16)
                empty.set_margin_bottom(16)
                entries_box.pack_start(empty, False, False, 0)
            else:
                for i in range(0, len(times) - 1, 2):
                    start_ms = times[i]
                    stop_ms = times[i + 1]
                    dur_ms = stop_ms - start_ms

                    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
                    row.set_margin_top(4)

                    # Date
                    date_label = Gtk.Label(label=f"\U0001F4C5 {_ms_to_date_str(start_ms)}")
                    date_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#d4d4d4"))
                    row.pack_start(date_label, False, False, 0)

                    # Start time
                    start_entry = Gtk.Entry()
                    start_entry.set_text(_ms_to_time_str(start_ms))
                    start_entry.set_width_chars(8)
                    start_entry.get_style_context().add_class("subtask-entry")
                    row.pack_start(start_entry, False, False, 0)

                    arrow = Gtk.Label(label="\u2192")
                    arrow.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
                    row.pack_start(arrow, False, False, 0)

                    # Stop time
                    stop_entry = Gtk.Entry()
                    stop_entry.set_text(_ms_to_time_str(stop_ms))
                    stop_entry.set_width_chars(8)
                    stop_entry.get_style_context().add_class("subtask-entry")
                    row.pack_start(stop_entry, False, False, 0)

                    # Duration label
                    dur_label = Gtk.Label(label=_format_duration(dur_ms))
                    dur_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#5b9dff"))
                    row.pack_start(dur_label, False, False, 4)

                    # Delete entry button
                    pair_idx = i
                    del_btn = Gtk.Button(label="\U0001F5D1")
                    del_btn.set_relief(Gtk.ReliefStyle.NONE)
                    del_btn.get_style_context().add_class("action-icon")
                    del_btn.set_tooltip_text("Remove entry")
                    def on_delete(b, idx=pair_idx):
                        del times[idx:idx + 2]
                        _rebuild_entries()
                        entries_box.show_all()
                    del_btn.connect("clicked", on_delete)
                    row.pack_end(del_btn, False, False, 0)

                    entries_box.pack_start(row, False, False, 0)

            # Total
            total_ms = sum(times[i + 1] - times[i] for i in range(0, len(times) - 1, 2)) if len(times) >= 2 else 0
            total_label = Gtk.Label()
            total_label.set_markup(f'<span color="#5b9dff" font_size="large"><b>{_format_duration(total_ms)}</b></span>')
            total_label.set_margin_top(8)
            entries_box.pack_start(total_label, False, False, 0)

            # Add time entry button
            add_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            add_row.set_margin_top(4)
            add_btn = Gtk.Button(label="\u2795 Add time entry")
            add_btn.set_relief(Gtk.ReliefStyle.NONE)
            add_btn.get_style_context().add_class("preset-btn")
            def on_add(b):
                now_ms = int(datetime.datetime.now().timestamp() * 1000)
                times.extend([now_ms - 300000, now_ms])  # default 5m entry
                _rebuild_entries()
                entries_box.show_all()
            add_btn.connect("clicked", on_add)
            add_row.pack_start(add_btn, False, False, 0)
            entries_box.pack_start(add_row, False, False, 0)

        _rebuild_entries()
        content.pack_start(entries_box, False, False, 0)

        # Save button
        save_btn = Gtk.Button(label="Save")
        save_btn.get_style_context().add_class("preset-btn")
        save_btn.set_margin_top(12)
        save_btn.set_halign(Gtk.Align.CENTER)
        save_btn.connect("clicked", lambda b: dialog.response(Gtk.ResponseType.OK))
        content.pack_start(save_btn, False, False, 0)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK:
            # `times` list has been mutated in-place by add/delete operations
            new_times = list(times)
            total_ms = sum(new_times[i + 1] - new_times[i] for i in range(0, len(new_times) - 1, 2)) if len(new_times) >= 2 else 0
            now_ms = int(datetime.datetime.now().timestamp() * 1000)

            # Update local
            for t in self.store.tasks:
                if t.get("_id") == task_id:
                    t["times"] = new_times
                    t["duration"] = total_ms
                    break
            if self.store.on_update:
                self.store.on_update()

            # Save to API
            def do_save():
                try:
                    self.store.api.update_task(task_id, {
                        "times": new_times,
                        "duration": total_ms,
                        "workedOnAt": now_ms if total_ms > 0 else 0,
                        "firstTracked": new_times[0] if new_times else 0,
                        "fieldUpdates.times": now_ms,
                        "fieldUpdates.duration": now_ms,
                        "fieldUpdates.workedOnAt": now_ms,
                    })
                except Exception as e:
                    print(f"Failed to save time entries: {e}", file=sys.stderr)
            threading.Thread(target=do_save, daemon=True).start()

    def _show_custom_start_dialog(self, task_id, task):
        """Show dialog to start tracking from X minutes ago."""
        dialog = Gtk.Dialog(title="Start tracking...", transient_for=self, modal=True)
        dialog.set_decorated(False)
        dialog.set_default_size(280, -1)

        content = dialog.get_content_area()
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        title_label = Gtk.Label()
        title_label.set_markup("<b>Start tracking...</b>")
        title_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#ffffff"))
        content.pack_start(title_label, False, False, 4)

        task_label = Gtk.Label(label=_clean_title(task.get("title", "")))
        task_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
        task_label.set_line_wrap(True)
        content.pack_start(task_label, False, False, 4)

        chosen_mins = [0]

        # Preset buttons
        presets = [("Now", 0), ("5m ago", 5), ("10m ago", 10), ("15m ago", 15),
                   ("20m ago", 20), ("30m ago", 30), ("45m ago", 45), ("1h ago", 60)]
        grid = Gtk.Grid()
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)
        grid.set_margin_top(8)
        for idx, (label, mins) in enumerate(presets):
            btn = Gtk.Button(label=label)
            btn.get_style_context().add_class("preset-btn")
            def on_preset(b, m=mins):
                chosen_mins[0] = m
                dialog.response(Gtk.ResponseType.OK)
            btn.connect("clicked", on_preset)
            grid.attach(btn, idx % 4, idx // 4, 1, 1)
        content.pack_start(grid, False, False, 0)

        # Custom input
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hbox.set_margin_top(8)
        entry = Gtk.Entry()
        entry.set_width_chars(8)
        entry.set_placeholder_text("custom")
        hbox.pack_start(entry, True, True, 0)
        suffix_label = Gtk.Label(label="m ago")
        suffix_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#d4d4d4"))
        hbox.pack_start(suffix_label, False, False, 0)
        content.pack_start(hbox, False, False, 0)

        def on_entry_activate(e):
            text = e.get_text().strip()
            if text.isdigit():
                chosen_mins[0] = int(text)
                dialog.response(Gtk.ResponseType.OK)
        entry.connect("activate", on_entry_activate)

        dialog.show_all()
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.OK:
            self._start_tracking_custom(task_id, chosen_mins[0])

    def _start_tracking_custom(self, task_id, mins_ago):
        """Start tracking a task, backdated by mins_ago minutes."""
        backdate_ms = mins_ago * 60 * 1000
        self._tracking_start_ms = int(datetime.datetime.now().timestamp() * 1000) - backdate_ms

        prev_tracked = self.store.tracked_task_id
        if prev_tracked and prev_tracked != task_id:
            self._stop_tracking(prev_tracked)

        self.store.tracked_task_id = task_id
        self._render_tasks()

        # Notify Marvin's tracking server
        def do_start():
            try:
                self.store.api.start_tracking(task_id)
            except Exception:
                pass
        threading.Thread(target=do_start, daemon=True).start()

    def _start_tracking(self, task_id):
        prev_tracked = self.store.tracked_task_id
        if prev_tracked and prev_tracked != task_id:
            self._stop_tracking(prev_tracked)

        self.store.tracked_task_id = task_id
        self._tracking_start_ms = int(datetime.datetime.now().timestamp() * 1000)
        self._render_tasks()

        # Also notify Marvin's tracking server so the app shows it as tracked
        def do_start():
            try:
                self.store.api.start_tracking(task_id)
            except Exception:
                pass  # Non-critical — we track locally
        threading.Thread(target=do_start, daemon=True).start()

    def _stop_tracking(self, task_id):
        start_ms = getattr(self, '_tracking_start_ms', 0)
        now_ms = int(datetime.datetime.now().timestamp() * 1000)
        self.store.tracked_task_id = None
        self._tracking_start_ms = 0

        # Add the tracking session as a time entry if it's at least 1 second
        if start_ms and (now_ms - start_ms) >= 1000:
            self._add_time_entry(task_id, now_ms - start_ms)

        self._render_tasks()

        # Also notify Marvin's tracking server
        def do_stop():
            try:
                self.store.api.stop_tracking(task_id)
            except Exception:
                pass
        threading.Thread(target=do_stop, daemon=True).start()

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
            if self._time_widget:
                self._time_widget.hide()
        else:
            self.show_all()
            self._reposition()
            self.set_keep_above(True)
            self.stick()
            self.present()
            if self._time_widget:
                self._time_widget.show_all()
                self._time_widget.position_above(self)
                self._time_widget.refresh()

    def _on_delete(self, widget, event):
        # Hide instead of destroying — keeps the app running in tray
        self.hide()
        return True  # Prevent actual window destruction


# ── Category Time Widget ─────────────────────────────────────────────────────

class CategoryTimeWidget(Gtk.Window):
    """Floating widget showing tracked time per category."""

    def __init__(self, store):
        super().__init__(title="Marvin Time")
        self.set_wmclass("marvin-time", "marvin-time")
        self.store = store
        self._show_all_cats = False

        self.set_default_size(WIDGET_WIDTH, 200)
        self.set_decorated(False)
        self.set_resizable(True)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(False)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
        if os.path.exists(icon_path):
            self.set_icon_from_file(icon_path)
        self.stick()

        self._apply_css()
        self._build_ui()
        self.connect("delete-event", lambda w, e: (w.hide(), True)[-1])

    def _apply_css(self):
        # Reuse global CSS already applied
        pass

    def _build_ui(self):
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header
        header = Gtk.EventBox()
        header.get_style_context().add_class("header-bar")
        header.connect("button-press-event", self._on_header_press)
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        title = Gtk.Label(label="\u23F1 Time Tracked")
        title.get_style_context().add_class("header-title")
        title.set_halign(Gtk.Align.START)
        title.set_margin_start(8)
        header_box.pack_start(title, True, True, 0)

        # Toggle all/tracked
        self._toggle_btn = Gtk.Button(label="All")
        self._toggle_btn.get_style_context().add_class("header-btn")
        self._toggle_btn.set_relief(Gtk.ReliefStyle.NONE)
        self._toggle_btn.set_tooltip_text("Show all categories")
        self._toggle_btn.connect("clicked", self._on_toggle)
        header_box.pack_end(self._toggle_btn, False, False, 0)

        # Hide
        hide_btn = Gtk.Button(label="\u2013")
        hide_btn.get_style_context().add_class("header-btn")
        hide_btn.set_relief(Gtk.ReliefStyle.NONE)
        hide_btn.connect("clicked", lambda b: self.hide())
        header_box.pack_end(hide_btn, False, False, 0)

        header.add(header_box)
        main_box.pack_start(header, False, False, 0)

        # Scrollable content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        self._list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._list_box.set_margin_start(10)
        self._list_box.set_margin_end(10)
        self._list_box.set_margin_top(8)
        self._list_box.set_margin_bottom(8)
        scrolled.add(self._list_box)
        main_box.pack_start(scrolled, True, True, 0)

        # Total row at bottom
        self._total_label = Gtk.Label()
        self._total_label.get_style_context().add_class("header-title")
        self._total_label.set_margin_top(6)
        self._total_label.set_margin_bottom(8)
        self._total_label.set_margin_start(12)
        self._total_label.set_halign(Gtk.Align.START)
        main_box.pack_end(self._total_label, False, False, 0)

        self.add(main_box)

    def _on_header_press(self, widget, event):
        if event.button == 1:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)

    def _on_toggle(self, btn):
        self._show_all_cats = not self._show_all_cats
        self._toggle_btn.set_label("Tracked" if self._show_all_cats else "All")
        self._toggle_btn.set_tooltip_text(
            "Show only tracked" if self._show_all_cats else "Show all categories")
        self.refresh()

    def position_above(self, main_widget):
        """Position this widget directly above the main widget."""
        wx, wy = main_widget.get_position()
        self.move(wx, wy - self.get_size()[1] - 4)

    def refresh(self):
        """Rebuild the category time list from current store data."""
        for child in self._list_box.get_children():
            self._list_box.remove(child)

        # Show spinner while data hasn't loaded yet
        if not self.store.tasks_loaded:
            spinner = Gtk.Spinner()
            spinner.start()
            spinner.set_margin_top(20)
            spinner.set_margin_bottom(20)
            spinner.set_halign(Gtk.Align.CENTER)
            self._list_box.pack_start(spinner, False, False, 0)
            self._total_label.set_text("")
            self._list_box.show_all()
            return

        # Aggregate duration by category
        all_tasks = self.store.tasks + self.store.done_tasks
        cat_durations = {}  # cat_id -> total_ms
        for t in all_tasks:
            parent_id = t.get("parentId", "")
            dur = t.get("duration", 0) or 0
            if parent_id:
                cat_durations[parent_id] = cat_durations.get(parent_id, 0) + dur

        total_ms = sum(cat_durations.values())

        # Build display list
        if self._show_all_cats:
            cats_to_show = list(self.store.categories)
        else:
            cats_to_show = [c for c in self.store.categories if cat_durations.get(c["id"], 0) > 0]

        # Sort by duration descending
        cats_to_show.sort(key=lambda c: cat_durations.get(c["id"], 0), reverse=True)

        if not cats_to_show:
            empty = Gtk.Label(label="No time tracked today")
            empty.override_color(Gtk.StateFlags.NORMAL, _parse_color("#808185"))
            empty.set_margin_top(12)
            self._list_box.pack_start(empty, False, False, 0)
        else:
            for cat in cats_to_show:
                dur = cat_durations.get(cat["id"], 0)
                row = self._make_cat_row(cat, dur, total_ms)
                self._list_box.pack_start(row, False, False, 0)

        # Total
        self._total_label.set_text(f"Total: {_format_duration(total_ms)}" if total_ms else "")

        self._list_box.show_all()

    def _make_cat_row(self, cat, duration_ms, total_ms):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_top(4)
        row.set_margin_bottom(4)

        # Color bar
        color_bar = Gtk.DrawingArea()
        color_bar.set_size_request(4, -1)
        rgba = Gdk.RGBA()
        rgba.parse(cat.get("color", "#888"))
        color_bar.connect("draw", lambda w, cr, c=rgba: (
            cr.set_source_rgba(c.red, c.green, c.blue, c.alpha),
            cr.rectangle(0, 0, w.get_allocated_width(), w.get_allocated_height()),
            cr.fill()))
        row.pack_start(color_bar, False, False, 0)

        # Category name
        name_label = Gtk.Label(label=cat["title"])
        name_label.get_style_context().add_class("task-title")
        name_label.set_halign(Gtk.Align.START)
        row.pack_start(name_label, True, True, 0)

        # Duration
        if duration_ms > 0:
            dur_label = Gtk.Label(label=_format_duration(duration_ms))
            dur_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#5bdb66"))
            row.pack_end(dur_label, False, False, 0)

            # Percentage bar
            if total_ms > 0:
                pct = duration_ms / total_ms
                bar = Gtk.DrawingArea()
                bar.set_size_request(50, 6)
                bar.set_valign(Gtk.Align.CENTER)
                bar_rgba = Gdk.RGBA()
                bar_rgba.parse(cat.get("color", "#888"))
                bar.connect("draw", lambda w, cr, p=pct, c=bar_rgba: (
                    cr.set_source_rgba(0.3, 0.3, 0.3, 1),
                    cr.rectangle(0, 0, w.get_allocated_width(), w.get_allocated_height()),
                    cr.fill(),
                    cr.set_source_rgba(c.red, c.green, c.blue, c.alpha),
                    cr.rectangle(0, 0, w.get_allocated_width() * p, w.get_allocated_height()),
                    cr.fill()))
                row.pack_end(bar, False, False, 0)
        else:
            dur_label = Gtk.Label(label="0m")
            dur_label.override_color(Gtk.StateFlags.NORMAL, _parse_color("#555555"))
            row.pack_end(dur_label, False, False, 0)

        return row

    def toggle_visibility(self):
        if self.get_visible():
            self.hide()
        else:
            self.show_all()
            self.refresh()
            self.set_keep_above(True)
            self.stick()
            self.present()


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

    # Category time widget — default open, positioned above main widget
    time_widget = CategoryTimeWidget(store)
    widget._time_widget = time_widget

    # Chain data updates to refresh time widget
    _orig_on_update = store.on_update
    def _on_data_updated():
        if _orig_on_update:
            _orig_on_update()
        time_widget.refresh()
    store.on_update = _on_data_updated

    def _show_time_widget():
        time_widget.show_all()
        time_widget.position_above(widget)
        time_widget.refresh()

    # Show time widget after main widget is positioned
    GLib.idle_add(_show_time_widget)

    # Ctrl+Alt+W keybinding to toggle time widget
    def _on_key_press(accel_group, window, keyval, modifier):
        time_widget.toggle_visibility()
        if time_widget.get_visible():
            time_widget.position_above(widget)
    accel_group = Gtk.AccelGroup()
    accel_group.connect(Gdk.keyval_from_name("w"),
                        Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK,
                        Gtk.AccelFlags.VISIBLE, _on_key_press)
    widget.add_accel_group(accel_group)

    create_indicator(widget)
    store.start_polling()

    def on_sigusr1(signum, frame):
        GLib.idle_add(widget.toggle_visibility)

    QUICK_ADD_DIR = os.path.expanduser("~/.cache/marvin-quick-add")

    def on_sigusr2(signum, frame):
        def handle():
            # Read all pending task files
            import glob
            for fpath in glob.glob(os.path.join(QUICK_ADD_DIR, "task_*.json")):
                try:
                    with open(fpath) as f:
                        task_data = json.load(f)
                    os.remove(fpath)
                    store.tasks.append(task_data)
                except (json.JSONDecodeError, OSError):
                    pass
            if store.on_update:
                store.on_update()
            # Background refresh to get real data from API
            store.refresh_tasks_now()
        GLib.idle_add(handle)

    def on_sigrtmin(signum, frame):
        def handle():
            time_widget.toggle_visibility()
            if time_widget.get_visible():
                time_widget.position_above(widget)
        GLib.idle_add(handle)

    signal.signal(signal.SIGUSR1, on_sigusr1)
    signal.signal(signal.SIGUSR2, on_sigusr2)
    signal.signal(signal.SIGRTMIN, on_sigrtmin)

    Gtk.main()


if __name__ == "__main__":
    main()
