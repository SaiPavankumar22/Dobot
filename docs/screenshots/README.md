# Screenshots

Real captures of the desktop app (Tauri shell renders in any browser, so these are headless-Chromium
shots of the running frontend against the live backend — nothing mocked). Taken 1 October 2026 with
the **Console** theme, 1440×900.

| File | Page | What it shows |
| --- | --- | --- |
| `chat.png` | Chat | The hero, suggestions, mode pills, composer with hotkey strip |
| `overview.png` | Overview | Numbered stat cards, provider health chips, live activity log |
| `tasks.png` | Tasks | Stats strip, filter toolbar, task records with run controls |
| `skills.png` | Skills | Skill cards grid with CLEAN pills, search and filters |
| `security.png` | Security | Sandbox honesty card, isolation notice, micro-label sections |
| `activity.png` | Activity | The full event timeline |
| `settings.png` | Settings | Key management and backend configuration |

## Recapturing

With the backend and Vite dev server running:

```bash
EDGE="/c/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
shot() { "$EDGE" --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1440,900 --virtual-time-budget=9000 \
  --user-data-dir="$TEMP/edge-shot-$$" \
  --screenshot="$(pwd -W)/$1.png" "http://127.0.0.1:1420/#$2" 2>/dev/null; }
shot overview overview   # hash is optional for chat: shot chat "http://127.0.0.1:1420/"
```
