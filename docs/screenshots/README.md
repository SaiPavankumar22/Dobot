# Screenshots

Real captures of the desktop app (Tauri shell renders in any browser, so these are headless-Chromium
shots of the running frontend against the live backend — nothing mocked). Taken 28 October 2026 with
the **Console** theme, 1440×900.

| File | Page | What it shows |
| --- | --- | --- |
| `chat.png` | Chat | History sidebar with the Workspace entry, a rendered markdown answer, mode pills, composer with mic and screen chips |
| `overview.png` | Overview | Level/XP card, numbered stat cards, provider health chips, live activity log |
| `tasks.png` | Tasks | Stats strip, filter toolbar, task records with run controls |
| `skills.png` | Skills | Skill cards grid with CLEAN pills, search and filters |
| `security.png` | Security | Sandbox honesty card, human-control toggles, micro-label sections |
| `activity.png` | Activity | The full event timeline, with the timeline / debug-trace switch |
| `settings.png` | Settings | Key management, storage controls, live backend capabilities, updates |

## Recapturing

With the backend and Vite dev server running:

```bash
cd docs/screenshots
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
shot() { name="$1"; hash="$2"; "$EDGE" --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1440,900 --virtual-time-budget=9000 \
  --user-data-dir="$TEMP/edge-shot-$$-$name" \
  --screenshot="$(pwd -W)/$name.png" "http://127.0.0.1:1420/$hash" 2>/dev/null; }

shot chat ""              # the chat page needs no hash
shot overview "#/overview"
shot tasks "#/tasks"
shot skills "#/skills"
shot security "#/security"
shot activity "#/activity"
shot settings "#/settings"
```

Each shot gets its own `--user-data-dir`, so every capture starts from a clean profile — the default
Console theme, no stale localStorage, exactly what a first-run user sees.
