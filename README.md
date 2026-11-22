# Canvas -> Telegram LMS Alert Bot

A Python bot that monitors your **Canvas LMS** account(s) and sends clean, **grouped Telegram digest alerts** (matching the MS Teams Telegram Bot) for:

- 📝 **Assignments** — new assignments, updates (title, due date, points, unlock/lock dates, description)
- ⏰ **Multi-Tier Reminders** — 24h, 3h, and 1h before assignment deadlines + 30m and 15m before calendar events
- 📢 **Announcements** — new and edited course announcements with author & preview
- 📚 **Module Materials** — newly added or renamed lectures, files, pages, quizzes, and external links
- 🎓 **Grades & Activity Feed** — new grades/submissions, inbox messages, discussions, peer reviews, conferences
- ⚠️ **System Alerts** — Canvas token expiry warnings (≤ 3 days) and 401 auth errors

---

## Quick Start

### 1. Install Dependencies & Configure

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item accounts.example.json accounts.json
```

Fill in `.env` with your Telegram bot token & chat ID (from `@BotFather`), and add your Canvas URL + Personal Access Token to `accounts.json`.

### 2. Verify Telegram Setup

```powershell
python bot.py --get-chat-id
python bot.py --test
```

---

## Running the Bot

### Option 1: GitHub Actions (Every 15 Minutes — Zero PC Needed)

Push to GitHub and configure repository **Secrets**:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `CANVAS_ACCOUNTS` (JSON array matching `accounts.example.json`)

Optional repository **Variables** (all have sensible defaults):
- `TIMEZONE` (default `Asia/Phnom_Penh`)
- `ASSIGNMENT_REMINDER_MINUTES` (default `1440,180,60`)
- `EVENT_REMINDER_MINUTES` (default `30,15`)
- `QUIET_HOURS` (e.g. `23:00-07:00`)
- `DIGEST_MODE` (default `true`)

### Option 2: Daemon Mode (Instant Interactive Commands & Buttons)

```powershell
python bot.py
```

Runs a live Telegram command/button listener in the background so tapping buttons or slash commands replies immediately, while polling Canvas every `POLL_SECONDS`.

### Option 3: Windows Task Scheduler (Background & Survives Reboots)

```powershell
python bot.py --once
.\scripts\install-task.ps1
```

To remove the scheduled task later:

```powershell
.\scripts\uninstall-task.ps1
```

---

## Telegram Commands & Buttons

- **🔄 Check Now** (`/check`) — trigger an immediate Canvas check (bypasses quiet hours)
- **📋 Upcoming** (`/upcoming [days]`) — upcoming assignments, quizzes & calendar events with direct Canvas links
- **📚 Courses** (`/courses`) — active Canvas courses with direct links
- **📊 Status** (`/status`) — accounts, last poll/alert times, quiet hours, reminder tiers & tracked counts
- **❓ Help** (`/help`) — display persistent bottom keyboard & commands
- `/chatid` — print your Telegram Chat ID

---

## Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `POLL_SECONDS` | `300` | Poll interval in daemon mode |
| `TIMEZONE` | `Asia/Phnom_Penh` | Local timezone for dates & digest timestamps |
| `DIGEST_MODE` | `true` | Group alerts into one clean digest message per poll |
| `ALERT_ON_FIRST_RUN` | `false` | Record existing items silently on first run (no spam) |
| `ASSIGNMENT_REMINDER_MINUTES` | `1440,180,60` | Remind 24h, 3h, and 1h before assignment deadlines |
| `EVENT_REMINDER_MINUTES` | `30,15` | Remind 30m and 15m before Canvas calendar events |
| `UPCOMING_LOOKAHEAD_DAYS` | `7` | Days ahead cached for `/upcoming` |
| `QUIET_HOURS` | *(empty)* | Optional e.g. `23:00-07:00` to defer alerts until morning |
| `TELEGRAM_ENABLE_COMMANDS` | `true` | Enable interactive commands & inline/reply buttons |

---

## Running Tests

```powershell
python -m unittest discover -s tests -v
```
