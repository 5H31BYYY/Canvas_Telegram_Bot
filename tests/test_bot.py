from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bot


class FakeCanvas:
    def __init__(
        self,
        name="AUPP",
        base_url="https://aupp.instructure.com",
        courses=None,
        stream=None,
        announcements=None,
        assignments=None,
        modules=None,
        planner=None,
        expires=None,
    ):
        self.name = name
        self.base = base_url.rstrip("/")
        self.expires = expires
        self._courses = courses or {101: "CS101 - Algorithms"}
        self._stream = stream or []
        self._announcements = announcements or []
        self._assignments = assignments or {}
        self._modules = modules or {}
        self._planner = planner or []

    def courses(self):
        return dict(self._courses)

    def full_url(self, url):
        if not url:
            return None
        return self.base + url if url.startswith("/") else url

    def get_all(self, path, max_pages=3, **params):
        if path == "/users/self/activity_stream":
            return list(self._stream)
        if path == "/announcements":
            return list(self._announcements)
        if path.endswith("/assignments"):
            cid = int(path.split("/")[2])
            return list(self._assignments.get(cid, []))
        if path.endswith("/modules"):
            cid = int(path.split("/")[2])
            return list(self._modules.get(cid, []))
        if path == "/planner/items":
            return list(self._planner)
        return []


class CanvasBotTests(unittest.TestCase):
    def setUp(self):
        self._orig_cfg = dict(bot.CFG)
        bot.CFG.update(
            {
                "tg_token": "fake-token",
                "chat_id": "999",
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
        )

    def tearDown(self):
        bot.CFG.clear()
        bot.CFG.update(self._orig_cfg)

    def test_formatter_groups_escapes_and_chunks_under_limit(self):
        items = [
            bot.CanvasNotification(
                category="module",
                badge="📄",
                account="AUPP",
                course="CS201",
                channel="Week 4",
                title="Week 4 Slides <Updated>.pdf",
                url="https://canvas.test/files/1",
            ),
            bot.CanvasNotification(
                category="assignment",
                badge="New",
                account="AUPP",
                course="CS201",
                title="Homework #3 & Quiz",
                due_at="2026-11-25T16:59:00Z",
            ),
            bot.CanvasNotification(
                category="announcement",
                badge="New",
                account="AUPP",
                course="CS201",
                sender="Prof. Ada",
                title="Midterm Review",
                summary="Bring your notes & calculator.",
            ),
        ]
        msgs = bot.format_digest_messages(
            items, tz=timezone.utc, checked_at=datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len(msgs), 1)
        body = msgs[0]
        self.assertIn("Canvas Digest</b> (3 updates)", body)
        self.assertIn("📝 Assignments", body)
        self.assertIn("Homework #3 &amp; Quiz", body)
        self.assertIn("📢 Announcements", body)
        self.assertIn("📚 Module Materials", body)
        self.assertIn("Week 4 Slides &lt;Updated&gt;.pdf", body)

        # Large digest chunks cleanly under 4096 chars
        many = [
            bot.CanvasNotification(
                category="announcement",
                badge="New",
                account="AUPP",
                course="SE301",
                title=f"Announcement #{i + 1} with a fairly long lecture description about next week",
                summary="Please review the attached slides and complete the pre-class reading before class.",
            )
            for i in range(45)
        ]
        chunks = bot.format_digest_messages(many, tz=timezone.utc)
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 4096)

    def test_quiet_hours_spanning_midnight(self):
        qh = bot.parse_quiet_hours("23:00-07:00")
        self.assertTrue(bot.is_within_quiet_hours(datetime(2026, 11, 21, 23, 30, tzinfo=timezone.utc), qh, timezone.utc))
        self.assertTrue(bot.is_within_quiet_hours(datetime(2026, 11, 22, 2, 15, tzinfo=timezone.utc), qh, timezone.utc))
        self.assertFalse(bot.is_within_quiet_hours(datetime(2026, 11, 22, 7, 0, tzinfo=timezone.utc), qh, timezone.utc))
        self.assertFalse(bot.is_within_quiet_hours(datetime(2026, 11, 22, 15, 0, tzinfo=timezone.utc), qh, timezone.utc))

    def test_poller_baseline_alerts_once_and_multi_tier_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = bot.load_state(state_path)
            sent_messages = []

            def fake_tg_send(text, button_url=None, chat_id=None, reply_markup=None):
                sent_messages.append({"text": text, "chat_id": chat_id, "reply_markup": reply_markup})
                return True

            t1 = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
            canvas = FakeCanvas(
                assignments={
                    101: [
                        {
                            "id": 1,
                            "name": "Old Lab 1",
                            "due_at": "2026-11-28T10:00:00Z",
                            "points_possible": 10,
                            "description": "<p>Old</p>",
                            "html_url": "https://aupp.instructure.com/courses/101/assignments/1",
                        }
                    ]
                }
            )

            with patch.object(bot, "tg_send", side_effect=fake_tg_send):
                # 1. First run: silent baseline + 1 welcome confirmation
                r1 = bot.poll_once([canvas], state, now=t1, state_path=state_path)
                self.assertTrue(r1["suppressed_by_first_run"])
                self.assertEqual(r1["sent_count"], 0)
                self.assertEqual(len(sent_messages), 1)
                self.assertIn("Canvas Bot Connected", sent_messages[0]["text"])

                # 2. Second run: 1 brand-new assignment + 1 event starting in 12 mins (hits 15m threshold)
                t2 = datetime(2026, 11, 21, 10, 15, tzinfo=timezone.utc)
                canvas._assignments[101].append(
                    {
                        "id": 2,
                        "name": "Lab 5 Submission",
                        "due_at": "2026-11-25T10:00:00Z",
                        "points_possible": 20,
                        "description": "<p>Submit PDF</p>",
                        "html_url": "https://aupp.instructure.com/courses/101/assignments/2",
                    }
                )
                canvas._planner = [
                    {
                        "plannable_type": "calendar_event",
                        "context_name": "CS101 - Algorithms",
                        "html_url": "/calendar?event_id=99",
                        "plannable": {
                            "id": 99,
                            "title": "Algorithms Live Lecture",
                            "start_at": "2026-11-21T10:27:00Z",
                        },
                    }
                ]
                r2 = bot.poll_once([canvas], state, now=t2, state_path=state_path)
                self.assertEqual(r2["new_count"], 1)
                self.assertEqual(r2["reminder_count"], 1)
                self.assertEqual(r2["sent_count"], 2)
                self.assertEqual(len(sent_messages), 2)  # 1 grouped digest!
                self.assertIn("Lab 5 Submission", sent_messages[1]["text"])
                self.assertIn("Algorithms Live Lecture", sent_messages[1]["text"])
                self.assertEqual(sent_messages[1]["reply_markup"], bot.QUICK_INLINE_KEYBOARD)

                # 3. Third run: completely silent (alert once)
                t3 = datetime(2026, 11, 21, 10, 20, tzinfo=timezone.utc)
                r3 = bot.poll_once([canvas], state, now=t3, state_path=state_path)
                self.assertEqual(r3["sent_count"], 0)
                self.assertEqual(len(sent_messages), 2)

            # Verify atomic state file persisted cleanly
            reloaded = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("AUPP:asn:2", reloaded["seen"])
            self.assertEqual(len(reloaded["upcoming"]), 1)

    def test_quiet_hours_defers_and_delivers_afterwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            state = bot.load_state(state_path)
            state["initialized"] = ["AUPP", "AUPP:ann", "AUPP:asn", "AUPP:mod"]
            bot.CFG["quiet_hours"] = bot.parse_quiet_hours("23:00-07:00")

            canvas = FakeCanvas(
                announcements=[
                    {
                        "id": 500,
                        "context_code": "course_101",
                        "title": "Late Night Announcement",
                        "message": "<p>Class moved online tomorrow</p>",
                        "user_name": "Dr. Smith",
                        "html_url": "https://aupp.instructure.com/courses/101/discussion_topics/500",
                    }
                ]
            )
            sent = []
            with patch.object(bot, "tg_send", side_effect=lambda text, **kw: sent.append(text) or True):
                # 02:00 UTC -> inside quiet hours
                r_quiet = bot.poll_once(
                    [canvas], state, now=datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc), state_path=state_path
                )
                self.assertTrue(r_quiet["suppressed_by_quiet_hours"])
                self.assertEqual(r_quiet["sent_count"], 0)
                self.assertEqual(len(sent), 0)

                # 07:15 UTC -> outside quiet hours; deferred alert is now delivered!
                r_morning = bot.poll_once(
                    [canvas], state, now=datetime(2026, 11, 22, 7, 15, tzinfo=timezone.utc), state_path=state_path
                )
                self.assertFalse(r_morning["suppressed_by_quiet_hours"])
                self.assertEqual(r_morning["sent_count"], 1)
                self.assertEqual(len(sent), 1)
                self.assertIn("Late Night Announcement", sent[0])

    def test_command_resolution_and_inline_callbacks(self):
        self.assertEqual(bot.resolve_command_action("/check"), ("check", []))
        self.assertEqual(bot.resolve_command_action("/upcoming@mybot 14"), ("upcoming", ["14"]))
        self.assertEqual(bot.resolve_command_action("🔄 Check Now"), ("check", []))
        self.assertEqual(bot.resolve_command_action("📋 Upcoming"), ("upcoming", []))
        self.assertEqual(bot.resolve_command_action("📚 Courses"), ("courses", []))
        self.assertEqual(bot.resolve_command_action("📊 Status"), ("status", []))
        self.assertEqual(bot.resolve_command_action("❓ Help"), ("help", []))
        self.assertEqual(bot.resolve_command_action("/chatid"), ("chatid", []))
        self.assertEqual(bot.resolve_command_action("hello"), (None, []))

        state = bot.load_state(Path("nonexistent_state_for_test.json"))
        canvas = FakeCanvas()
        sent = []
        answered = []

        with patch.object(bot, "tg_send", side_effect=lambda text, **kw: sent.append((text, kw)) or True), \
             patch.object(bot, "tg_answer_callback", side_effect=lambda cid, text=None: answered.append((cid, text)) or True):
            # /start returns persistent reply keyboard
            bot.process_update(
                {"update_id": 1, "message": {"text": "/start", "chat": {"id": 999}}},
                [canvas],
                state,
            )
            self.assertEqual(sent[-1][1]["reply_markup"], bot.MAIN_REPLY_KEYBOARD)

            # Inline button action:courses answers callback & sends course link with inline keyboard
            bot.process_update(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "cb-1",
                        "data": "action:courses",
                        "message": {"chat": {"id": 999}},
                    },
                },
                [canvas],
                state,
            )
            self.assertEqual(answered[-1], ("cb-1", None))
            self.assertIn("CS101 - Algorithms", sent[-1][0])
            self.assertEqual(sent[-1][1]["reply_markup"], bot.QUICK_INLINE_KEYBOARD)

            # Unauthorized chat callback is rejected
            bot.process_update(
                {
                    "update_id": 3,
                    "callback_query": {
                        "id": "cb-2",
                        "data": "action:status",
                        "message": {"chat": {"id": 111}},
                    },
                },
                [canvas],
                state,
            )
            self.assertEqual(answered[-1], ("cb-2", "Unauthorized chat"))

    def test_format_upcoming_shows_assignments_and_events(self):
        now = datetime(2026, 11, 21, 10, 0, tzinfo=timezone.utc)
        upcoming = [
            {
                "id": "AUPP:assignment:1",
                "category": "assignment",
                "title": "Project Proposal",
                "course": "SE301",
                "due_at": (now + timedelta(hours=24)).isoformat(),
                "url": "https://aupp.instructure.com/courses/101/assignments/1",
            },
            {
                "id": "AUPP:event:2",
                "category": "event",
                "title": "Lab Session",
                "course": "SE301",
                "starts_at": (now + timedelta(hours=3)).isoformat(),
                "url": "https://aupp.instructure.com/calendar?event_id=2",
            },
        ]
        text = bot.format_upcoming_message(upcoming, days=7, tz=timezone.utc, now=now)
        self.assertIn("Project Proposal", text)
        self.assertIn("Lab Session", text)
        self.assertIn("https://aupp.instructure.com/courses/101/assignments/1", text)

    def test_assignment_edit_deferred_during_quiet_hours_and_legacy_reminder_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            now = datetime(2026, 11, 22, 2, 0, tzinfo=timezone.utc)
            due_iso = "2026-11-22T20:00:00Z"  # 18 hours away -> hits 1440m (24h) tier
            state = bot.load_state(state_path)
            state["initialized"] = ["AUPP", "AUPP:ann", "AUPP:asn", "AUPP:mod"]
            state["seen"]["AUPP:asn:10"] = now.isoformat()
            state["seen"][f"AUPP:due:10:{due_iso}"] = now.isoformat()  # legacy 24h reminder already sent
            state["fp"]["AUPP:asn:10"] = {
                "t": now.isoformat(),
                "v": {
                    "name": "Essay 1",
                    "due_at": "2026-11-22T18:00:00Z",
                    "points_possible": 50,
                    "unlock_at": None,
                    "lock_at": None,
                    "desc": bot.digest("<p>Original</p>"),
                },
            }

            canvas = FakeCanvas(
                assignments={
                    101: [
                        {
                            "id": 10,
                            "name": "Essay 1",
                            "due_at": due_iso,  # edited due date!
                            "points_possible": 50,
                            "description": "<p>Original</p>",
                            "html_url": "https://aupp.instructure.com/courses/101/assignments/10",
                        }
                    ]
                },
                planner=[
                    {
                        "plannable_type": "assignment",
                        "context_name": "CS101 - Algorithms",
                        "html_url": "/courses/101/assignments/10",
                        "plannable": {"id": 10, "title": "Essay 1", "due_at": due_iso},
                    }
                ],
            )

            bot.CFG["quiet_hours"] = bot.parse_quiet_hours("23:00-07:00")
            sent = []
            with patch.object(bot, "tg_send", side_effect=lambda text, **kw: sent.append(text) or True):
                # 02:00 UTC -> quiet hours defers the assignment edit; legacy 24h reminder is NOT duplicated
                r1 = bot.poll_once([canvas], state, now=now, state_path=state_path)
                self.assertTrue(r1["suppressed_by_quiet_hours"])
                self.assertEqual(r1["reminder_count"], 0)
                self.assertEqual(r1["new_count"], 1)
                self.assertEqual(len(sent), 0)

                # 07:15 UTC -> morning poll delivers the deferred assignment edit diff!
                r2 = bot.poll_once(
                    [canvas], state, now=datetime(2026, 11, 22, 7, 15, tzinfo=timezone.utc), state_path=state_path
                )
                self.assertEqual(r2["sent_count"], 1)
                self.assertEqual(len(sent), 1)
                self.assertIn("Due date:", sent[0])


if __name__ == "__main__":
    unittest.main()
