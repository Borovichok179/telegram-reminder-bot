# -*- coding: utf-8 -*-
"""Тесты разбора напоминаний и хранилища. Запуск: python -m unittest discover -s tests"""

import sys
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot

TZ = "Europe/Moscow"
NOW = datetime(2026, 11, 5, 15, 0, tzinfo=ZoneInfo(TZ))  # четверг, 15:00


def local(spec):
    return spec.due_utc.astimezone(ZoneInfo(TZ))


class ParserTests(unittest.TestCase):
    def test_relative_minutes(self):
        spec = bot.parse_reminder("через 30 минут выпить чай", TZ, NOW)
        self.assertEqual(local(spec), NOW + timedelta(minutes=30))
        self.assertEqual(spec.text, "выпить чай")
        self.assertEqual(spec.repeat, "none")

    def test_relative_hours_with_prefix(self):
        spec = bot.parse_reminder("напомни мне через 2 часа позвонить маме", TZ, NOW)
        self.assertEqual(local(spec), NOW + timedelta(hours=2))
        self.assertEqual(spec.text, "позвонить маме")

    def test_relative_compound(self):
        spec = bot.parse_reminder("через 1 час 30 минут позвонить", TZ, NOW)
        self.assertEqual(local(spec), NOW + timedelta(minutes=90))
        self.assertEqual(spec.text, "позвонить")

    def test_relative_hour_word(self):
        spec = bot.parse_reminder("через час выпить таблетку", TZ, NOW)
        self.assertEqual(local(spec), NOW + timedelta(hours=1))
        self.assertEqual(spec.text, "выпить таблетку")

    def test_relative_decimal(self):
        spec = bot.parse_reminder("через 1,5 часа позвонить", TZ, NOW)
        self.assertEqual(local(spec), NOW + timedelta(minutes=90))
        self.assertEqual(spec.text, "позвонить")

    def test_tomorrow(self):
        spec = bot.parse_reminder("завтра в 9:00 совещание", TZ, NOW)
        expected = datetime(2026, 11, 6, 9, 0, tzinfo=ZoneInfo(TZ))
        self.assertEqual(local(spec), expected)
        self.assertEqual(spec.text, "совещание")

    def test_today_future_time(self):
        spec = bot.parse_reminder("в 18:30 забрать посылку", TZ, NOW)
        self.assertEqual(local(spec), datetime(2026, 11, 5, 18, 30, tzinfo=ZoneInfo(TZ)))
        self.assertEqual(spec.text, "забрать посылку")

    def test_past_time_moves_to_tomorrow(self):
        spec = bot.parse_reminder("в 9:00 зарядка", TZ, NOW)
        self.assertEqual(local(spec), datetime(2026, 11, 6, 9, 0, tzinfo=ZoneInfo(TZ)))

    def test_explicit_date(self):
        spec = bot.parse_reminder("25.12 в 10:00 купить подарки", TZ, NOW)
        self.assertEqual(local(spec), datetime(2026, 12, 25, 10, 0, tzinfo=ZoneInfo(TZ)))
        self.assertEqual(spec.text, "купить подарки")

    def test_explicit_date_with_year(self):
        spec = bot.parse_reminder("25.12.2027 в 10:00 купить подарки", TZ, NOW)
        self.assertEqual(local(spec), datetime(2027, 12, 25, 10, 0, tzinfo=ZoneInfo(TZ)))

    def test_month_name(self):
        spec = bot.parse_reminder("5 декабря в 12:00 поздравить друга", TZ, NOW)
        self.assertEqual(local(spec), datetime(2026, 12, 5, 12, 0, tzinfo=ZoneInfo(TZ)))
        self.assertEqual(spec.text, "поздравить друга")

    def test_past_date_moves_to_next_year(self):
        spec = bot.parse_reminder("1 января в 10:00 салют", TZ, NOW)
        self.assertEqual(local(spec), datetime(2027, 1, 1, 10, 0, tzinfo=ZoneInfo(TZ)))

    def test_daily(self):
        spec = bot.parse_reminder("каждый день в 8:00 зарядка", TZ, NOW)
        self.assertEqual(spec.repeat, "daily")
        self.assertEqual(local(spec), datetime(2026, 11, 6, 8, 0, tzinfo=ZoneInfo(TZ)))
        self.assertEqual(spec.text, "зарядка")

    def test_weekdays_skip_weekend(self):
        spec = bot.parse_reminder("по будням в 9:30 планёрка", TZ, NOW)
        self.assertEqual(spec.repeat, "weekdays")
        self.assertEqual(local(spec), datetime(2026, 11, 6, 9, 30, tzinfo=ZoneInfo(TZ)))

    def test_weekdays_first_is_monday(self):
        friday = datetime(2026, 11, 6, 18, 0, tzinfo=ZoneInfo(TZ))
        spec = bot.parse_reminder("по будням в 9:30 планёрка", TZ, friday)
        self.assertEqual(local(spec), datetime(2026, 11, 9, 9, 30, tzinfo=ZoneInfo(TZ)))

    def test_weekly_monday(self):
        spec = bot.parse_reminder("каждый понедельник в 19:00 тренировка", TZ, NOW)
        self.assertEqual(spec.repeat, "weekly")
        self.assertEqual(spec.weekday, 0)
        self.assertEqual(local(spec), datetime(2026, 11, 9, 19, 0, tzinfo=ZoneInfo(TZ)))

    def test_weekly_po_form(self):
        spec = bot.parse_reminder("по средам в 10:00 планёрка", TZ, NOW)
        self.assertEqual(spec.repeat, "weekly")
        self.assertEqual(spec.weekday, 2)
        self.assertEqual(local(spec), datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo(TZ)))

    def test_weekday_date(self):
        spec = bot.parse_reminder("в понедельник в 10:00 встреча", TZ, NOW)
        self.assertEqual(spec.repeat, "none")
        self.assertEqual(local(spec), datetime(2026, 11, 9, 10, 0, tzinfo=ZoneInfo(TZ)))

    def test_evening_default(self):
        spec = bot.parse_reminder("каждый вечер читать книгу", TZ, NOW)
        self.assertEqual(spec.repeat, "daily")
        self.assertEqual(local(spec), datetime(2026, 11, 5, 19, 0, tzinfo=ZoneInfo(TZ)))

    def test_ampm(self):
        spec = bot.parse_reminder("завтра в 9 вечера фильм", TZ, NOW)
        self.assertEqual(local(spec), datetime(2026, 11, 6, 21, 0, tzinfo=ZoneInfo(TZ)))
        self.assertEqual(spec.text, "фильм")

    def test_text_without_time(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.parse_reminder("позвонить маме", TZ, NOW)

    def test_empty_text(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.parse_reminder("через 5 минут", TZ, NOW)

    def test_invalid_date(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.parse_reminder("32.01 в 10:00 ерунда", TZ, NOW)

    def test_past_explicit_date(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.parse_reminder("01.01.2020 в 10:00 прошлое", TZ, NOW)

    def test_relative_with_repeat_is_error(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.parse_reminder("каждый день через 2 часа пить воду", TZ, NOW)

    def test_utc_conversion(self):
        spec = bot.parse_reminder("завтра в 9:00 совещание", TZ, NOW)
        self.assertEqual(spec.due_utc.utcoffset(), timedelta(0))
        self.assertEqual(spec.due_utc.hour, 6)  # 09:00 MSK = 06:00 UTC

    def test_other_timezone(self):
        spec = bot.parse_reminder("завтра в 9:00 совещание", "Europe/Berlin", NOW)
        self.assertEqual(local(spec), datetime(2026, 11, 6, 11, 0, tzinfo=ZoneInfo(TZ)))

    def test_timezone_aliases(self):
        self.assertEqual(bot.resolve_timezone("Москва"), "Europe/Moscow")
        self.assertEqual(bot.resolve_timezone("берлин"), "Europe/Berlin")
        self.assertEqual(bot.resolve_timezone("Europe/Berlin"), "Europe/Berlin")

    def test_timezone_unknown(self):
        with self.assertRaises(bot.ReminderParseError):
            bot.resolve_timezone("Атлантида")


class NextOccurrenceTests(unittest.TestCase):
    def test_daily(self):
        start = datetime(2026, 11, 6, 8, 0, tzinfo=ZoneInfo(TZ))
        self.assertEqual(
            bot.next_occurrence(start, "daily"),
            datetime(2026, 11, 7, 8, 0, tzinfo=ZoneInfo(TZ)),
        )

    def test_weekdays(self):
        friday = datetime(2026, 11, 6, 9, 30, tzinfo=ZoneInfo(TZ))
        self.assertEqual(
            bot.next_occurrence(friday, "weekdays"),
            datetime(2026, 11, 9, 9, 30, tzinfo=ZoneInfo(TZ)),
        )

    def test_weekly(self):
        monday = datetime(2026, 11, 9, 19, 0, tzinfo=ZoneInfo(TZ))
        self.assertEqual(
            bot.next_occurrence(monday, "weekly", 0),
            datetime(2026, 11, 16, 19, 0, tzinfo=ZoneInfo(TZ)),
        )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        tmp_dir = Path(__file__).resolve().parent / "_tmp_db"
        tmp_dir.mkdir(exist_ok=True)
        self.db_path = tmp_dir / f"test_{uuid.uuid4().hex}.db"
        self.db = bot.Database(self.db_path)
        self.spec = bot.parse_reminder("завтра в 9:00 совещание", TZ, NOW)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(self.db_path) + suffix).unlink()
            except FileNotFoundError:
                pass

    def test_default_timezone(self):
        self.assertEqual(self.db.get_tz(1), bot.DEFAULT_TZ)

    def test_set_timezone(self):
        self.db.set_tz(1, "Europe/Berlin")
        self.assertEqual(self.db.get_tz(1), "Europe/Berlin")

    def test_add_and_list(self):
        reminder_id = self.db.add(1, 100, self.spec)
        items = self.db.list_active(1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, reminder_id)
        self.assertEqual(items[0].text, "совещание")
        self.assertEqual(items[0].due_utc, self.spec.due_utc)
        self.assertEqual(items[0].tz, bot.DEFAULT_TZ)

    def test_cancel(self):
        reminder_id = self.db.add(1, 100, self.spec)
        self.assertTrue(self.db.cancel(reminder_id, 1))
        self.assertFalse(self.db.cancel(reminder_id, 1))
        self.assertEqual(self.db.list_active(1), [])

    def test_cancel_foreign_chat(self):
        reminder_id = self.db.add(1, 100, self.spec)
        self.assertFalse(self.db.cancel(reminder_id, 2))
        self.assertEqual(len(self.db.list_active(1)), 1)

    def test_due(self):
        self.db.add(1, 100, self.spec)
        self.assertEqual(len(self.db.due(NOW)), 0)
        self.assertEqual(len(self.db.due(NOW + timedelta(days=1))), 1)

    def test_snooze(self):
        reminder_id = self.db.add(1, 100, self.spec)
        before = datetime.now(bot.timezone.utc)
        new_due = self.db.snooze(reminder_id, 1, 10)
        self.assertIsNotNone(new_due)
        self.assertGreaterEqual(new_due, before + timedelta(minutes=9))
        self.assertEqual(len(self.db.list_active(1)), 1)

    def test_reschedule(self):
        reminder_id = self.db.add(1, 100, self.spec)
        new_due = NOW + timedelta(days=2)
        self.db.reschedule(reminder_id, new_due)
        self.assertEqual(self.db.list_active(1)[0].due_utc, new_due)


if __name__ == "__main__":
    unittest.main()
