# Marvin Desktop Quick Actions

A lightweight desktop widget and quick-add input for [Amazing Marvin](https://amazingmarvin.com) on Linux (GNOME/Wayland).

Solves the problem of Amazing Marvin's global shortcuts not working on Wayland — Electron's `globalShortcut` module cannot register system-wide hotkeys due to Wayland's security model.

## Features

### Task Widget (`Ctrl+Alt+S`)
- Floating task list showing today's tasks
- Styled to match Amazing Marvin's dark theme
- Click checkboxes to mark tasks done
- Collapsible "Completed Today" section with undo support
- Drag and drop to reorder tasks (synced back to Marvin via dayRank)
- Hover action icons (like Marvin desktop app):
  - **Play/Stop** — start/stop time tracking (Alt+click for custom start)
  - **Add time** — add time entry with presets (5m, 10m, 15m, etc.)
  - **Schedule** — reschedule to tomorrow
  - **Add subtask** — inline entry (like Marvin, no dialog popup)
  - **Set duration** — time estimate with presets
  - **Delete** — remove task
  - **More** — full context menu
- **Double-click to edit** task titles inline
- Right-click context menu with:
  - Mark done/undone, add subtask, edit note
  - Move to category, schedule, set deadline, set duration
  - Edit time entries (view/add/delete tracking sessions)
  - Push to top/bottom, hide from widget
  - Set activity properties, delete
- Right-click context menu on completed tasks (mark undone, reschedule, etc.)
- Attribute picker on hover (left side) for setting task properties:
  - Important, Urgent, Frog, Physical, Pinned
- Activity property icons displayed inline with title
- Auto-generated emoji based on task title keywords
- Duration badge showing tracked time per task
- Live tracking timer (green counter) on actively tracked task
- Hide/show tasks (persisted to config) with toggle in header
- Resizable from all edges
- Always on top, visible on all workspaces
- Auto-refreshes tasks every 60 seconds

### Category Time Widget (`Ctrl+Alt+W`)
- Floating widget showing tracked time per category
- Color-coded bars with percentage visualization
- Toggle between tracked-only and all categories
- Total time display at the bottom
- Opens by default above the main widget
- Hides/shows together with main widget via `Ctrl+Alt+S`

### Quick-Add Input (`Ctrl+Alt+A`)
- Lightweight floating input bar for adding tasks
- **Default "today" scheduling** — tasks are scheduled for today by default (visible as a tag), removable by picking a different date
- **Screenshot OCR** — capture a screen region and auto-extract task title, notes, and category using AI (Ctrl+Shift+S)
- **Notes field** — toggle with Ctrl+N, appears above the input bar
- **Metadata tag badges** — visual tags for category, date, labels etc. with unique replacement (selecting a new category replaces the old one)
- Autocomplete for all Marvin shortcut syntax:
  - `#Project` — assign to category/project
  - `@label` — add a label
  - `+tomorrow` — schedule for a day
  - `~30m` — time estimate
  - `!morning` — daily section
  - `*p1` / `*p2` / `*p3` — priority
  - `due next week` — due date
  - `starts` / `ends` / `review` — date fields
  - `&Next Week` — plan for week/month
- Auto-refreshes the task widget after adding

## Time Tracking

The widget integrates with Amazing Marvin's time tracking:
- **Start/stop** tracking from hover icons — syncs with the Marvin app
- **Add time entries** — writes proper CouchDB entries with fieldUpdates
- **Edit time entries** — view, add, and delete sessions via right-click menu
- **Custom start** — Alt+click the play button to backdate tracking (e.g., "started 10m ago")
- **Live timer** — shows elapsed time on the actively tracked task
- **Category breakdown** — time widget aggregates tracked time by category

## Requirements

- Ubuntu (tested on 25.10) with GNOME desktop
- Python 3 with GTK3 bindings (system Python)
- Amazing Marvin account with API access

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/logical-luke/marvin-desktop-quick-actions.git
   cd marvin-desktop-quick-actions
   ```

2. Run the installer:
   ```bash
   ./install.sh
   ```

   This will:
   - Prompt for your Amazing Marvin API token
   - Register `Ctrl+Alt+A` and `Ctrl+Alt+S` keyboard shortcuts
   - Create a desktop entry and autostart entry

3. Set up all keyboard shortcuts (including the time widget):
   ```bash
   ./setup-shortcuts.sh
   ```

4. Get your API tokens from [Amazing Marvin API settings](https://app.amazingmarvin.com/pre?api):
   - **API Token** — for reading tasks, categories, labels, and marking done
   - **Full Access Token** — for reordering, time tracking, and task updates

5. Add tokens to `~/.config/marvin-widget/config.json`:
   ```json
   {
       "api_token": "YOUR_API_TOKEN",
       "full_access_token": "YOUR_FULL_ACCESS_TOKEN",
       "anthropic_api_key": "YOUR_ANTHROPIC_API_KEY"
   }
   ```
   The Anthropic API key is optional — only needed for Screenshot OCR feature.

## Usage

- **`Ctrl+Alt+S`** — Toggle the task widget (and time widget)
- **`Ctrl+Alt+W`** — Toggle the category time widget independently
- **`Ctrl+Alt+A`** — Open quick-add input (press again to dismiss)

## How It Works

- Uses GNOME's custom keyboard shortcuts (which work as the Wayland compositor) to trigger shell scripts
- The task widget runs as a persistent GTK3 app under XWayland (for reliable window positioning)
- Communicates with the [Amazing Marvin API](https://github.com/amazingmarvin/MarvinAPI/wiki) for all data
- Time tracking writes directly to CouchDB via `doc/update` with proper `fieldUpdates` merge (dot-notation) for sync compatibility

## License

MIT
