"""Deterministic checks for Fred's respectful Isa reminder routine."""

import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


BOT_DIR = Path(__file__).resolve().parents[1] / "bot"
sys.path.insert(0, str(BOT_DIR))
os.environ.setdefault("GEMINI_API_KEY", "test-key")
import app  # noqa: E402


class IsaReminderTests(unittest.TestCase):
    @patch.object(app, "send_isa_pending_notification", return_value=True)
    @patch.object(app, "claim_daily_isa_reminder", return_value=True)
    @patch.object(app, "isa_reminders_snoozed", return_value=False)
    @patch.object(app, "pending_reminder_snapshot")
    @patch.object(app, "claim_requested_isa_reminder", return_value=False)
    @patch.object(app, "ISA_REMINDERS_ENABLED", True)
    def test_gentle_reminder_after_25_minutes(
        self, requested, snapshot, snoozed, claim, send
    ):
        now = datetime(2026, 8, 10, 11, 0, tzinfo=app.ARGENTINA_TZ)
        snapshot.return_value = {"count": 2, "oldest_created_at": now - timedelta(minutes=30)}
        app.run_isa_reminder_check(now)
        claim.assert_called_once_with(app.ISA_WHATSAPP_NUMBER, "gentle", now.date())
        send.assert_called_once_with(2)

    @patch.object(app, "send_isa_pending_notification")
    @patch.object(app, "claim_daily_isa_reminder")
    @patch.object(app, "isa_reminders_snoozed")
    @patch.object(app, "pending_reminder_snapshot")
    @patch.object(app, "claim_requested_isa_reminder", return_value=False)
    @patch.object(app, "ISA_REMINDERS_ENABLED", True)
    def test_no_automatic_reminder_at_night(
        self, requested, snapshot, snoozed, claim, send
    ):
        now = datetime(2026, 8, 10, 22, 0, tzinfo=app.ARGENTINA_TZ)
        snapshot.return_value = {"count": 1, "oldest_created_at": now - timedelta(hours=3)}
        app.run_isa_reminder_check(now)
        claim.assert_not_called()
        send.assert_not_called()

    @patch.object(app, "send_whatsapp_text", return_value=True)
    @patch.object(app, "snooze_isa_reminders")
    def test_isa_can_ask_for_reminder_in_one_hour(self, snooze, send):
        handled = app._handle_isa_reminder_request("recordame en 1 hora")
        self.assertTrue(handled)
        self.assertEqual(snooze.call_args.args[0], app.ISA_WHATSAPP_NUMBER)
        self.assertIn("te lo recuerdo", send.call_args.args[1])
