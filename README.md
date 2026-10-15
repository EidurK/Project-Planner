# Project Manager Agent

A web app where you chat with an AI project manager (powered by Claude). It keeps a project list, plans projects by
splitting them into subtasks, and can research and ask you questions along the way. It runs on your own computer.

- **Chat**: talk to the agent; it reads and edits your projects.
- **Overview**: who is working on what, plus notes about your organization and its scope.
- **Agent prompt**: the agent's own instructions (and its subagents'). The agent can improve them itself.
- **Projects list** and **Gantt chart**: your projects, subtasks and dependencies.
- **Settings**: light/dark mode, which Claude model to use, and your API key.

## What you need

1. **Python 3.10 or newer**. Check with `python --version` (on Linux/macOS you may need `python3`).
   Get it from <https://www.python.org/downloads/> if you don't have it.
2. **An Anthropic API key** (the agent uses Claude, and usage is billed to your Anthropic account).
   Create one at <https://console.anthropic.com/> under *API keys*.
3. An internet connection.

## Run it

From the project folder:

```bash
python main.py
```

(Use `python3 main.py` if `python` isn't found.)

The first run creates a private virtual environment in `.venv/` and installs the dependencies, which takes a minute.
After that it starts instantly. Your browser opens at <http://127.0.0.1:8000>. Press `Ctrl+C` in the terminal to stop.

Then:

1. Open the **Settings** tab and paste your **API key**. It is checked when you save it.
2. Optionally pick a **model** (cheaper models cost less; bigger ones plan better).
3. Go to **Chat** and say what you're working on, for example *"Add a project: build a new iron plant"*.

Options:

| Option | What it does |
| --- | --- |
| `--port 9000` | Use another port (it also picks a free one automatically if 8000 is busy). |
| `--no-browser` | Don't open the browser automatically. |
| `--export [FILE]`, `--import FILE` | Save / load your setup as a zip (see below). `--yes` skips the import question and allows overwriting an existing export file. |
| `--host 0.0.0.0` | Make the app reachable from other computers. **Careful:** there is no login, so anyone who can reach it can use your API key and see your data. Only do this on a network you trust. |

## Where things are stored

Everything you create lives in the `data/` folder next to `main.py` (created on first run from the starter files in
`defaults/`):

| File | What it is |
| --- | --- |
| `data/projects.json` | Your projects, subtasks, assignees, hours and dependencies |
| `data/prompt.md` | The project manager's prompt |
| `data/agents/` | Prompts of the subagents (information gatherer, planner, reviewer, and any you create) |
| `data/organization.md`, `data/employees.json` | Organization/scope notes and the employee list |
| `data/settings.json` | Your chosen model |
| `data/secrets.json` | Your API key (plain text, readable only by your user account) |
| `data/history/` | Backups of prompts, of the project file before deletions, and of data replaced by an import |

To start fresh, stop the app and delete the `data/` folder. To back up or move your setup, copy `data/`.

## Sharing your setup with friends (export / import)

To send a friend your prompts, projects and organization notes, make an export:

```bash
python main.py --export                  # creates pm-agent-export-<date>.zip in the current folder
python main.py --export my-setup.zip     # or choose the file name
```

The zip contains the project manager's prompt, all subagent prompts (including custom subagents you created), the
project data (projects, subtasks, assignees, hours, dependencies), the organization notes and the employee list.
It does **not** contain your API key, your model/theme settings, or old history.

Your friend sets the app up (see *Run it*), then imports your zip:

```bash
python main.py --import my-setup.zip
```

- The zip is **validated first**: it must be a real export from this app, with the right files and well-formed data
  (valid statuses, no broken or circular dependencies, and so on). If anything is wrong, it tells you what and
  **changes nothing**.
- A valid zip **replaces** the friend's current prompts, projects, subagents and organization data. You are asked to
  confirm (add `--yes` to skip the question). What was replaced is copied to
  `data/history/import-backup-<time>/` first, so it can be recovered by hand.
- Their API key and settings are left alone.
- If the app is running during an import, the new data is picked up automatically; reload the browser page.

### Sharing the app itself

Send the project folder **without** `data/` (your projects and API key), `.venv/` and `.env`. If you use git,
`.gitignore` already excludes them. A friend who runs it for the first time starts with an empty project list and the
starter prompts from `defaults/`; then they can `--import` your export to get your setup.

## Optional: `.env`

Instead of pasting the key in Settings you can put it in a file called `.env` next to `main.py`
(copy `.env.example`):

```
ANTHROPIC_API_KEY=your-key-here
MODEL=claude-sonnet-5-5
```

A key saved in the Settings tab takes priority over the one in `.env`.

## Troubleshooting

- **"No valid API key. Add one in the Settings tab."** Add or fix the key in Settings.
- **`python: command not found`**: try `python3 main.py`, or install Python (on Windows, tick *Add Python to PATH*).
- **Installing the dependencies failed**: check your internet connection and run `python main.py` again.
  If the install was interrupted, delete the `.venv/` folder and retry.
- **Port already in use**: the launcher picks the next free port and prints the address; or use `--port`.
- **The agent keeps going and costs tokens**: press the red **Stop** button next to Send.

## For developers

```bash
python main.py --no-browser                                # run
.venv/bin/pip install -r requirements-dev.txt              # test dependencies (Windows: .venv\Scripts\pip)
.venv/bin/pytest                                           # run the tests
.venv/bin/uvicorn app.main:app --reload                    # run with auto-reload while editing
```

Layout: `app/` is the FastAPI backend (`agent.py` is the tool-use loop, `subagents.py` the subagents,
`storage.py`/`org.py`/`settings.py` the data files, `transfer.py` export/import), `static/` the web UI, `defaults/` the starter data, `tests/` the tests.
