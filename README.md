# Marvin Desktop Quick Actions

A lightweight desktop widget and quick-add input for [Amazing Marvin](https://amazingmarvin.com) on Linux (GNOME/Wayland).

Solves the problem of Amazing Marvin's global shortcuts not working on Wayland — Electron's `globalShortcut` module cannot register system-wide hotkeys due to Wayland's security model.

## Features

### Task Widget (`Ctrl+Alt+S`)
- Floating task list showing today's tasks
- Styled to match Amazing Marvin's dark theme
- Click checkboxes to mark tasks done
- Collapsible "Completed Today" section with undo support
- Drag and drop to reorder tasks (synced back to Marvin)
- Always on top, visible on all workspaces
- Auto-refreshes tasks every 60 seconds

### Sidebar widget

<img width="340" height="400" alt="image" src="https://github.com/user-attachments/assets/e0a1d56f-4066-4cbb-b24f-c61ed172c284" />

### Context menu in widget

<img width="419" height="273" alt="image" src="https://github.com/user-attachments/assets/fb035e0a-84d4-494f-bc70-5bdb35c8ac7a" />


### Quick-Add Input (`Ctrl+Alt+A`)
- Lightweight floating input bar for adding tasks
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
- Closes on submit, Escape, or focus loss
- Auto-refreshes the task widget after adding

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

3. Get your API tokens from [Amazing Marvin API settings](https://app.amazingmarvin.com/pre?api):
   - **API Token** — for reading tasks, categories, labels, and marking done
   - **Full Access Token** — for reordering tasks and uncompleting

4. Add both tokens to `~/.config/marvin-widget/config.json`:
   ```json
   {
       "api_token": "YOUR_API_TOKEN",
       "full_access_token": "YOUR_FULL_ACCESS_TOKEN"
   }
   ```

## Usage

- **`Ctrl+Alt+S`** — Toggle the task widget
- **`Ctrl+Alt+A`** — Open quick-add input (press again to dismiss)

## How It Works

- Uses GNOME's custom keyboard shortcuts (which work as the Wayland compositor) to trigger shell scripts
- The task widget runs as a persistent GTK3 app under XWayland (for reliable window positioning)
- Communicates with the [Amazing Marvin API](https://github.com/amazingmarvin/MarvinAPI/wiki) for all data

## License

MIT
