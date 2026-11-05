# -*- coding: utf-8 -*-
"""Сценарные тесты без обращения к Telegram: команды, кнопки, планировщик."""

import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot

TZ = "Europe/Moscow"
CHAT_ID = 42
USER_ID = 100


class FakeBot:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.markups = []
        self.answers = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.messages.append({"chat_id": chat_id, "text": text, "keyboard": reply_markup})
        return {"message_id": len(self.messages)}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append({"chat_id": chat_id, "message_id": message_id,
                           "text": text, "keyboard": reply_markup})
        return True

    def edit_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markups.append({"chat_id": chat_id, "message_id": message_id,
                             "keyboard": reply_markup})
        return True

    def answer_callback_query(self, callback_id, text=None, show_alert=False):
        self.answers.append(text)
        return True


def make_message(text):
    return {
        "message_id": 1,
        "from": {"id": USER_ID, "is_bot": False, "first_name": "Тест"},
        "chat": {"id": CHAT_ID, "type": "private"},
        "text": text,
    }


def make_callback(data, text="📋 <b>Мои напоминания</b>"):
    return {
        "id": "cb1",
        "from": {"id": USER_ID, "is_bot": False},
        "message": {"message_id": 10, "chat": {"id": CHAT_ID}, "text": text},
        "data": data,
    }


class FlowTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = Path(__file__).resolve().parent / "_tmp_db"
        tmp_dir.mkdir(exist_ok=True)
        self.db_path = tmp_dir / f"flow_{uuid.uuid4().hex}.db"
        self.db = bot.Database(self.db_path)
        self.bot = FakeBot()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.db_path) + suffix).unlink()
            except FileNotFoundError:
                pass

    def test_start(self):
        bot.handle_message(self.bot, self.db, make_message("/start"))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("бот-напоминалка", self.bot.messages[0]["text"])
        self.assertIsNotNone(self.bot.messages[0]["keyboard"])

    def test_create_via_free_text(self):
        bot.handle_message(self.bot, self.db, make_message("через 10 минут позвонить"))
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("Напоминание создано", self.bot.messages[0]["text"])
        self.assertEqual(len(self.db.list_active(CHAT_ID)), 1)

    def test_create_error(self):
        bot.handle_message(self.bot, self.db, make_message("просто текст"))
        self.assertIn("Не понял, когда напомнить", self.bot.messages[0]["text"])
        self.assertEqual(self.db.list_active(CHAT_ID), [])

    def test_remind_command(self):
        bot.handle_message(self.bot, self.db, make_message("/remind завтра в 9:00 созвон"))
        self.assertIn("Напоминание создано", self.bot.messages[0]["text"])
        self.assertEqual(self.db.list_active(CHAT_ID)[0].text, "созвон")

    def test_list_and_cancel_by_number(self):
        bot.handle_message(self.bot, self.db, make_message("через 10 минут раз"))
        bot.handle_message(self.bot, self.db, make_message("через 20 минут два"))
        self.bot.messages.clear()
        bot.handle_message(self.bot, self.db, make_message("/list"))
        self.assertIn("Мои напоминания", self.bot.messages[0]["text"])
        bot.handle_message(self.bot, self.db, make_message("/cancel 1"))
        self.assertIn("Удалено", self.bot.messages[-1]["text"])
        self.assertEqual(len(self.db.list_active(CHAT_ID)), 1)

    def test_callback_delete_updates_list(self):
        bot.handle_message(self.bot, self.db, make_message("через 10 минут раз"))
        reminder = self.db.list_active(CHAT_ID)[0]
        bot.handle_callback(self.bot, self.db, make_callback(f"del:{reminder.id}"))
        self.assertEqual(self.db.list_active(CHAT_ID), [])
        self.assertEqual(len(self.bot.edits), 1)
        self.assertIn("Активных напоминаний нет", self.bot.edits[0]["text"])

    def test_timezone_callback(self):
        bot.handle_callback(self.bot, self.db, make_callback("tz:Europe/Berlin"))
        self.assertEqual(self.db.get_tz(CHAT_ID), "Europe/Berlin")
        self.assertEqual(self.bot.answers, ["Часовой пояс обновлён"])

    def test_timezone_command(self):
        bot.handle_message(self.bot, self.db, make_message("/timezone Берлин"))
        self.assertEqual(self.db.get_tz(CHAT_ID), "Europe/Berlin")
        self.assertIn("Europe/Berlin", self.bot.messages[0]["text"])

    def test_callback_new(self):
        bot.handle_callback(self.bot, self.db, make_callback("new", text="в меню"))
        self.assertIn("Напишите одним сообщением", self.bot.messages[0]["text"])

    def test_scheduler_one_time(self):
        spec = bot.parse_reminder("через 1 минуту тест", TZ)
        reminder_id = self.db.add(CHAT_ID, USER_ID, spec)
        reminder = self.db.get(reminder_id)
        scheduler = bot.ReminderScheduler(self.bot, self.db)
        scheduler.fire(reminder, reminder.due_utc)
        self.assertEqual(len(self.bot.messages), 1)
        self.assertIn("тест", self.bot.messages[0]["text"])
        self.assertIn("Отложить", str(self.bot.messages[0]["keyboard"]))
        self.assertEqual(self.db.get(reminder_id).status, "done")

    def test_scheduler_recurring_reschedules(self):
        spec = bot.parse_reminder("каждый день в 8:00 зарядка", TZ)
        reminder_id = self.db.add(CHAT_ID, USER_ID, spec)
        reminder = self.db.get(reminder_id)
        scheduler = bot.ReminderScheduler(self.bot, self.db)
        scheduler.fire(reminder, reminder.due_utc)
        updated = self.db.get(reminder_id)
        self.assertEqual(updated.status, "active")
        self.assertEqual(updated.due_utc - reminder.due_utc, timedelta(days=1))
        self.assertIn("каждый день", self.bot.messages[0]["text"])

    def test_snooze_after_fire(self):
        spec = bot.parse_reminder("через 1 минуту тест", TZ)
        reminder_id = self.db.add(CHAT_ID, USER_ID, spec)
        self.db.mark_done(reminder_id)
        bot.handle_callback(self.bot, self.db, make_callback(f"snz:{reminder_id}:10"))
        self.assertEqual(self.db.get(reminder_id).status, "active")
        self.assertIn("Отложено", self.bot.answers[0])

    def test_ok_removes_keyboard(self):
        bot.handle_callback(self.bot, self.db, make_callback("ok:5", text="⏰ Напоминание"))
        self.assertEqual(self.bot.markups[-1]["keyboard"], None)

    def test_help(self):
        bot.handle_message(self.bot, self.db, make_message("/help"))
        self.assertIn("Как пользоваться", self.bot.messages[0]["text"])


if __name__ == "__main__":
    unittest.main()
