# MongoIDE — Claude Code Guide

This file tells Claude Code how to work with this project.

## Project Overview

MongoIDE is a single-page MongoDB GUI. The entire frontend lives in **one file** (`templates/index.html` — ~3800 lines of HTML + CSS + vanilla JS). The backend is a single Flask file (`app.py`).

## Architecture

```
app.py                  Flask REST API (all routes)
templates/index.html    Single-file SPA — HTML + CSS + JS, no framework, no build step
connections.json        Auto-created at runtime; stores connection configs (gitignored)
.env                    ANTHROPIC_API_KEY (gitignored)
```

## Running the App

```bash
# Requires: Python 3.9+, MongoDB running locally (or remotely)
pip install -r requirements.txt
cp .env.example .env   # add your Anthropic API key
python app.py          # starts on http://localhost:8080
```

## Key Conventions

### Backend (`app.py`)
- All routes return `jsonify(...)` — either `{"success": True, ...}` or `{"error": "..."}` (with 4xx status)
- MongoDB connections are cached in `active_connections` dict (in-memory)
- Connection configs are persisted to `connections.json` via `save_connections()`
- `get_client(name)` raises `ValueError` if the connection name is unknown — callers wrap in try/except
- `serial(doc)` converts BSON to JSON-safe dicts via `bson.json_util`

### Frontend (`templates/index.html`)
- **State**: all runtime state lives in the `state` object (defined near the top of the `<script>` block)
- **CSS tokens**: all colors/sizes are CSS custom properties on `:root`; light theme overrides them under `body.light`
- **Panels**: each tool is a `<div id="panel-xxx" class="tool-panel">` shown/hidden by `switchTool(name)`
- **Tree**: sidebar renders with `renderTree()` — call this after any change to `state.connections`
- **API calls**: always use the `api(method, url, body)` helper — never raw `fetch`
- **Notifications**: use `notify(message, type)` — types: `'success'`, `'error'`, `'warning'`, `'info'`
- **Modals**: `openModal('modal-id')` / `closeModal('modal-id')`

### Design Tokens (CSS variables)
```
--bg0 → darkest background (page)
--bg1 → topbar / sidebar
--bg2 → panels, toolbars
--bg3 → inputs, cards
--bg4 → hover states
--border / --border2 → dividers
--txt0 → primary text
--txt1 → secondary text
--txt2 → muted / labels
--green → primary accent (#00ed64 dark / #00994d light)
--blue, --purple, --orange, --red, --cyan, --pink → semantic colors
```

### Adding a New Panel
1. Add a `<button class="tool-btn" id="tbtn-xxx" onclick="switchTool('xxx')">` in `#toolbar`
2. Add `<div id="panel-xxx" class="tool-panel">...</div>` inside `#content`
3. `switchTool` handles show/hide automatically — no JS needed for basic cases
4. If the panel needs init on first open, add a case in the `switchTool` function

### Adding a New API Route
1. Add a Flask route in `app.py`
2. Always wrap in `try/except Exception as e: return jsonify({"error": str(e)}), 400`
3. Call it from the frontend via `await api('METHOD', '/api/your-route', body)`

## Common Pitfalls

- `renderTree()` rebuilds `#conn-tree` from `state.connections` — always update `state.connections` before calling it
- `filterTree(v)` does in-place DOM filtering (show/hide nodes) — it does NOT call `renderTree()`
- The `#no-connections` placeholder is recreated on demand in `renderTree()` — do not rely on `getElementById` finding it
- `loadConnections()` fetches from server AND calls `renderTree()` — use it for a full refresh
- For immediate UI feedback after save, push to `state.connections` directly then call `renderTree()` — don't wait for a network round-trip
- Resize handles work by setting `style.width` + `style.flex = 'none'` on the left panel; `flex:1` panels auto-fill the rest
- Theme is toggled by adding/removing `body.light` class; all colors update via CSS cascade — no JS color logic needed
- Favicon is regenerated as an SVG data URL on every theme toggle via `setFavicon()`

## Environment Variables

| Variable | Required | Purpose |
|----------|----------|---------|
| `ANTHROPIC_API_KEY` | Only for AI Helper | Powers the Claude-based query assistant |
