#!/usr/bin/env python3
"""
Canvas (Instructure) -> Telegram LMS Alert Bot.

Sends clean, grouped Telegram digest alerts (matching the MS Teams Telegram Bot) for:
  - New & edited announcements
  - New & updated assignments (title, due date, points, unlock/lock dates, description)
  - New & renamed course module items (lectures, files, pages, quizzes, links)
  - Grades, submissions, inbox messages, discussions, conferences (Canvas activity feed)
  - Multi-tier due-soon reminders for assignments (24h, 3h, 1h) & calendar events (30m, 15m)
  - Token expiry & 401 auth warnings

Interactive Telegram UX:
  - Slash Menu commands: /check, /upcoming [days], /courses, /status, /help, /chatid
  - Persistent Reply Keyboard (🔄 Check Now, 📋 Upcoming, 📚 Courses, 📊 Status, ❓ Help)
  - Inline Quick-Action Buttons on digests, /status, /upcoming, and /courses
  - Quiet Hours (e.g. 23:00-07:00) that defer alerts without losing them until morning
  - Instant background command listener in daemon mode; clean drain in --once mode

Usage:
    python bot.py                # run forever (polls every POLL_SECONDS + instant button/command replies)
    python bot.py --once         # single poll + pending commands, then exit (GitHub Actions / Task Scheduler)
    python bot.py --test         # send a test Telegram message
    python bot.py --get-chat-id  # print chat IDs that have messaged your bot
    python bot.py --setup-menu   # register Telegram "/" menu commands right now
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - very old Python
    ZoneInfo = None

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
ACCOUNTS_FILE = BASE_DIR / "accounts.json"

TELEGRAM_MAX_LENGTH = 4000
RETENTION_DAYS = 90
MENU_VERSION = 2

ICONS = {
    "Announcement": "📢 Announcement",
    "DiscussionTopic": "💬 Discussion",
    "Conversation": "✉️ Inbox message",
    "Submission": "📝 Grade / submission",
    "Message": "🔔 Notification",
    "Conference": "🎥 Conference",
    "Collaboration": "🤝 Collaboration",
    "AssessmentRequest": "👥 Peer review",
}
MODULE_ICONS = {
    "File": "📄",
    "Page": "📃",
    "Discussion": "💬",
    "Quiz": "❓",
    "ExternalUrl": "🔗",
    "ExternalTool": "🧩",
}
FIELD_LABELS = {
    "name": "Title",
    "title": "Title",
    "due_at": "Due date",
    "points_possible": "Points",
    "unlock_at": "Available from",
    "lock_at": "Available until",
}
DATE_FIELDS = ("due_at", "unlock_at", "lock_at")

CATEGORY_ORDER = (
    "system",
    "reminder",
    "assignment",
    "announcement",
    "module",
    "grade",
    "activity",
)
CATEGORY_LABELS = {
    "system": "⚠️ System Alerts",
    "reminder": "⏰ Upcoming Reminders",
    "assignment": "📝 Assignments",
    "announcement": "📢 Announcements",
    "module": "📚 Module Materials",
    "grade": "🎓 Grades & Submissions",
    "activity": "💬 Discussions & Inbox",
}

BOT_COMMANDS = [
    {"command": "check", "description": "Check Canvas right now"},
    {"command": "upcoming", "description": "Upcoming deadlines & events (e.g. /upcoming 14)"},
    {"command": "courses", "description": "Your active Canvas courses"},
    {"command": "status", "description": "Bot status & last check time"},
    {"command": "help", "description": "Show menu & command buttons"},
    {"command": "chatid", "description": "Show this chat ID"},
]

MAIN_REPLY_KEYBOARD = {
    "keyboard": [
        [{"text": "🔄 Check Now"}, {"text": "📋 Upcoming"}],
        [{"text": "📚 Courses"}, {"text": "📊 Status"}],
        [{"text": "❓ Help"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

QUICK_INLINE_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "🔄 Check Now", "callback_data": "action:check"},
            {"text": "📋 Upcoming", "callback_data": "action:upcoming"},
        ],
        [
            {"text": "📚 Courses", "callback_data": "action:courses"},
            {"text": "📊 Status", "callback_data": "action:status"},
        ],
    ]
}

CFG: dict[str, Any] = {
    "tg_token": "",
    "chat_id": "",
    "poll": 300,
    "remind_hours": 24,
    "assignment_reminder_minutes": [1440, 180, 60],
    "event_reminder_minutes": [30, 15],
    "upcoming_lookahead_days": 7,
    "tz": timezone.utc,
    "tz_name": "UTC",
    "quiet_hours": None,
    "alert_on_first_run": False,
    "digest_mode": True,
    "enable_commands": True,
    "persist_last_poll": True,
}

esc = lambda s: html.escape(str(s), quote=True)


# ---------- data models ----------
@dataclass
class QuietHours:
    start_minutes: int
    end_minutes: int
    raw: str = ""


@dataclass
class CanvasNotification:
    category: str  # one of CATEGORY_ORDER
    title: str
    account: str = ""
    course: str = ""
    channel: str = ""  # module name or sub-context
    sender: str = ""
    badge: str = ""  # e.g. "New", "Updated", "Edited", "Renamed", icon label
    due_at: str | None = None
    starts_at: str | None = None
    grade_line: str = ""
    details: list[str] = field(default_factory=list)  # pre-escaped HTML bullet lines (e.g. diffs)
    summary: str = ""
    url: str | None = None
    is_urgent: bool = False


@dataclass
class StateChanges:
    seen_updates: dict[str, str] = field(default_factory=dict)
    fp_updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    silent_fp_inits: dict[str, dict[str, Any]] = field(default_factory=dict)
    refresh_seen: dict[str, str] = field(default_factory=dict)
    refresh_fp: dict[str, str] = field(default_factory=dict)
    reminder_keys: dict[str, str] = field(default_factory=dict)
    warned_add: set[str] = field(default_factory=set)
    warned_remove: set[str] = field(default_factory=set)

    def has_housekeeping(self) -> bool:
        return bool(self.refresh_seen or self.refresh_fp or self.silent_fp_inits or self.warned_remove)

    def apply_housekeeping(self, state: dict[str, Any]) -> None:
        state["seen"].update(self.refresh_seen)
        state["fp"].update(self.silent_fp_inits)
        for key, iso in self.refresh_fp.items():
            if key in state["fp"]:
                state["fp"][key]["t"] = iso
        warned = state.setdefault("warned", [])
        for name in self.warned_remove:
            if name in warned:
                warned.remove(name)

    def commit_all(self, state: dict[str, Any]) -> None:
        self.apply_housekeeping(state)
        state["seen"].update(self.seen_updates)
        state["fp"].update(self.fp_updates)
        state["sent_reminders"].update(self.reminder_keys)
        warned = state.setdefault("warned", [])
        for name in self.warned_add:
            if name not in warned:
                warned.append(name)


# ---------- config / quiet hours ----------
def load_dotenv(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def parse_bool(val: str | None, default: bool) -> bool:
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


def parse_number_list(val: str | None, default: list[int]) -> list[int]:
    if not val or not val.strip():
        return list(default)
    nums: list[int] = []
    for part in val.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if n > 0:
                nums.append(n)
        except ValueError:
            continue
    return sorted(set(nums), reverse=True) if nums else list(default)


def parse_quiet_hours(raw: str | None) -> QuietHours | None:
    if not raw or not raw.strip():
        return None
    raw_clean = raw.strip()
    m = re.match(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$", raw_clean)
    if not m:
        raise ValueError(f'Invalid QUIET_HOURS format "{raw_clean}". Expected HH:MM-HH:MM (e.g. 23:00-07:00).')
    sh, sm, eh, em = (int(x) for x in m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise ValueError(f'Invalid QUIET_HOURS time range "{raw_clean}".')
    return QuietHours(start_minutes=sh * 60 + sm, end_minutes=eh * 60 + em, raw=raw_clean)


def is_within_quiet_hours(now: datetime, quiet_hours: QuietHours | None, tz: Any) -> bool:
    if quiet_hours is None:
        return False
    local_now = now.astimezone(tz)
    current_minutes = local_now.hour * 60 + local_now.minute
    if quiet_hours.start_minutes <= quiet_hours.end_minutes:
        return quiet_hours.start_minutes <= current_minutes < quiet_hours.end_minutes
    # Spans midnight (e.g. 23:00-07:00)
    return current_minutes >= quiet_hours.start_minutes or current_minutes < quiet_hours.end_minutes


def load_config() -> None:
    CFG["tg_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    CFG["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    CFG["poll"] = max(30, int(os.environ.get("POLL_SECONDS", "300")))
    CFG["remind_hours"] = max(1, int(os.environ.get("REMIND_HOURS", "24")))

    raw_asn_rem = os.environ.get("ASSIGNMENT_REMINDER_MINUTES", "").strip()
    if raw_asn_rem:
        CFG["assignment_reminder_minutes"] = parse_number_list(raw_asn_rem, [1440, 180, 60])
    elif os.environ.get("REMIND_HOURS") and CFG["remind_hours"] != 24:
        CFG["assignment_reminder_minutes"] = [CFG["remind_hours"] * 60]
    else:
        CFG["assignment_reminder_minutes"] = [1440, 180, 60]

    CFG["event_reminder_minutes"] = parse_number_list(
        os.environ.get("EVENT_REMINDER_MINUTES"), [30, 15]
    )
    CFG["upcoming_lookahead_days"] = max(1, min(30, int(os.environ.get("UPCOMING_LOOKAHEAD_DAYS", "7"))))
    CFG["alert_on_first_run"] = parse_bool(os.environ.get("ALERT_ON_FIRST_RUN"), False)
    CFG["digest_mode"] = parse_bool(os.environ.get("DIGEST_MODE"), True)
    CFG["enable_commands"] = parse_bool(os.environ.get("TELEGRAM_ENABLE_COMMANDS"), True)
    # Avoid creating noisy git commits on GitHub Actions when only last_poll_at changed
    default_persist_poll = os.environ.get("GITHUB_ACTIONS", "").lower() != "true"
    CFG["persist_last_poll"] = parse_bool(os.environ.get("PERSIST_LAST_POLL"), default_persist_poll)

    tzname = os.environ.get("TIMEZONE", "Asia/Phnom_Penh").strip() or "Asia/Phnom_Penh"
    CFG["tz_name"] = tzname
    try:
        CFG["tz"] = ZoneInfo(tzname) if ZoneInfo else timezone.utc
    except Exception:
        print(f"[warn] unknown TIMEZONE '{tzname}', showing times in UTC", file=sys.stderr)
        CFG["tz"] = timezone.utc
        CFG["tz_name"] = "UTC"

    raw_qh = os.environ.get("QUIET_HOURS", "").strip()
    try:
        CFG["quiet_hours"] = parse_quiet_hours(raw_qh)
    except ValueError as e:
        print(f"[warn] {e} — quiet hours disabled", file=sys.stderr)
        CFG["quiet_hours"] = None


# ---------- state persistence ----------
def load_state(path: Path | None = None) -> dict[str, Any]:
    target = path or STATE_FILE
    state: dict[str, Any] = {}
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except json.JSONDecodeError:
            state = {}
    state.setdefault("seen", {})            # key -> iso time last (re)confirmed
    state.setdefault("fp", {})              # key -> {"t": iso, "v": {field: value}}
    state.setdefault("initialized", [])
    state.setdefault("warned", [])
    state.setdefault("sent_reminders", {})  # reminder_key -> iso time sent
    state.setdefault("upcoming", [])        # cached upcoming assignments & events
    state.setdefault("last_poll_at", None)
    state.setdefault("last_alert_at", None)
    return state


def save_state(state: dict[str, Any], path: Path | None = None, now: datetime | None = None) -> None:
    target = path or STATE_FILE
    ref_now = now or datetime.now(timezone.utc)
    cutoff = (ref_now - timedelta(days=RETENTION_DAYS)).isoformat()
    state["seen"] = {k: v for k, v in state.get("seen", {}).items() if isinstance(v, str) and v >= cutoff}
    state["fp"] = {
        k: v for k, v in state.get("fp", {}).items()
        if isinstance(v, dict) and isinstance(v.get("t"), str) and v["t"] >= cutoff
    }
    state["sent_reminders"] = {
        k: v for k, v in state.get("sent_reminders", {}).items()
        if isinstance(v, str) and v >= cutoff
    }

    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(state, indent=1)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(serialized, encoding="utf-8")
    os.replace(tmp_path, target)


def stage_seen_check(state: dict[str, Any], changes: StateChanges, key: str, now: datetime) -> bool:
    """Return True if `key` is new. Stages the write in `changes` instead of mutating `state` immediately."""
    old = state["seen"].get(key)
    now_iso = now.isoformat()
    if old is None:
        changes.seen_updates[key] = now_iso
        return True
    if old < (now - timedelta(days=30)).isoformat():
        changes.refresh_seen[key] = now_iso
    return False


def stage_fp_check(
    state: dict[str, Any], changes: StateChanges, key: str, vals: dict[str, Any], now: datetime
) -> dict[str, tuple[Any, Any]]:
    """Compare fingerprint to stored one without mutating `state` until committed."""
    rec = state["fp"].get(key)
    now_iso = now.isoformat()
    if rec is None:
        # Initial fingerprint produces no diff alert; safe to commit as housekeeping unless newly seen
        if key in state["seen"]:
            changes.silent_fp_inits[key] = {"t": now_iso, "v": vals}
        else:
            changes.fp_updates[key] = {"t": now_iso, "v": vals}
        return {}
    old = rec.get("v", {})
    changed = {k: (old.get(k), vals.get(k)) for k in vals if old.get(k) != vals.get(k)}
    if changed:
        changes.fp_updates[key] = {"t": now_iso, "v": vals}
    elif rec.get("t", "") < (now - timedelta(days=30)).isoformat():
        changes.refresh_fp[key] = now_iso
    return changed


# ---------- formatting helpers ----------
def clean(h: str | None, limit: int = 400) -> str:
    if not h:
        return ""
    t = re.sub(r"<(script|style).*?</\1>", "", h, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = re.sub(r"\n\s*\n+", "\n", t).strip()
    return truncate(t, limit)


def truncate(text: str, limit: int = 180) -> str:
    cleaned = text.strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def digest(h: str | None) -> str:
    """Short hash of the visible text of an HTML body (ignores tags and link tokens)."""
    text = re.sub(r"\s+", " ", clean(h, limit=10**7)).strip()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def parse_iso(dt_str: str | None) -> datetime | None:
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def local(dt_str: str | None, tz: Any = None) -> str:
    if not dt_str:
        return "none"
    dt = parse_iso(dt_str)
    if dt is None:
        return dt_str
    target_tz = tz or CFG["tz"]
    return f"{dt.astimezone(target_tz):%a %d %b, %H:%M}"


def format_lead_time(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        days = minutes // 1440
        return f"{days} day" if days == 1 else f"{days} days"
    if minutes >= 60 and minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    return f"{minutes} min"


def format_lead_short(minutes: int) -> str:
    if minutes >= 1440 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes >= 60 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def change_lines(changed: dict[str, tuple[Any, Any]], tz: Any = None) -> list[str]:
    out: list[str] = []
    for k, (o, n) in changed.items():
        if k == "desc":
            out.append("• Description edited")
        elif k == "body":
            out.append("• Text edited")
        elif k in DATE_FIELDS:
            out.append(f"• {FIELD_LABELS[k]}: {esc(local(o, tz))} → {esc(local(n, tz))}")
        else:
            show = lambda v: esc(str(v)) if v not in (None, "") else "none"
            out.append(f"• {FIELD_LABELS.get(k, k)}: {show(o)} → {show(n)}")
    return out


def format_single_digest_item(item: CanvasNotification, tz: Any = None) -> str:
    lines: list[str] = []
    prefix = f"[{esc(item.badge)}] " if item.badge else ""
    title_text = esc(truncate(item.title or "(untitled)", 140))
    title_html = f'<a href="{esc(item.url)}">{title_text}</a>' if item.url else f"<b>{title_text}</b>"
    lines.append(f"• {prefix}{title_html}")

    meta_parts: list[str] = []
    if item.account:
        meta_parts.append(esc(item.account))
    if item.course:
        meta_parts.append(esc(item.course))
    if item.channel and item.channel != item.course:
        meta_parts.append(f"📂 {esc(item.channel)}")
    if item.sender:
        meta_parts.append(f"by {esc(item.sender)}")
    if meta_parts:
        lines.append(f"  <i>{' · '.join(meta_parts)}</i>")

    if item.grade_line:
        lines.append(f"  {item.grade_line}")

    if item.due_at:
        lines.append(f"  Due: <b>{esc(local(item.due_at, tz))}</b>")

    if item.starts_at:
        lines.append(f"  Starts: <b>{esc(local(item.starts_at, tz))}</b>")

    for d in item.details:
        lines.append(f"  {d}")

    if item.summary:
        lines.append(f"  {esc(truncate(item.summary, 220))}")

    return "\n".join(lines)


def format_digest_messages(
    items: list[CanvasNotification],
    tz: Any = None,
    checked_at: datetime | None = None,
) -> list[str]:
    if not items:
        return []

    grouped: dict[str, list[CanvasNotification]] = {}
    for item in items:
        grouped.setdefault(item.category, []).append(item)

    ref_time = checked_at or datetime.now(timezone.utc)
    time_label = local(ref_time.isoformat(), tz)
    plural = "" if len(items) == 1 else "s"
    header = (
        f"🎓 <b>Canvas Digest</b> ({len(items)} update{plural})\n"
        f"<i>Checked {esc(time_label)}</i>"
    )

    blocks: list[str] = []
    for cat in CATEGORY_ORDER:
        cat_items = grouped.get(cat)
        if not cat_items:
            continue
        blocks.append(f"\n<b>{CATEGORY_LABELS.get(cat, '🔔 Updates')}</b>")
        for item in cat_items:
            blocks.append(format_single_digest_item(item, tz))

    messages: list[str] = []
    current = header
    for block in blocks:
        candidate = f"{current}\n{block}"
        if len(candidate) > TELEGRAM_MAX_LENGTH and current != header:
            messages.append(current)
            current = f"🎓 <b>Canvas Digest (cont.)</b>\n{block}"
        else:
            current = candidate

    if current.strip():
        messages.append(current)

    return messages


def format_individual_message(item: CanvasNotification, tz: Any = None) -> str:
    head = CATEGORY_LABELS.get(item.category, "🔔 Update")
    if item.badge:
        head = f"{item.badge}"
    acct = f" · {esc(item.account)}" if item.account else ""
    lines = [f"<b>{esc(head)}</b>{acct}"]
    if item.course:
        lines.append(f"📚 {esc(item.course)}")
    if item.channel:
        lines.append(f"📂 {esc(item.channel)}")
    lines.append(f"<b>{esc(item.title or '(untitled)')}</b>")
    if item.sender:
        lines.append(f"👤 {esc(item.sender)}")
    if item.grade_line:
        lines.append(item.grade_line)
    if item.due_at:
        lines.append(f"Due: <b>{esc(local(item.due_at, tz))}</b>")
    if item.starts_at:
        lines.append(f"Starts: <b>{esc(local(item.starts_at, tz))}</b>")
    lines.extend(item.details)
    if item.summary:
        lines.append(esc(truncate(item.summary, 400)))
    return "\n".join(lines)


def format_upcoming_message(
    upcoming: list[dict[str, Any]],
    days: int = 7,
    tz: Any = None,
    now: datetime | None = None,
) -> str:
    ref_now = now or datetime.now(timezone.utc)
    cutoff_past = ref_now - timedelta(hours=1)
    cutoff_future = ref_now + timedelta(days=days)

    assignments: list[tuple[datetime, dict[str, Any]]] = []
    events: list[tuple[datetime, dict[str, Any]]] = []

    for item in upcoming:
        cat = item.get("category", "assignment")
        iso = item.get("due_at") if cat == "assignment" else item.get("starts_at")
        dt = parse_iso(iso)
        if dt is None or dt < cutoff_past or dt > cutoff_future:
            continue
        if cat == "event":
            events.append((dt, item))
        else:
            assignments.append((dt, item))

    assignments.sort(key=lambda pair: pair[0])
    events.sort(key=lambda pair: pair[0])

    if not assignments and not events:
        return f"🎉 Nothing due or scheduled in the next {days} day(s)."

    lines = [f"📋 <b>Upcoming Canvas Schedule</b> (next {days} day{'s' if days != 1 else ''})"]

    if assignments:
        lines.append(f"\n<b>📝 Assignments & Quizzes</b> ({len(assignments)})")
        for _, a in assignments[:20]:
            title = esc(truncate(a.get("title") or "(untitled)", 100))
            url = a.get("url")
            title_html = f'<a href="{esc(url)}">{title}</a>' if url else f"<b>{title}</b>"
            course = f" <i>({esc(a['course'])})</i>" if a.get("course") else ""
            due = esc(local(a.get("due_at"), tz))
            lines.append(f"• {title_html}{course} — Due <b>{due}</b>")
        if len(assignments) > 20:
            lines.append(f"… and {len(assignments) - 20} more")

    if events:
        lines.append(f"\n<b>📅 Calendar Events</b> ({len(events)})")
        for _, e in events[:15]:
            title = esc(truncate(e.get("title") or "(untitled)", 100))
            url = e.get("url")
            title_html = f'<a href="{esc(url)}">{title}</a>' if url else f"<b>{title}</b>"
            course = f" <i>({esc(e['course'])})</i>" if e.get("course") else ""
            starts = esc(local(e.get("starts_at"), tz))
            lines.append(f"• {title_html}{course} — <b>{starts}</b>")
        if len(events) > 15:
            lines.append(f"… and {len(events) - 15} more")

    return "\n".join(lines)


# ---------- Telegram client ----------
def tg_api(method: str, payload: dict[str, Any] | None = None, timeout: int = 35) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{CFG['tg_token']}/{method}"
    for attempt in range(3):
        r = requests.post(url, json=payload or {}, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {"ok": False, "description": r.text}
        if data.get("ok"):
            return data
        if r.status_code == 429 and attempt < 2:
            retry_after = data.get("parameters", {}).get("retry_after", 5)
            time.sleep(retry_after + 1)
            continue
        print(f"[telegram:{method}] {r.status_code}: {data.get('description', r.text)}", file=sys.stderr)
        return data
    return {"ok": False}


def tg_send(
    text: str,
    button_url: str | None = None,
    chat_id: str | None = None,
    reply_markup: dict[str, Any] | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id or CFG["chat_id"],
        "text": text[:4096],
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    elif button_url:
        payload["reply_markup"] = {
            "inline_keyboard": [[{"text": "Open in Canvas", "url": button_url}]]
        }
    res = tg_api("sendMessage", payload)
    time.sleep(0.3)
    return bool(res.get("ok"))


def tg_answer_callback(callback_query_id: str, text: str | None = None) -> bool:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    res = tg_api("answerCallbackQuery", payload)
    return bool(res.get("ok"))


def tg_register_commands() -> bool:
    res = tg_api("setMyCommands", {"commands": BOT_COMMANDS})
    return bool(res.get("ok"))


def send_notifications(
    items: list[CanvasNotification],
    now: datetime,
    chat_id: str | None = None,
) -> bool:
    if not items:
        return True
    if CFG.get("digest_mode", True):
        messages = format_digest_messages(items, tz=CFG["tz"], checked_at=now)
        all_ok = True
        for idx, msg in enumerate(messages):
            is_last = idx == len(messages) - 1
            ok = tg_send(
                msg,
                chat_id=chat_id,
                reply_markup=QUICK_INLINE_KEYBOARD if is_last else None,
            )
            all_ok = all_ok and ok
        return all_ok

    all_ok = True
    for idx, item in enumerate(items):
        is_last = idx == len(items) - 1
        markup = None
        if item.url and is_last:
            markup = {
                "inline_keyboard": [
                    [{"text": "Open in Canvas", "url": item.url}],
                    *QUICK_INLINE_KEYBOARD["inline_keyboard"],
                ]
            }
        elif is_last:
            markup = QUICK_INLINE_KEYBOARD
        ok = tg_send(
            format_individual_message(item, tz=CFG["tz"]),
            button_url=item.url if markup is None else None,
            chat_id=chat_id,
            reply_markup=markup,
        )
        all_ok = all_ok and ok
    return all_ok


# ---------- Canvas client ----------
class Canvas:
    def __init__(self, name: str, base_url: str, token: str, expires: str | None = None):
        self.name = name
        self.expires = expires  # optional "YYYY-MM-DD" for expiry warnings
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"

    def get_all(self, path: str, max_pages: int = 3, **params: Any) -> list[Any]:
        url, out = f"{self.base}/api/v1{path}", []
        for _ in range(max_pages):
            r = self.s.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            out.extend(data if isinstance(data, list) else [data])
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                break
            url, params = nxt, None
        return out

    def courses(self) -> dict[int, str]:
        cs = self.get_all("/courses", enrollment_state="active", per_page=100)
        return {c["id"]: c.get("name") or c.get("course_code") or "Course" for c in cs if "id" in c}

    def full_url(self, url: str | None) -> str | None:
        if not url:
            return None
        if url.startswith("/"):
            return self.base + url
        return url


# ---------- feature pollers ----------
def poll_stream(
    c: Canvas,
    state: dict[str, Any],
    changes: StateChanges,
    courses: dict[int, str],
    now: datetime,
    suppress: bool,
) -> list[CanvasNotification]:
    alerts: list[CanvasNotification] = []
    stream = c.get_all("/users/self/activity_stream", only_active_courses="true", per_page=50)
    stream.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or "")
    for it in stream:
        if it.get("type") == "Announcement":
            continue  # handled by poll_announcements
        key = f"{c.name}:{it.get('type')}:{it.get('id')}:{it.get('updated_at')}"
        if not stage_seen_check(state, changes, key, now) or suppress:
            continue
        it_type = it.get("type") or "Update"
        category = "grade" if it_type == "Submission" else "activity"
        grade_line = ""
        if it_type == "Submission" and it.get("grade") is not None:
            score = f" ({it['score']} pts)" if it.get("score") is not None else ""
            grade_line = f"Grade: <b>{esc(str(it['grade']))}</b>{score}"
        alerts.append(
            CanvasNotification(
                category=category,
                badge=ICONS.get(it_type, "🔔 Update"),
                account=c.name,
                course=courses.get(it.get("course_id"), ""),
                title=it.get("title") or "(no title)",
                grade_line=grade_line,
                summary=clean(it.get("message"), limit=220),
                url=c.full_url(it.get("html_url")),
            )
        )
    return alerts


def poll_announcements(
    c: Canvas,
    state: dict[str, Any],
    changes: StateChanges,
    courses: dict[int, str],
    now: datetime,
) -> list[CanvasNotification]:
    first = (f"{c.name}:ann" not in state["initialized"]) and not CFG["alert_on_first_run"]
    ids = list(courses)
    anns: list[dict[str, Any]] = []
    for i in range(0, len(ids), 10):  # Canvas allows max 10 context codes per call
        try:
            anns += c.get_all(
                "/announcements",
                start_date=(now - timedelta(days=30)).date().isoformat(),
                per_page=50,
                **{"context_codes[]": [f"course_{x}" for x in ids[i:i + 10]]},
            )
        except requests.HTTPError:
            pass
    anns.sort(key=lambda a: a.get("posted_at") or "")
    alerts: list[CanvasNotification] = []
    for a in anns:
        key = f"{c.name}:ann:{a.get('id')}"
        vals = {"title": a.get("title") or "", "body": digest(a.get("message"))}
        is_new = stage_seen_check(state, changes, key, now)
        changed = stage_fp_check(state, changes, key, vals, now)
        cid = str(a.get("context_code", "")).replace("course_", "")
        course = courses.get(int(cid)) if cid.isdigit() else ""
        if is_new and not first:
            alerts.append(
                CanvasNotification(
                    category="announcement",
                    badge="New",
                    account=c.name,
                    course=course,
                    sender=a.get("user_name") or "",
                    title=vals["title"] or "(no title)",
                    summary=clean(a.get("message"), limit=240),
                    url=c.full_url(a.get("html_url")),
                )
            )
        elif not is_new and changed:
            details: list[str] = []
            if "title" in changed:
                details.append(f"• Title was: {esc(changed['title'][0] or 'none')}")
            if "body" in changed:
                details.append("• Text edited")
            alerts.append(
                CanvasNotification(
                    category="announcement",
                    badge="Edited",
                    account=c.name,
                    course=course,
                    sender=a.get("user_name") or "",
                    title=vals["title"] or "(no title)",
                    details=details,
                    summary=clean(a.get("message"), limit=220) if "body" in changed else "",
                    url=c.full_url(a.get("html_url")),
                )
            )
    return alerts


def poll_assignments(
    c: Canvas,
    state: dict[str, Any],
    changes: StateChanges,
    courses: dict[int, str],
    now: datetime,
) -> list[CanvasNotification]:
    first = (f"{c.name}:asn" not in state["initialized"]) and not CFG["alert_on_first_run"]
    alerts: list[CanvasNotification] = []
    for cid, cname in courses.items():
        try:
            asns = c.get_all(f"/courses/{cid}/assignments", max_pages=2, per_page=100)
        except requests.HTTPError:
            continue
        asns.sort(key=lambda a: a.get("created_at") or "")
        for a in asns:
            key = f"{c.name}:asn:{a.get('id')}"
            vals = {
                "name": a.get("name") or "",
                "due_at": a.get("due_at"),
                "points_possible": a.get("points_possible"),
                "unlock_at": a.get("unlock_at"),
                "lock_at": a.get("lock_at"),
                "desc": digest(a.get("description")),
            }
            is_new = stage_seen_check(state, changes, key, now)
            changed = stage_fp_check(state, changes, key, vals, now)
            if is_new and not first:
                details = []
                if a.get("points_possible") is not None:
                    details.append(f"Points: <b>{esc(str(a['points_possible']))}</b>")
                alerts.append(
                    CanvasNotification(
                        category="assignment",
                        badge="New",
                        account=c.name,
                        course=cname,
                        title=vals["name"] or "(untitled)",
                        due_at=a.get("due_at"),
                        details=details,
                        url=c.full_url(a.get("html_url")),
                    )
                )
            elif not is_new and changed:
                alerts.append(
                    CanvasNotification(
                        category="assignment",
                        badge="Updated",
                        account=c.name,
                        course=cname,
                        title=vals["name"] or "(untitled)",
                        details=change_lines(changed, tz=CFG["tz"]),
                        url=c.full_url(a.get("html_url")),
                    )
                )
    return alerts


def poll_modules(
    c: Canvas,
    state: dict[str, Any],
    changes: StateChanges,
    courses: dict[int, str],
    now: datetime,
) -> list[CanvasNotification]:
    first = (f"{c.name}:mod" not in state["initialized"]) and not CFG["alert_on_first_run"]
    alerts: list[CanvasNotification] = []
    for cid, cname in courses.items():
        try:
            mods = c.get_all(
                f"/courses/{cid}/modules",
                max_pages=3,
                per_page=50,
                **{"include[]": ["items"]},
            )
        except requests.HTTPError:
            continue
        for m in mods:
            items = m.get("items")
            if items is None:  # Canvas may omit items inline for big modules
                try:
                    items = c.get_all(f"/courses/{cid}/modules/{m['id']}/items", per_page=100)
                except requests.HTTPError:
                    continue
            for it in items:
                if it.get("type") in ("SubHeader", "Assignment"):
                    continue  # subheaders are noise; assignments already alert via poll_assignments
                key = f"{c.name}:mod:{it.get('id')}"
                title = it.get("title") or "(untitled)"
                is_new = stage_seen_check(state, changes, key, now)
                changed = stage_fp_check(state, changes, key, {"title": title}, now)
                icon = MODULE_ICONS.get(it.get("type"), "📌")
                mod_name = m.get("name") or "Module"
                if is_new and not first:
                    alerts.append(
                        CanvasNotification(
                            category="module",
                            badge=icon,
                            account=c.name,
                            course=cname,
                            channel=mod_name,
                            title=title,
                            url=c.full_url(it.get("html_url")),
                        )
                    )
                elif not is_new and changed:
                    alerts.append(
                        CanvasNotification(
                            category="module",
                            badge=f"✏️ {icon} Renamed",
                            account=c.name,
                            course=cname,
                            channel=mod_name,
                            title=title,
                            details=change_lines(changed, tz=CFG["tz"]),
                            url=c.full_url(it.get("html_url")),
                        )
                    )
    return alerts


def fetch_upcoming_items(c: Canvas, now: datetime, days: int) -> list[dict[str, Any]]:
    """Fetch upcoming assignments, quizzes, discussions, and calendar events from Canvas Planner."""
    items = c.get_all(
        "/planner/items",
        start_date=now.isoformat(),
        end_date=(now + timedelta(days=days)).isoformat(),
        per_page=50,
    )
    upcoming: list[dict[str, Any]] = []
    for item in items:
        ptype = item.get("plannable_type")
        p = item.get("plannable") or {}
        if (item.get("planner_override") or {}).get("marked_complete"):
            continue
        if ptype in ("assignment", "quiz", "discussion_topic"):
            due = p.get("due_at")
            if not due:
                continue
            if (item.get("submissions") or {}).get("submitted"):
                continue
            upcoming.append(
                {
                    "id": f"{c.name}:{ptype}:{p.get('id')}",
                    "plannable_id": str(p.get("id") or ""),
                    "account": c.name,
                    "category": "assignment",
                    "title": p.get("title") or "(untitled)",
                    "course": item.get("context_name") or "",
                    "due_at": due,
                    "url": c.full_url(item.get("html_url")),
                }
            )
        elif ptype == "calendar_event":
            starts = p.get("start_at") or item.get("plannable_date")
            if not starts:
                continue
            upcoming.append(
                {
                    "id": f"{c.name}:event:{p.get('id')}",
                    "plannable_id": str(p.get("id") or ""),
                    "account": c.name,
                    "category": "event",
                    "title": p.get("title") or "(untitled event)",
                    "course": item.get("context_name") or "",
                    "starts_at": starts,
                    "ends_at": p.get("end_at"),
                    "url": c.full_url(item.get("html_url")),
                }
            )
    return upcoming


def build_reminders(
    upcoming: list[dict[str, Any]],
    state: dict[str, Any],
    changes: StateChanges,
    now: datetime,
) -> list[CanvasNotification]:
    """Generate multi-tier due-soon reminders matching MS Teams Bot's tightest-threshold logic."""
    reminders: list[CanvasNotification] = []
    asn_thresholds = sorted(CFG["assignment_reminder_minutes"])
    evt_thresholds = sorted(CFG["event_reminder_minutes"])
    now_iso = now.isoformat()

    for item in upcoming:
        cat = item.get("category", "assignment")
        target_iso = item.get("due_at") if cat == "assignment" else item.get("starts_at")
        target_dt = parse_iso(target_iso)
        if target_dt is None or target_dt <= now:
            continue

        minutes_until = (target_dt - now).total_seconds() / 60.0
        thresholds = asn_thresholds if cat == "assignment" else evt_thresholds
        matched = next((t for t in thresholds if minutes_until <= t), None)
        if matched is None:
            continue

        rem_key = f"rem:{item['id']}:{target_iso}:{matched}m"
        if rem_key in state.get("sent_reminders", {}) or rem_key in changes.reminder_keys:
            continue

        # Backward compatibility with legacy single-tier "seen" key (24h)
        if cat == "assignment" and matched == 1440:
            legacy_key = f"{item.get('account', '')}:due:{item.get('plannable_id', '')}:{target_iso}"
            if legacy_key in state.get("seen", {}):
                changes.reminder_keys[rem_key] = now_iso
                continue

        changes.reminder_keys[rem_key] = now_iso
        label = format_lead_time(matched)
        title_prefix = (
            f"Assignment due in ≤ {label}" if cat == "assignment" else f"Event starts in ≤ {label}"
        )
        reminders.append(
            CanvasNotification(
                category="reminder",
                badge=f"≤ {format_lead_short(matched)}",
                account=item.get("account") or "",
                course=item.get("course") or "",
                title=f"{title_prefix}: {item.get('title') or '(untitled)'}",
                due_at=item.get("due_at"),
                starts_at=item.get("starts_at"),
                url=item.get("url"),
                is_urgent=matched <= 60,
            )
        )
    return reminders


def check_expiry(
    c: Canvas, state: dict[str, Any], changes: StateChanges, now: datetime
) -> list[CanvasNotification]:
    """Warn on Telegram (once a day) when the Canvas token is about to expire."""
    if not c.expires:
        return []
    today = now.date()
    try:
        days = (datetime.fromisoformat(c.expires).date() - today).days
    except ValueError:
        return []
    if days > 3:
        return []
    key = f"{c.name}:expiry:{today}"
    if key in state["seen"]:
        return []
    changes.seen_updates[key] = now.isoformat()
    when = f"expires in {days} day(s)" if days >= 0 else "has expired"
    return [
        CanvasNotification(
            category="system",
            badge="Token",
            account=c.name,
            title=f"Canvas token for {c.name} {when}",
            summary="Create a new token in Canvas and update accounts.json / CANVAS_ACCOUNTS secret.",
            is_urgent=True,
        )
    ]


# ---------- orchestration ----------
def deduplicate_upcoming(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_map: dict[str, dict[str, Any]] = {}
    for item in items:
        seen_map[item["id"]] = item
    return list(seen_map.values())


def poll_once(
    accounts: list[Canvas],
    state: dict[str, Any],
    *,
    manual_chat_id: str | None = None,
    ignore_quiet_hours: bool = False,
    now: datetime | None = None,
    state_path: Path | None = None,
) -> dict[str, Any]:
    ref_now = now or datetime.now(timezone.utc)
    now_iso = ref_now.isoformat()

    all_alerts: list[CanvasNotification] = []
    all_upcoming: list[dict[str, Any]] = []
    upcoming_fetched_any = False
    first_run_accounts: list[tuple[str, int]] = []
    changes = StateChanges()
    state_dirty = False

    for c in accounts:
        acct_changes = StateChanges()
        try:
            first_run = c.name not in state["initialized"]
            suppress_stream = first_run and not CFG["alert_on_first_run"]
            courses = c.courses()

            acct_alerts: list[CanvasNotification] = []
            acct_alerts.extend(poll_stream(c, state, acct_changes, courses, ref_now, suppress_stream))
            acct_alerts.extend(poll_announcements(c, state, acct_changes, courses, ref_now))
            acct_alerts.extend(poll_assignments(c, state, acct_changes, courses, ref_now))
            acct_alerts.extend(poll_modules(c, state, acct_changes, courses, ref_now))

            lookahead_days = max(
                CFG["upcoming_lookahead_days"],
                int((max(CFG["assignment_reminder_minutes"], default=1440) / 1440) + 1),
            )
            try:
                acct_upcoming = fetch_upcoming_items(c, ref_now, lookahead_days)
                all_upcoming.extend(acct_upcoming)
                upcoming_fetched_any = True
            except requests.HTTPError:
                pass

            if c.name in state.get("warned", []):
                acct_changes.warned_remove.add(c.name)

            acct_alerts.extend(check_expiry(c, state, acct_changes, ref_now))

            if first_run and not CFG["alert_on_first_run"]:
                # Commit baseline silently so existing items never flood Telegram
                baseline_count = len(acct_changes.seen_updates)
                acct_changes.commit_all(state)
                for flag in (c.name, f"{c.name}:ann", f"{c.name}:asn", f"{c.name}:mod"):
                    if flag not in state["initialized"]:
                        state["initialized"].append(flag)
                first_run_accounts.append((c.name, baseline_count))
                state_dirty = True
            else:
                # Merge staged changes into cycle-level changes
                changes.seen_updates.update(acct_changes.seen_updates)
                changes.fp_updates.update(acct_changes.fp_updates)
                changes.silent_fp_inits.update(acct_changes.silent_fp_inits)
                changes.refresh_seen.update(acct_changes.refresh_seen)
                changes.refresh_fp.update(acct_changes.refresh_fp)
                changes.warned_add.update(acct_changes.warned_add)
                changes.warned_remove.update(acct_changes.warned_remove)
                all_alerts.extend(acct_alerts)
                for flag in (c.name, f"{c.name}:ann", f"{c.name}:asn", f"{c.name}:mod"):
                    if flag not in state["initialized"]:
                        state["initialized"].append(flag)
                        state_dirty = True

            print(f"[{ref_now.astimezone(CFG['tz']):%H:%M:%S}] {c.name}: polled ({len(acct_alerts)} alert(s))")
        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else "?"
            print(f"[{c.name}] HTTP {code}", file=sys.stderr)
            if code == 401 and c.name not in state.get("warned", []):
                changes.warned_add.add(c.name)
                all_alerts.append(
                    CanvasNotification(
                        category="system",
                        badge="Auth 401",
                        account=c.name,
                        title=f"Canvas token for {c.name} was rejected (401)",
                        summary="Generate a new access token in Canvas and update your config.",
                        is_urgent=True,
                    )
                )
        except Exception as e:
            print(f"[{c.name}] error: {e}", file=sys.stderr)

    # Refresh cached upcoming items if fetch succeeded
    if upcoming_fetched_any:
        deduped = deduplicate_upcoming(all_upcoming)
        if deduped != state.get("upcoming"):
            state["upcoming"] = deduped
            state_dirty = True

    # Build multi-tier reminders from current upcoming items
    reminders = build_reminders(state.get("upcoming", []), state, changes, ref_now)
    pending_alerts = reminders + all_alerts

    # Send welcome message(s) for newly baselined accounts
    for acct_name, baseline_count in first_run_accounts:
        rem_str = ", ".join(format_lead_short(m) for m in CFG["assignment_reminder_minutes"])
        tg_send(
            "\n".join(
                [
                    f"✅ <b>Canvas Bot Connected!</b> · {esc(acct_name)}",
                    f"Recorded <b>{baseline_count}</b> existing items as baseline (no spam).",
                    f"Reminders: <b>{esc(rem_str)}</b> before deadlines.",
                    "From now on, you'll receive clean grouped alerts for new assignments, announcements, modules, grades, and reminders!",
                ]
            ),
            chat_id=manual_chat_id,
            reply_markup=QUICK_INLINE_KEYBOARD,
        )

    # Always commit periodic 30-day TTL refreshes and recovered-token housekeeping
    if changes.has_housekeeping():
        changes.apply_housekeeping(state)
        state_dirty = True

    in_quiet = (not ignore_quiet_hours) and is_within_quiet_hours(ref_now, CFG["quiet_hours"], CFG["tz"])
    sent_count = 0
    suppressed_by_quiet = False

    if in_quiet and pending_alerts:
        suppressed_by_quiet = True
        print(f"[quiet-hours] Deferring {len(pending_alerts)} alert(s) until quiet hours end.")
    elif pending_alerts:
        if send_notifications(pending_alerts, ref_now, chat_id=manual_chat_id):
            changes.commit_all(state)
            state["last_alert_at"] = now_iso
            sent_count = len(pending_alerts)
            state_dirty = True
    elif manual_chat_id and not first_run_accounts:
        tg_send(
            "✅ Checked Canvas — no new updates since the last check.",
            chat_id=manual_chat_id,
            reply_markup=QUICK_INLINE_KEYBOARD,
        )

    if CFG.get("persist_last_poll", True) or state_dirty:
        state["last_poll_at"] = now_iso
        save_state(state, path=state_path, now=ref_now)
    else:
        # Keep accurate in-memory last_poll_at without churning state.json on GitHub Actions
        state["last_poll_at"] = now_iso

    return {
        "polled_at": now_iso,
        "new_count": len(all_alerts),
        "reminder_count": len(reminders),
        "sent_count": sent_count,
        "suppressed_by_first_run": bool(first_run_accounts),
        "suppressed_by_quiet_hours": suppressed_by_quiet,
    }


# ---------- Telegram commands & interactive buttons ----------
HELP_TEXT = (
    "🎓 <b>Canvas LMS Alert Bot</b>\n\n"
    "Use the buttons below or the Telegram <b>Menu</b> button:\n"
    "• <b>🔄 Check Now</b> (/check) — Check Canvas right now\n"
    "• <b>📋 Upcoming</b> (/upcoming [days]) — Deadlines & events (default 7 days)\n"
    "• <b>📚 Courses</b> (/courses) — Your active courses\n"
    "• <b>📊 Status</b> (/status) — Bot status, reminders & last check time\n"
    "• /chatid — Show this chat ID"
)


def resolve_command_action(text: str) -> tuple[str | None, list[str]]:
    trimmed = (text or "").strip()
    if not trimmed:
        return None, []

    button_map = {
        "🔄 Check Now": "check",
        "📋 Upcoming": "upcoming",
        "📚 Courses": "courses",
        "📊 Status": "status",
        "❓ Help": "help",
    }
    if trimmed in button_map:
        return button_map[trimmed], []

    parts = trimmed.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]
    slash_map = {
        "/start": "start",
        "/help": "help",
        "/check": "check",
        "/upcoming": "upcoming",
        "/courses": "courses",
        "/status": "status",
        "/chatid": "chatid",
    }
    return slash_map.get(cmd), args


def cmd_upcoming(accounts: list[Canvas], state: dict[str, Any], days: int) -> str:
    now = datetime.now(timezone.utc)
    fetched: list[dict[str, Any]] = []
    any_ok = False
    for c in accounts:
        try:
            fetched.extend(fetch_upcoming_items(c, now, days))
            any_ok = True
        except Exception:
            continue
    items = deduplicate_upcoming(fetched) if any_ok else state.get("upcoming", [])
    return format_upcoming_message(items, days=days, tz=CFG["tz"], now=now)


def cmd_courses(accounts: list[Canvas]) -> str:
    lines: list[str] = ["📚 <b>Active Canvas Courses</b>"]
    found = False
    for c in accounts:
        try:
            courses = c.courses()
        except Exception:
            continue
        sorted_courses = sorted(courses.items(), key=lambda kv: kv[1].lower())
        lines.append(f"\n<b>{esc(c.name)}</b> ({len(sorted_courses)})")
        for cid, name in sorted_courses:
            url = f"{c.base}/courses/{cid}"
            lines.append(f'• <a href="{esc(url)}">{esc(name)}</a>')
            found = True
    return "\n".join(lines) if found else "No active courses found."


def cmd_status(accounts: list[Canvas], state: dict[str, Any]) -> str:
    names = ", ".join(c.name for c in accounts) or "none"
    last_poll = local(state.get("last_poll_at")) if state.get("last_poll_at") else "Never"
    last_alert = local(state.get("last_alert_at")) if state.get("last_alert_at") else "None yet"
    qh = CFG["quiet_hours"].raw if CFG.get("quiet_hours") else "Off"
    asn_rem = ", ".join(format_lead_short(m) for m in CFG["assignment_reminder_minutes"])
    evt_rem = ", ".join(format_lead_short(m) for m in CFG["event_reminder_minutes"])
    mode = "Grouped Digest" if CFG.get("digest_mode", True) else "Individual"
    return "\n".join(
        [
            "🤖 <b>Canvas Alert Bot Status</b>",
            f"Accounts: <b>{esc(names)}</b> ({len(accounts)})",
            f"Mode: <b>{mode}</b> · Timezone: <code>{esc(CFG['tz_name'])}</code>",
            f"Last poll: <b>{esc(last_poll)}</b>",
            f"Last alert: <b>{esc(last_alert)}</b>",
            f"Quiet hours: <b>{esc(qh)}</b>",
            f"Reminders: <b>{esc(asn_rem)}</b> (assignments) · <b>{esc(evt_rem)}</b> (events)",
            f"Tracked items: <b>{len(state.get('seen', {}))}</b> · Upcoming cached: <b>{len(state.get('upcoming', []))}</b>",
        ]
    )


def process_update(
    update: dict[str, Any],
    accounts: list[Canvas],
    state: dict[str, Any],
    *,
    poll_lock: threading.Lock | None = None,
    defer_check: bool = False,
) -> dict[str, Any]:
    """Process one Telegram message or callback_query. Returns flags e.g. {'requested_check_chat_id': ...}."""
    result: dict[str, Any] = {"requested_check_chat_id": None}
    allowed_chat = str(CFG["chat_id"])

    if "callback_query" in update:
        cq = update["callback_query"] or {}
        cq_id = cq.get("id", "")
        chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
        data = cq.get("data") or ""

        if not chat_id:
            if cq_id:
                tg_answer_callback(cq_id)
            return result
        if allowed_chat and chat_id != allowed_chat:
            if cq_id:
                tg_answer_callback(cq_id, "Unauthorized chat")
            return result

        if data == "action:check":
            tg_answer_callback(cq_id, "Checking Canvas...")
            tg_send("🔄 Checking Canvas for new updates...", chat_id=chat_id)
            if defer_check:
                result["requested_check_chat_id"] = chat_id
            else:
                _run_locked_poll(accounts, state, poll_lock, manual_chat_id=chat_id)
        elif data == "action:upcoming":
            tg_answer_callback(cq_id)
            tg_send(cmd_upcoming(accounts, state, 7), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)
        elif data == "action:courses":
            tg_answer_callback(cq_id)
            tg_send(cmd_courses(accounts), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)
        elif data == "action:status":
            tg_answer_callback(cq_id)
            tg_send(cmd_status(accounts, state), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)
        elif data == "action:chatid":
            tg_answer_callback(cq_id)
            tg_send(f"🆔 Your Telegram Chat ID is: <code>{esc(chat_id)}</code>", chat_id=chat_id)
        else:
            tg_answer_callback(cq_id)
        return result

    msg = update.get("message") or {}
    text = msg.get("text")
    if not text:
        return result

    chat_id = str((msg.get("chat") or {}).get("id", ""))
    action, args = resolve_command_action(text)
    if not action:
        return result

    if action == "chatid":
        tg_send(f"🆔 Your Telegram Chat ID is: <code>{esc(chat_id)}</code>", chat_id=chat_id)
        return result

    if allowed_chat and chat_id != allowed_chat:
        return result

    if action in ("start", "help"):
        tg_send(HELP_TEXT, chat_id=chat_id, reply_markup=MAIN_REPLY_KEYBOARD)
    elif action == "check":
        tg_send("🔄 Checking Canvas for new updates...", chat_id=chat_id)
        if defer_check:
            result["requested_check_chat_id"] = chat_id
        else:
            _run_locked_poll(accounts, state, poll_lock, manual_chat_id=chat_id)
    elif action == "upcoming":
        days = int(args[0]) if args and args[0].isdigit() else 7
        days = max(1, min(days, 30))
        tg_send(cmd_upcoming(accounts, state, days), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)
    elif action == "courses":
        tg_send(cmd_courses(accounts), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)
    elif action == "status":
        tg_send(cmd_status(accounts, state), chat_id=chat_id, reply_markup=QUICK_INLINE_KEYBOARD)

    return result


def _run_locked_poll(
    accounts: list[Canvas],
    state: dict[str, Any],
    poll_lock: threading.Lock | None,
    *,
    manual_chat_id: str | None = None,
) -> None:
    if poll_lock is not None:
        if not poll_lock.acquire(blocking=False):
            if manual_chat_id:
                tg_send("⏳ A Canvas check is already in progress. Please wait a moment.", chat_id=manual_chat_id)
            return
        try:
            poll_once(accounts, state, manual_chat_id=manual_chat_id, ignore_quiet_hours=True)
        finally:
            poll_lock.release()
    else:
        poll_once(accounts, state, manual_chat_id=manual_chat_id, ignore_quiet_hours=True)


def handle_commands_once(
    accounts: list[Canvas], state: dict[str, Any], *, defer_check: bool = False
) -> str | None:
    """Drain pending Telegram commands/button taps. Returns manual_chat_id if /check was deferred."""
    res = tg_api(
        "getUpdates",
        {
            "offset": state.get("tg_offset", 0),
            "timeout": 0,
            "allowed_updates": ["message", "callback_query"],
        },
        timeout=30,
    )
    if not res.get("ok"):
        return None
    updates = res.get("result") or []
    check_chat_id: str | None = None
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        info = process_update(u, accounts, state, defer_check=defer_check)
        if info.get("requested_check_chat_id"):
            check_chat_id = info["requested_check_chat_id"]
    if updates:
        save_state(state)
    return check_chat_id


def start_command_listener(
    accounts: list[Canvas],
    state: dict[str, Any],
    poll_lock: threading.Lock,
    stop_event: threading.Event,
) -> threading.Thread:
    """Long-polling Telegram command listener for instant button & slash-command responses in daemon mode."""
    def _loop() -> None:
        print("[telegram] Interactive command listener started (menu + reply keyboard + inline buttons)")
        while not stop_event.is_set():
            try:
                res = tg_api(
                    "getUpdates",
                    {
                        "offset": state.get("tg_offset", 0),
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=35,
                )
                if not res.get("ok"):
                    stop_event.wait(5)
                    continue
                updates = res.get("result") or []
                for u in updates:
                    state["tg_offset"] = u["update_id"] + 1
                    process_update(u, accounts, state, poll_lock=poll_lock, defer_check=False)
                if updates:
                    with poll_lock:
                        save_state(state)
            except Exception as e:
                if stop_event.is_set():
                    break
                print(f"[commands] {e}", file=sys.stderr)
                stop_event.wait(5)

    t = threading.Thread(target=_loop, name="telegram-commands", daemon=True)
    t.start()
    return t


def ensure_menu_registered(state: dict[str, Any], force: bool = False) -> bool:
    if not force and state.get("menu_version", 0) >= MENU_VERSION:
        return True
    try:
        if tg_register_commands():
            state["menu_set"] = True
            state["menu_version"] = MENU_VERSION
            return True
    except Exception as e:
        print(f"[menu] {e}", file=sys.stderr)
    return False


# ---------- entry point ----------
def load_accounts() -> list[Canvas]:
    raw = os.environ.get("CANVAS_ACCOUNTS")
    if raw:
        data = json.loads(raw)
    elif ACCOUNTS_FILE.exists():
        data = json.loads(ACCOUNTS_FILE.read_text(encoding="utf-8"))
    else:
        sys.exit("Create accounts.json (see accounts.example.json) or set CANVAS_ACCOUNTS")
    return [Canvas(a["name"], a["base_url"], a["token"], a.get("expires")) for a in data]


def main() -> None:
    load_dotenv()
    load_config()

    ap = argparse.ArgumentParser(description="Canvas (Instructure) -> Telegram LMS Alert Bot")
    ap.add_argument("--once", action="store_true", help="Run a single poll + pending commands, then exit")
    ap.add_argument("--test", action="store_true", help="Send a test Telegram message")
    ap.add_argument("--get-chat-id", action="store_true", help="Print chat IDs that have messaged your bot")
    ap.add_argument("--setup-menu", action="store_true", help="Register Telegram '/' menu commands now")
    args = ap.parse_args()

    if not CFG["tg_token"]:
        sys.exit("Set TELEGRAM_BOT_TOKEN in .env")

    if args.get_chat_id:
        res = tg_api("getUpdates", {"timeout": 0})
        ids = {u["message"]["chat"]["id"] for u in res.get("result", []) if "message" in u}
        print("Chat IDs:", ids or "none yet - send your bot a message first, then rerun")
        return

    if args.setup_menu:
        print("menu updated" if tg_register_commands() else "failed")
        return

    if not CFG["chat_id"]:
        sys.exit("Set TELEGRAM_CHAT_ID in .env (run: python bot.py --get-chat-id)")

    if args.test:
        ok = tg_send(
            "🤖 <b>Canvas Alert Bot Test</b>\nIt works! Use the quick buttons below anytime.",
            reply_markup=QUICK_INLINE_KEYBOARD,
        )
        print("sent" if ok else "failed")
        return

    accounts = load_accounts()
    state = load_state()
    if ensure_menu_registered(state):
        save_state(state)

    if args.once:
        manual_chat_id = None
        if CFG["enable_commands"]:
            manual_chat_id = handle_commands_once(accounts, state, defer_check=True)
        poll_once(
            accounts,
            state,
            manual_chat_id=manual_chat_id,
            ignore_quiet_hours=bool(manual_chat_id),
        )
        return

    # Daemon mode: instant command listener + periodic polling
    poll_lock = threading.Lock()
    stop_event = threading.Event()

    def _shutdown(signum: int, _frame: Any) -> None:
        print(f"\nReceived signal {signum}; shutting down cleanly...")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _shutdown)

    if CFG["enable_commands"]:
        start_command_listener(accounts, state, poll_lock, stop_event)

    print(
        f"Starting Canvas -> Telegram Bot (accounts={len(accounts)}, poll={CFG['poll']}s, "
        f"tz={CFG['tz_name']}, digest={CFG['digest_mode']})"
    )

    while not stop_event.is_set():
        with poll_lock:
            poll_once(accounts, state)
        stop_event.wait(CFG["poll"])


if __name__ == "__main__":
    main()
