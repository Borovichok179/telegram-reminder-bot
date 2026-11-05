#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Телеграм-бот «Напоминалка».

Умеет:
  • создавать разовые напоминания («через 30 минут…», «завтра в 9:00…»);
  • создавать повторяющиеся («каждый день в 8:00…», «по будням в 9:30…»,
    «каждый понедельник в 19:00…»);
  • хранить напоминания в SQLite и присылать их, пока бот запущен;
  • работать с часовыми поясами (по умолчанию Europe/Moscow).

Внешних зависимостей нет — только стандартная библиотека Python 3.9+.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --------------------------------------------------------------------------- #
#  Константы
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "reminders.db"
TOKEN_FILE = BASE_DIR / "token.txt"
LOG_FILE = BASE_DIR / "bot.log"

DEFAULT_TZ = "Europe/Moscow"
MAX_REMINDER_LENGTH = 500
MAX_LIST_ITEMS = 20

WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WEEKDAY_EVERY = [
    "каждый понедельник",
    "каждый вторник",
    "каждую среду",
    "каждый четверг",
    "каждую пятницу",
    "каждую субботу",
    "каждое воскресенье",
]

# Названия городов → идентификаторы IANA. Нужны, чтобы можно было писать
# «/timezone Москва», а не только «/timezone Europe/Moscow».
TIMEZONE_ALIASES = {
    "москва": "Europe/Moscow",
    "мск": "Europe/Moscow",
    "питер": "Europe/Moscow",
    "санкт-петербург": "Europe/Moscow",
    "калининград": "Europe/Kaliningrad",
    "самара": "Europe/Samara",
    "екатеринбург": "Asia/Yekaterinburg",
    "омск": "Asia/Omsk",
    "новосибирск": "Asia/Novosibirsk",
    "красноярск": "Asia/Krasnoyarsk",
    "иркутск": "Asia/Irkutsk",
    "якутск": "Asia/Yakutsk",
    "владивосток": "Asia/Vladivostok",
    "магадан": "Asia/Magadan",
    "камчатка": "Asia/Kamchatka",
    "киев": "Europe/Kyiv",
    "минск": "Europe/Minsk",
    "кишинев": "Europe/Chisinau",
    "тбилиси": "Asia/Tbilisi",
    "ереван": "Asia/Yerevan",
    "баку": "Asia/Baku",
    "алматы": "Asia/Almaty",
    "астана": "Asia/Almaty",
    "ташкент": "Asia/Tashkent",
    "бишкек": "Asia/Bishkek",
    "душанбе": "Asia/Dushanbe",
    "ашхабад": "Asia/Ashgabat",
    "вильнюс": "Europe/Vilnius",
    "рига": "Europe/Riga",
    "таллин": "Europe/Tallinn",
    "варшава": "Europe/Warsaw",
    "берлин": "Europe/Berlin",
    "прага": "Europe/Prague",
    "лондон": "Europe/London",
    "париж": "Europe/Paris",
    "рим": "Europe/Rome",
    "мадрид": "Europe/Madrid",
    "лиссабон": "Europe/Lisbon",
    "амстердам": "Europe/Amsterdam",
    "брюссель": "Europe/Brussels",
    "вена": "Europe/Vienna",
    "цюрих": "Europe/Zurich",
    "стамбул": "Europe/Istanbul",
    "анталья": "Europe/Istanbul",
    "дубай": "Asia/Dubai",
    "тель-авив": "Asia/Jerusalem",
    "дели": "Asia/Kolkata",
    "бангкок": "Asia/Bangkok",
    "пхукет": "Asia/Bangkok",
    "пекин": "Asia/Shanghai",
    "шанхай": "Asia/Shanghai",
    "токио": "Asia/Tokyo",
    "сеул": "Asia/Seoul",
    "нью-йорк": "America/New_York",
    "лос-анджелес": "America/Los_Angeles",
    "сан-франциско": "America/Los_Angeles",
    "чикаго": "America/Chicago",
    "майами": "America/New_York",
    "торонто": "America/Toronto",
    "мехико": "America/Mexico_City",
    "сан-паулу": "America/Sao_Paulo",
    "буэнос-айрес": "America/Argentina/Buenos_Aires",
    "сидней": "Australia/Sydney",
    "мельбурн": "Australia/Melbourne",
    "олкленд": "Pacific/Auckland",
}

POPULAR_TIMEZONES = [
    ("Москва", "Europe/Moscow"),
    ("Киев", "Europe/Kyiv"),
    ("Минск", "Europe/Minsk"),
    ("Алматы", "Asia/Almaty"),
    ("Тбилиси", "Asia/Tbilisi"),
    ("Ереван", "Asia/Yerevan"),
    ("Лондон", "Europe/London"),
    ("Берлин", "Europe/Berlin"),
    ("Нью-Йорк", "America/New_York"),
    ("Дубай", "Asia/Dubai"),
    ("Стамбул", "Europe/Istanbul"),
    ("Токио", "Asia/Tokyo"),
]

LOG = logging.getLogger("reminder_bot")

# --------------------------------------------------------------------------- #
#  Разбор текста напоминания
# --------------------------------------------------------------------------- #

# «напомни», «напомни мне», «не забудь» в начале сообщения — просто выбрасываем.
PREFIX_RE = re.compile(
    r"^\s*(?:напомни(?:ть)?|напоминание|не\s+забудь|не\s+забыть)(?:\s+мне)?\b[\s,.:;—–\-]*",
    re.IGNORECASE,
)

REL_START_RE = re.compile(r"(?<!\w)через\s+", re.IGNORECASE)

REC_WEEKLY_K_RE = re.compile(
    r"(?<!\w)(?:каждый|каждую|каждое)\s+"
    r"(?P<wd>понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)(?!\w)",
    re.IGNORECASE,
)
REC_WEEKLY_PO_RE = re.compile(
    r"(?<!\w)по\s+"
    r"(?P<wd>понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)(?!\w)",
    re.IGNORECASE,
)
REC_WEEKDAYS_RE = re.compile(
    r"(?<!\w)(?:по\s+будням|по\s+будним|каждый\s+будний\s+день|по\s+рабочим\s+дням)(?!\w)",
    re.IGNORECASE,
)
REC_MORNING_RE = re.compile(r"(?<!\w)каждое\s+утро(?!\w)", re.IGNORECASE)
REC_EVENING_RE = re.compile(r"(?<!\w)(?:каждый\s+вечер|по\s+вечерам)(?!\w)", re.IGNORECASE)
REC_DAILY_RE = re.compile(
    r"(?<!\w)(?:каждый\s+день|каждодневно|ежедневно|ежедневный)(?!\w)", re.IGNORECASE
)

DATE_REL_RE = re.compile(r"(?<!\w)(?P<rel>послезавтра|завтра|сегодня)(?!\w)", re.IGNORECASE)
DATE_DOT_RE = re.compile(
    r"(?<![\d.])(?P<d>[0-3]?\d)\.(?P<mo>[01]?\d)(?:\.(?P<y>\d{4}|\d{2}))?(?![\d.])"
)
DATE_MONTH_RE = re.compile(
    r"(?<!\d)(?P<d>[0-3]?\d)\s+"
    r"(?P<mon>январ[яье]?|феврал[яье]?|март[ае]?|апрел[яье]?|ма[йя]|июн[яье]?|"
    r"июл[яье]?|август[ае]?|сентябр[яье]?|октябр[яье]?|ноябр[яье]?|декабр[яье]?)"
    r"(?:\s+(?P<y>\d{4}))?(?!\w)",
    re.IGNORECASE,
)
DATE_WEEKDAY_RE = re.compile(
    r"(?<!\w)(?:в|во)\s+"
    r"(?P<wd>понедельник|вторник|среду|четверг|пятницу|субботу|воскресенье)(?!\w)",
    re.IGNORECASE,
)

# Время: «9:30», «09:30», «в 9:30», «в 9 часов 30 минут», «в 9 вечера».
TIME_HM_RE = re.compile(
    r"(?<![\d:.])(?:(?P<pre>в|во)\s+)?(?P<h>[0-2]?\d):(?P<m>[0-5]\d)"
    r"(?:\s*(?P<ap>утра|дня|вечера|ночи))?(?!\d)",
    re.IGNORECASE,
)
TIME_HOURS_RE = re.compile(
    r"(?<![\d:.])(?:(?P<pre>в|во)\s+)?(?P<h>[0-2]?\d)\s*(?:час(?:ов|а|ик)?|ч)"
    r"(?:\s*(?P<m>[0-5]?\d)\s*мин(?:ут\w*)?)?"
    r"(?:\s*(?P<ap>утра|дня|вечера|ночи))?(?!\w)",
    re.IGNORECASE,
)
TIME_AMPM_RE = re.compile(
    r"(?<![\d:.])(?:(?P<pre>в|во)\s+)?(?P<h>[0-2]?\d)\s*"
    r"(?P<ap>утра|дня|вечера|ночи)(?!\w)",
    re.IGNORECASE,
)

MONTH_PREFIXES = [
    ("янв", 1),
    ("фев", 2),
    ("мар", 3),
    ("апр", 4),
    ("ма", 5),
    ("июн", 6),
    ("июл", 7),
    ("авг", 8),
    ("сен", 9),
    ("окт", 10),
    ("ноя", 11),
    ("дек", 12),
]

WEEKDAY_PREFIXES = [
    ("пон", 0),
    ("втор", 1),
    ("сред", 2),
    ("чет", 3),
    ("пят", 4),
    ("суб", 5),
    ("вос", 6),
]

NO_TIME_HINT = (
    "🤔 Не понял, когда напомнить.\n\n"
    "Укажите время одним сообщением, например:\n"
    "• <code>через 30 минут выпить чай</code>\n"
    "• <code>завтра в 9:00 совещание</code>\n"
    "• <code>25.12 в 10:00 купить подарки</code>\n"
    "• <code>каждый день в 8:00 зарядка</code>\n"
    "• <code>по будням в 9:30 планёрка</code>"
)


class ReminderParseError(ValueError):
    """Понятная пользователю ошибка разбора текста."""


@dataclass
class ReminderSpec:
    text: str
    due_utc: datetime
    repeat: str = "none"
    weekday: Optional[int] = None


@dataclass
class Recurrence:
    rule: str  # daily | weekdays | weekly
    weekday: Optional[int] = None
    default_time: Optional[tuple] = None


@dataclass
class DateInfo:
    kind: str  # rel | dot | month | weekday
    d: Optional[int] = None
    mo: Optional[int] = None
    y: Optional[int] = None
    year_given: bool = False
    rel_days: int = 0
    weekday: Optional[int] = None


def _find(pattern: re.Pattern, text: str, spans: list) -> Optional[re.Match]:
    """Первый матч, не пересекающийся с уже разобранными фрагментами."""
    for match in pattern.finditer(text):
        start, end = match.span()
        if any(start < s_end and end > s_start for s_start, s_end in spans):
            continue
        return match
    return None


def _weekday_from_word(word: str) -> Optional[int]:
    word = word.lower()
    for prefix, index in WEEKDAY_PREFIXES:
        if word.startswith(prefix):
            return index
    return None


def _month_from_word(word: str) -> Optional[int]:
    word = word.lower()
    for prefix, number in MONTH_PREFIXES:
        if word.startswith(prefix):
            return number
    return None


def _unit_delta(amount: float, unit: str) -> timedelta:
    unit = unit.lower()
    if unit.startswith("сек"):
        return timedelta(seconds=amount)
    if unit.startswith("мин"):
        return timedelta(minutes=amount)
    if unit.startswith("час") or unit == "ч":
        return timedelta(hours=amount)
    if unit.startswith(("день", "дня", "дней", "сутк")):
        return timedelta(days=amount)
    if unit.startswith("недел"):
        return timedelta(weeks=amount)
    if unit.startswith("месяц"):
        return timedelta(days=30 * amount)  # приблизительно
    return timedelta(days=365 * amount)  # «год»/«лет»


def _consume_relative(rest: str) -> Optional[tuple]:
    """Разбирает «10 минут», «1 час 30 минут», «полчаса», «1,5 часа» и т. п."""
    match = re.match(r"пол\s*часа\b", rest)
    if match:
        return timedelta(minutes=30), match.end()
    match = re.match(r"полтора\s+часа\b", rest)
    if match:
        return timedelta(minutes=90), match.end()
    match = re.match(r"час\b(?!\s*\d)", rest)
    if match:
        return timedelta(hours=1), match.end()

    total = timedelta(0)
    pos = 0
    found = False
    while True:
        gap = re.match(r"[\s,]*(?:и\s+)?", rest[pos:])
        pos += gap.end() if gap else 0
        token = re.match(
            r"(\d+(?:[.,]\d+)?)\s*(?:"
            r"секунд\w*|сек|минут\w*|мин|час\w*|ч|день|дня|дней|сутк\w*|"
            r"недел\w*|месяц\w*|год\w*|лет)\b",
            rest[pos:],
        )
        if not token:
            break
        number = float(token.group(1).replace(",", "."))
        unit_match = re.match(r"[\d.,]+\s*(.+)", token.group(0))
        unit = unit_match.group(1) if unit_match else ""
        total += _unit_delta(number, unit)
        pos += token.end()
        found = True
        if pos > 100:
            break
    return (total, pos) if found else None


def _find_recurrence(low: str, spans: list) -> Optional[Recurrence]:
    match = _find(REC_WEEKLY_K_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("weekly", weekday=_weekday_from_word(match.group("wd")))
    match = _find(REC_WEEKLY_PO_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("weekly", weekday=_weekday_from_word(match.group("wd")))
    match = _find(REC_WEEKDAYS_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("weekdays")
    match = _find(REC_MORNING_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("daily", default_time=(9, 0))
    match = _find(REC_EVENING_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("daily", default_time=(19, 0))
    match = _find(REC_DAILY_RE, low, spans)
    if match:
        spans.append(match.span())
        return Recurrence("daily")
    return None


def _find_date(low: str, spans: list) -> Optional[DateInfo]:
    match = _find(DATE_REL_RE, low, spans)
    if match:
        spans.append(match.span())
        word = match.group("rel").lower()
        days = {"сегодня": 0, "завтра": 1, "послезавтра": 2}[word]
        return DateInfo("rel", rel_days=days)

    match = _find(DATE_DOT_RE, low, spans)
    if match:
        day = int(match.group("d"))
        month = int(match.group("mo"))
        year_raw = match.group("y")
        year = None
        if year_raw:
            year = int(year_raw)
            if len(year_raw) == 2:
                year += 2000
        spans.append(match.span())
        return DateInfo("dot", d=day, mo=month, y=year, year_given=year is not None)

    match = _find(DATE_MONTH_RE, low, spans)
    if match:
        day = int(match.group("d"))
        month = _month_from_word(match.group("mon"))
        year_raw = match.group("y")
        year = int(year_raw) if year_raw else None
        spans.append(match.span())
        return DateInfo("month", d=day, mo=month, y=year, year_given=year is not None)

    match = _find(DATE_WEEKDAY_RE, low, spans)
    if match:
        spans.append(match.span())
        return DateInfo("weekday", weekday=_weekday_from_word(match.group("wd")))
    return None


def _apply_ampm(hour: int, ampm: Optional[str]) -> int:
    if not ampm:
        return hour
    ampm = ampm.lower()
    if ampm.startswith("утра"):
        return 0 if hour == 12 else hour
    if ampm.startswith("дня"):
        return hour + 12 if hour < 12 else hour
    if ampm.startswith("вечера"):
        return hour + 12 if hour < 12 else hour
    if ampm.startswith("ночи"):
        return 0 if hour == 12 else hour
    return hour


def _find_time(low: str, spans: list) -> Optional[tuple]:
    for pattern in (TIME_HM_RE, TIME_HOURS_RE, TIME_AMPM_RE):
        match = _find(pattern, low, spans)
        if not match:
            continue
        groups = match.groupdict()
        hour = int(groups["h"])
        minute = int(groups.get("m") or 0)
        hour = _apply_ampm(hour, groups.get("ap"))
        if hour > 23 or minute > 59:
            raise ReminderParseError("🕒 Такого времени не существует. Проверьте часы и минуты.")
        spans.append(match.span())
        return hour, minute
    return None


def _clean_text(original: str, spans: list) -> str:
    if not spans:
        return original.strip()
    chars = list(original)
    for start, end in spans:
        for index in range(max(0, start), min(len(chars), end)):
            chars[index] = " "
    result = "".join(chars)
    result = re.sub(r"\s+", " ", result).strip()
    result = result.strip(" \t\r\n,.;:!?—–-«»\"'()[]")
    result = re.sub(r"^(?:и|а)\s+", "", result, flags=re.IGNORECASE).strip()
    return re.sub(r"\s+", " ", result)


def _advance(local_dt: datetime, rule: str) -> datetime:
    if rule == "daily":
        return local_dt + timedelta(days=1)
    if rule == "weekdays":
        nxt = local_dt + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt
    return local_dt + timedelta(days=7)  # weekly


def next_occurrence(previous_local: datetime, rule: str, weekday: Optional[int] = None) -> datetime:
    """Следующее срабатывание повторяющегося напоминания (в его часовом поясе)."""
    if rule == "weekly" and weekday is not None:
        nxt = previous_local + timedelta(days=7)
        # На случай, если weekday не совпадает (например, пояс поменяли).
        shift = (weekday - nxt.weekday()) % 7
        return nxt + timedelta(days=shift)
    return _advance(previous_local, rule)


def _non_recurring_candidate(
    now: datetime, tz: ZoneInfo, date_info: Optional[DateInfo], hour: int, minute: int
) -> datetime:
    moment = dtime(hour, minute)
    if date_info is None:
        candidate = datetime.combine(now.date(), moment, tzinfo=tz)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if date_info.kind == "rel":
        base = now.date() + timedelta(days=date_info.rel_days)
        candidate = datetime.combine(base, moment, tzinfo=tz)
        if candidate <= now:
            if date_info.rel_days == 0:
                raise ReminderParseError(
                    "⏳ Это время уже прошло сегодня. Укажите будущее время."
                )
            candidate += timedelta(days=1)
        return candidate

    if date_info.kind in ("dot", "month"):
        year = date_info.y or now.year
        try:
            base = date(year, date_info.mo, date_info.d)
        except ValueError:
            raise ReminderParseError("📅 Такой даты не существует. Проверьте день и месяц.")

        if date_info.y is None and base < now.date():
            try:
                base = base.replace(year=now.year + 1)
            except ValueError:  # 29 февраля
                raise ReminderParseError("📅 Такой даты не существует в следующем году.")

        candidate = datetime.combine(base, moment, tzinfo=tz)
        if candidate <= now:
            if date_info.y is not None:
                raise ReminderParseError(
                    "⏳ Эта дата уже прошла. Укажите будущую дату и время."
                )
            raise ReminderParseError("⏳ Указанное время уже прошло. Укажите будущее время.")
        return candidate

    # Ближайший указанный день недели.
    delta = (date_info.weekday - now.weekday()) % 7
    base = now.date() + timedelta(days=delta)
    candidate = datetime.combine(base, moment, tzinfo=tz)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _recurring_candidate(
    now: datetime, tz: ZoneInfo, date_info: Optional[DateInfo], hour: int, minute: int,
    recurrence: Recurrence,
) -> datetime:
    moment = dtime(hour, minute)
    if date_info is not None and date_info.kind in ("dot", "month"):
        year = date_info.y or now.year
        try:
            base = date(year, date_info.mo, date_info.d)
        except ValueError:
            raise ReminderParseError("📅 Такой даты не существует. Проверьте день и месяц.")
    else:
        base = now.date()

    candidate = datetime.combine(base, moment, tzinfo=tz)

    if recurrence.rule == "weekly" and recurrence.weekday is not None:
        shift = (recurrence.weekday - candidate.weekday()) % 7
        candidate += timedelta(days=shift)

    guard = 0
    while candidate <= now:
        candidate = _advance(candidate, recurrence.rule)
        guard += 1
        if guard > 3000:
            break
    return candidate


def parse_reminder(raw_text: str, tz_name: str = DEFAULT_TZ, now: Optional[datetime] = None) -> ReminderSpec:
    """Разбирает сообщение пользователя в напоминание.

    :raises ReminderParseError: если не удалось понять время или текст.
    """
    if not raw_text or not raw_text.strip():
        raise ReminderParseError("Пустое сообщение. Напишите, когда и о чём напомнить.")

    raw_text = raw_text.strip()
    low = raw_text.lower().replace("ё", "е")
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)

    spans: list = []

    prefix = PREFIX_RE.match(low)
    if prefix:
        spans.append(prefix.span())

    recurrence = _find_recurrence(low, spans)

    relative = None
    rel_start = _find(REL_START_RE, low, spans)
    if rel_start:
        parsed = _consume_relative(low[rel_start.end():])
        if parsed:
            delta, length = parsed
            spans.append((rel_start.start(), rel_start.end() + length))
            relative = delta

    if relative is not None:
        if recurrence is not None:
            raise ReminderParseError(
                "🔁 Для повторяющихся напоминаний указывайте конкретное время, "
                "например: «каждый день в 9:00 зарядка»."
            )
        due_local = now + relative
        text = _clean_text(raw_text, spans)
        if not text:
            raise ReminderParseError(
                "А о чём напомнить? Добавьте текст после времени, например: "
                "«через 10 минут выпить чай»."
            )
        if len(text) > MAX_REMINDER_LENGTH:
            raise ReminderParseError(
                f"Слишком длинный текст напоминания (максимум {MAX_REMINDER_LENGTH} символов)."
            )
        return ReminderSpec(text, due_local.astimezone(timezone.utc))

    date_info = _find_date(low, spans)
    time_info = _find_time(low, spans)

    if time_info is not None:
        hour, minute = time_info
    elif recurrence is not None and recurrence.default_time is not None:
        hour, minute = recurrence.default_time
    elif recurrence is not None:
        raise ReminderParseError(
            "🕒 Для повторяющегося напоминания нужно время. "
            "Например: «каждый день в 8:00 зарядка»."
        )
    elif date_info is not None:
        hour, minute = 9, 0  # дата без времени — напомним в 9 утра
    else:
        raise ReminderParseError(NO_TIME_HINT)

    if recurrence is not None:
        due_local = _recurring_candidate(now, tz, date_info, hour, minute, recurrence)
        repeat = recurrence.rule
        weekday = recurrence.weekday
    else:
        due_local = _non_recurring_candidate(now, tz, date_info, hour, minute)
        repeat = "none"
        weekday = None

    text = _clean_text(raw_text, spans)
    if not text:
        raise ReminderParseError(
            "А о чём напомнить? Добавьте текст после времени, например: "
            "«завтра в 9:00 позвонить маме»."
        )
    if len(text) > MAX_REMINDER_LENGTH:
        raise ReminderParseError(
            f"Слишком длинный текст напоминания (максимум {MAX_REMINDER_LENGTH} символов)."
        )

    return ReminderSpec(
        text=text,
        due_utc=due_local.astimezone(timezone.utc),
        repeat=repeat,
        weekday=weekday,
    )


# --------------------------------------------------------------------------- #
#  Часовые пояса
# --------------------------------------------------------------------------- #

def resolve_timezone(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise ReminderParseError("Укажите часовой пояс, например: /timezone Europe/Berlin")
    key = raw.lower().replace("ё", "е")
    if key in TIMEZONE_ALIASES:
        return TIMEZONE_ALIASES[key]

    variants = [raw.replace(" ", "_")]
    variants.append("/".join(part.capitalize() for part in variants[0].split("/")))
    for candidate in variants:
        try:
            ZoneInfo(candidate)
            return candidate
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    raise ReminderParseError(
        "🌍 Не знаю такой часовой пояс.\n\n"
        "Напишите, например: <code>/timezone Europe/Berlin</code>\n"
        "Или город: <code>/timezone Берлин</code>"
    )


def ensure_timezone_database() -> None:
    try:
        ZoneInfo(DEFAULT_TZ)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(
            "Не найдена база часовых поясов (tzdata).\n"
            "Установите её командой:  python -m pip install tzdata\n"
            "и запустите бота снова."
        )


# --------------------------------------------------------------------------- #
#  База данных
# --------------------------------------------------------------------------- #

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id  INTEGER PRIMARY KEY,
    tz       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    due_utc     TEXT NOT NULL,
    tz          TEXT NOT NULL,
    repeat      TEXT NOT NULL DEFAULT 'none',
    weekday     INTEGER,
    created_utc TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders (status, due_utc);
CREATE INDEX IF NOT EXISTS idx_reminders_chat ON reminders (chat_id, status);
"""


@dataclass
class Reminder:
    id: int
    chat_id: int
    user_id: int
    text: str
    due_utc: datetime
    tz: str
    repeat: str
    weekday: Optional[int]
    status: str


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        chat_id=row["chat_id"],
        user_id=row["user_id"],
        text=row["text"],
        due_utc=datetime.fromisoformat(row["due_utc"]),
        tz=row["tz"],
        repeat=row["repeat"],
        weekday=row["weekday"],
        status=row["status"],
    )


class Database:
    """Очень простое хранилище на SQLite (по соединению на операцию)."""

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self._lock, self._connection() as conn:
            conn.executescript(SCHEMA_SQL)

    def get_tz(self, chat_id: int) -> str:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT tz FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()
            if row:
                return row["tz"]
            conn.execute(
                "INSERT OR IGNORE INTO chats (chat_id, tz) VALUES (?, ?)",
                (chat_id, DEFAULT_TZ),
            )
            return DEFAULT_TZ

    def set_tz(self, chat_id: int, tz_name: str) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                "INSERT INTO chats (chat_id, tz) VALUES (?, ?) "
                "ON CONFLICT(chat_id) DO UPDATE SET tz = excluded.tz",
                (chat_id, tz_name),
            )

    def add(self, chat_id: int, user_id: int, spec: ReminderSpec) -> int:
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        due = spec.due_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
        tz_name = self.get_tz(chat_id)
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO reminders "
                "(chat_id, user_id, text, due_utc, tz, repeat, weekday, created_utc, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    chat_id,
                    user_id,
                    spec.text,
                    due,
                    tz_name,
                    spec.repeat,
                    spec.weekday,
                    created,
                ),
            )
            return int(cursor.lastrowid)

    def list_active(self, chat_id: int) -> list:
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE chat_id = ? AND status = 'active' "
                "ORDER BY due_utc, id",
                (chat_id,),
            ).fetchall()
            return [_row_to_reminder(row) for row in rows]

    def get(self, reminder_id: int) -> Optional[Reminder]:
        with self._lock, self._connection() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
            return _row_to_reminder(row) if row else None

    def cancel(self, reminder_id: int, chat_id: int) -> bool:
        with self._lock, self._connection() as conn:
            cursor = conn.execute(
                "UPDATE reminders SET status = 'cancelled' "
                "WHERE id = ? AND chat_id = ? AND status != 'cancelled'",
                (reminder_id, chat_id),
            )
            return cursor.rowcount > 0

    def mark_done(self, reminder_id: int) -> None:
        with self._lock, self._connection() as conn:
            conn.execute("UPDATE reminders SET status = 'done' WHERE id = ?", (reminder_id,))

    def reschedule(self, reminder_id: int, due_utc: datetime) -> None:
        due = due_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connection() as conn:
            conn.execute(
                "UPDATE reminders SET due_utc = ?, status = 'active' WHERE id = ?",
                (due, reminder_id),
            )

    def snooze(self, reminder_id: int, chat_id: int, minutes: int) -> Optional[datetime]:
        reminder = self.get(reminder_id)
        if not reminder or reminder.chat_id != chat_id:
            return None
        if reminder.status == "cancelled":
            return None
        new_due = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        self.reschedule(reminder_id, new_due)
        return new_due

    def due(self, now_utc: datetime) -> list:
        moment = now_utc.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'active' AND due_utc <= ? "
                "ORDER BY due_utc, id LIMIT 50",
                (moment,),
            ).fetchall()
            return [_row_to_reminder(row) for row in rows]


# --------------------------------------------------------------------------- #
#  Telegram Bot API
# --------------------------------------------------------------------------- #

class TelegramError(RuntimeError):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


class FatalBotError(RuntimeError):
    """Ошибка, после которой дальше работать бессмысленно (токен, конфликт)."""


class Bot:
    def __init__(self, token: str):
        self.token = token

    def api(self, method: str, params: Optional[dict] = None, timeout: int = 60,
            attempts: int = 3):
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        body = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        last_error = "нет соединения"

        for attempt in range(attempts):
            request = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", "replace")
                try:
                    payload = json.loads(raw)
                except Exception:
                    payload = {}
                description = payload.get("description") or f"HTTP {exc.code}"
                if exc.code == 429:
                    retry_after = int((payload.get("parameters") or {}).get("retry_after", 3))
                    LOG.warning("Telegram просит подождать %s с", retry_after)
                    time.sleep(min(retry_after, 30))
                    continue
                if exc.code in (401, 404):
                    raise FatalBotError(
                        "Telegram не принял токен. Проверьте токен от @BotFather."
                    )
                if exc.code == 409:
                    raise FatalBotError(
                        "Конфликт getUpdates: похоже, этот бот уже запущен в другом месте."
                    )
                if exc.code >= 500:
                    last_error = description
                    time.sleep(2)
                    continue
                raise TelegramError(description, exc.code)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = str(exc)
                time.sleep(1.5 * (attempt + 1))
                continue

            if not payload.get("ok"):
                description = payload.get("description") or "неизвестная ошибка Telegram"
                raise TelegramError(description)
            return payload.get("result")

        raise TelegramError(f"Не удалось связаться с Telegram: {last_error}")

    # -- методы API ------------------------------------------------------- #

    def get_me(self) -> dict:
        return self.api("getMe", timeout=20)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 50) -> list:
        params = {
            "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            params["offset"] = offset
        return self.api("getUpdates", params, timeout=timeout + 20, attempts=1)

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None) -> dict:
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.api("sendMessage", params, timeout=30)

    def edit_message_text(self, chat_id: int, message_id: int, text: str,
                          reply_markup: Optional[dict] = None) -> dict:
        params = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            params["reply_markup"] = reply_markup
        return self.api("editMessageText", params, timeout=30)

    def edit_reply_markup(self, chat_id: int, message_id: int,
                          reply_markup: Optional[dict] = None) -> dict:
        return self.api(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup},
            timeout=30,
        )

    def answer_callback_query(self, callback_id: str, text: Optional[str] = None,
                             show_alert: bool = False) -> dict:
        params = {"callback_query_id": callback_id}
        if text:
            params["text"] = text
        if show_alert:
            params["show_alert"] = True
        return self.api("answerCallbackQuery", params, timeout=20)

    def set_my_commands(self, commands: list) -> dict:
        return self.api("setMyCommands", {"commands": commands}, timeout=20)


# --------------------------------------------------------------------------- #
#  Оформление сообщений
# --------------------------------------------------------------------------- #

def escape(text: str) -> str:
    return html.escape(text or "", quote=False)


def shorten(text: str, limit: int = 80) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def human_when(due_utc: datetime, tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    moment = due_utc.astimezone(tz)
    now = datetime.now(tz)
    days = (moment.date() - now.date()).days
    clock = moment.strftime("%H:%M")
    if days == 0:
        return f"сегодня, {clock}"
    if days == 1:
        return f"завтра, {clock}"
    if 2 <= days <= 6:
        return f"{WEEKDAYS_SHORT[moment.weekday()]}, {moment.strftime('%d.%m')}, {clock}"
    if moment.year == now.year:
        return f"{moment.strftime('%d.%m')}, {clock}"
    return f"{moment.strftime('%d.%m.%Y')}, {clock}"


def repeat_label(reminder: Reminder) -> str:
    if reminder.repeat == "daily":
        return "каждый день"
    if reminder.repeat == "weekdays":
        return "по будням"
    if reminder.repeat == "weekly" and reminder.weekday is not None:
        return WEEKDAY_EVERY[reminder.weekday]
    return "повторяется"


def new_reminder_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [{"text": "➕ Новое напоминание", "callback_data": "new"}],
            [
                {"text": "📋 Мои напоминания", "callback_data": "list"},
                {"text": "🌍 Часовой пояс", "callback_data": "tz"},
            ],
            [{"text": "❓ Помощь", "callback_data": "help"}],
        ]
    }


def build_list_view(db: Database, chat_id: int) -> tuple:
    items = db.list_active(chat_id)
    if not items:
        text = (
            "📭 <b>Активных напоминаний нет.</b>\n\n"
            "Напишите мне, когда и о чём напомнить, например:\n"
            "<code>через 30 минут выпить чай</code>"
        )
        return text, {
            "inline_keyboard": [[{"text": "➕ Новое напоминание", "callback_data": "new"}]]
        }

    lines = ["📋 <b>Мои напоминания</b>", ""]
    for index, reminder in enumerate(items[:MAX_LIST_ITEMS], start=1):
        line = (
            f"{index}. <b>{escape(human_when(reminder.due_utc, reminder.tz))}</b> — "
            f"{escape(shorten(reminder.text))}"
        )
        if reminder.repeat != "none":
            line += f"\n    🔁 {escape(repeat_label(reminder))}"
        lines.append(line)
    if len(items) > MAX_LIST_ITEMS:
        lines.append(f"\n…и ещё {len(items) - MAX_LIST_ITEMS}.")
    lines.append("\nУдалить: нажмите кнопку ниже или отправьте /cancel номер.")

    rows: list = []
    row: list = []
    for index, reminder in enumerate(items[:MAX_LIST_ITEMS], start=1):
        row.append({"text": f"🗑 {index}", "callback_data": f"del:{reminder.id}"})
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "➕ Новое напоминание", "callback_data": "new"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_created_text(spec: ReminderSpec, tz_name: str) -> str:
    text = (
        "✅ <b>Напоминание создано</b>\n\n"
        f"📝 {escape(spec.text)}\n"
        f"🕒 {escape(human_when(spec.due_utc, tz_name))}"
    )
    if spec.repeat == "daily":
        text += "\n🔁 каждый день"
    elif spec.repeat == "weekdays":
        text += "\n🔁 по будням"
    elif spec.repeat == "weekly" and spec.weekday is not None:
        text += f"\n🔁 {WEEKDAY_EVERY[spec.weekday]}"
    text += "\n\nЯ напишу вам в это время."
    return text


def timezone_keyboard() -> dict:
    rows: list = []
    row: list = []
    for title, zone in POPULAR_TIMEZONES:
        row.append({"text": title, "callback_data": f"tz:{zone}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def timezone_text(tz_name: str) -> str:
    return (
        "🌍 <b>Ваш часовой пояс</b>\n\n"
        f"Сейчас: <b>{escape(tz_name)}</b>\n\n"
        "Чтобы изменить, отправьте команду, например:\n"
        "<code>/timezone Europe/Berlin</code>\n"
        "или город: <code>/timezone Берлин</code>\n\n"
        "Можно выбрать из списка ниже."
    )


HELP_TEXT = (
    "❓ <b>Как пользоваться</b>\n\n"
    "Напишите мне одно сообщение: время + текст напоминания.\n\n"
    "<b>Примеры:</b>\n"
    "• <code>через 30 минут позвонить маме</code>\n"
    "• <code>через 2 часа выпить таблетку</code>\n"
    "• <code>в 18:30 забрать посылку</code>\n"
    "• <code>завтра в 9:00 совещание</code>\n"
    "• <code>25.12 в 10:00 купить подарки</code>\n"
    "• <code>5 декабря в 12:00 поздравить друга</code>\n"
    "• <code>каждый день в 8:00 зарядка</code>\n"
    "• <code>по будням в 9:30 планёрка</code>\n"
    "• <code>каждый понедельник в 19:00 тренировка</code>\n\n"
    "<b>Команды:</b>\n"
    "/list — мои напоминания\n"
    "/cancel 2 — удалить напоминание №2\n"
    "/timezone — часовой пояс\n"
    "/help — эта справка\n\n"
    "⚠️ Важно: бот присылает напоминания, только пока он запущен "
    "(компьютер включён и программа работает)."
)


# --------------------------------------------------------------------------- #
#  Обработка сообщений и кнопок
# --------------------------------------------------------------------------- #

def _create_reminder(bot: Bot, db: Database, chat_id: int, user_id: int, text: str) -> None:
    tz_name = db.get_tz(chat_id)
    try:
        spec = parse_reminder(text, tz_name)
    except ReminderParseError as exc:
        bot.send_message(chat_id, str(exc))
        return
    reminder_id = db.add(chat_id, user_id, spec)
    keyboard = {
        "inline_keyboard": [[{"text": "🗑 Отменить", "callback_data": f"del:{reminder_id}"}]]
    }
    bot.send_message(chat_id, build_created_text(spec, tz_name), keyboard)


def _handle_command(bot: Bot, db: Database, chat_id: int, user_id: int,
                    command: str, args: str) -> None:
    if command in ("start", "menu"):
        tz_name = db.get_tz(chat_id)
        bot.send_message(
            chat_id,
            f"👋 Привет!\n\n"
            f"Я бот-напоминалка. Напишите мне, когда и о чём напомнить — "
            f"я пришлю сообщение в нужный момент.\n\n"
            f"<b>Например:</b>\n"
            f"• <code>через 30 минут позвонить маме</code>\n"
            f"• <code>завтра в 9:00 совещание</code>\n"
            f"• <code>каждый день в 8:00 зарядка</code>\n\n"
            f"🌍 Ваш часовой пояс: <b>{escape(tz_name)}</b> "
            f"(изменить — /timezone)",
            new_reminder_keyboard(),
        )
        return

    if command == "help":
        bot.send_message(chat_id, HELP_TEXT, new_reminder_keyboard())
        return

    if command == "remind":
        if not args:
            bot.send_message(
                chat_id,
                "Напишите время и текст одним сообщением, например:\n"
                "<code>/remind через 10 минут выпить чай</code>",
            )
            return
        _create_reminder(bot, db, chat_id, user_id, args)
        return

    if command in ("list", "reminders"):
        text, keyboard = build_list_view(db, chat_id)
        bot.send_message(chat_id, text, keyboard)
        return

    if command in ("cancel", "delete", "del"):
        active = db.list_active(chat_id)
        if args:
            try:
                number = int(args.split()[0])
            except ValueError:
                bot.send_message(chat_id, "Укажите номер из списка, например: /cancel 2")
                return
            if not 1 <= number <= len(active):
                bot.send_message(
                    chat_id,
                    f"Нет напоминания №{number}. Всего активных: {len(active)}. "
                    "Список — /list.",
                )
                return
            reminder = active[number - 1]
            db.cancel(reminder.id, chat_id)
            bot.send_message(chat_id, f"🗑 Удалено: <b>{escape(reminder.text)}</b>")
            return
        text, keyboard = build_list_view(db, chat_id)
        bot.send_message(chat_id, text, keyboard)
        return

    if command in ("timezone", "tz"):
        if args:
            try:
                tz_name = resolve_timezone(args)
            except ReminderParseError as exc:
                bot.send_message(chat_id, str(exc))
                return
            db.set_tz(chat_id, tz_name)
            bot.send_message(
                chat_id,
                f"✅ Часовой пояс: <b>{escape(tz_name)}</b>\n\n"
                "Он будет использоваться для новых напоминаний. "
                "Старые напоминания не меняются.",
            )
            return
        bot.send_message(chat_id, timezone_text(db.get_tz(chat_id)), timezone_keyboard())
        return

    bot.send_message(
        chat_id,
        "Неизвестная команда. Список команд — /help",
        new_reminder_keyboard(),
    )


def _handle_free_text(bot: Bot, db: Database, chat_id: int, user_id: int, text: str) -> None:
    _create_reminder(bot, db, chat_id, user_id, text)


def handle_message(bot: Bot, db: Database, message: dict) -> None:
    if message.get("from", {}).get("is_bot"):
        return
    chat_id = message["chat"]["id"]
    user_id = message.get("from", {}).get("id", 0)
    text = (message.get("text") or "").strip()
    if not text:
        bot.send_message(chat_id, "Пока я понимаю только текстовые сообщения 🙂")
        return

    if text.startswith("/"):
        head, _, tail = text.partition(" ")
        command = head[1:].split("@", 1)[0].lower()
        _handle_command(bot, db, chat_id, user_id, command, tail.strip())
        return

    _handle_free_text(bot, db, chat_id, user_id, text)


def handle_callback(bot: Bot, db: Database, callback: dict) -> None:
    callback_id = callback.get("id")
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    message_text = message.get("text") or ""

    if chat_id is None:
        bot.answer_callback_query(callback_id, "Не понял, к какому чату относится кнопка.")
        return

    if data == "new":
        bot.answer_callback_query(callback_id)
        bot.send_message(
            chat_id,
            "Напишите одним сообщением время и текст напоминания.\n\n"
            "<b>Например:</b>\n"
            "• <code>через 10 минут выпить чай</code>\n"
            "• <code>в 18:30 забрать посылку</code>\n"
            "• <code>завтра в 9:00 созвон</code>\n"
            "• <code>каждый день в 8:00 зарядка</code>",
        )
        return

    if data == "list":
        bot.answer_callback_query(callback_id)
        text, keyboard = build_list_view(db, chat_id)
        bot.send_message(chat_id, text, keyboard)
        return

    if data == "help":
        bot.answer_callback_query(callback_id)
        bot.send_message(chat_id, HELP_TEXT, new_reminder_keyboard())
        return

    if data == "tz":
        bot.answer_callback_query(callback_id)
        bot.send_message(chat_id, timezone_text(db.get_tz(chat_id)), timezone_keyboard())
        return

    if data.startswith("tz:"):
        zone = data.split(":", 1)[1]
        try:
            tz_name = resolve_timezone(zone)
        except ReminderParseError as exc:
            bot.answer_callback_query(callback_id, "Не получилось 🙂", show_alert=True)
            bot.send_message(chat_id, str(exc))
            return
        db.set_tz(chat_id, tz_name)
        bot.answer_callback_query(callback_id, "Часовой пояс обновлён")
        if message_id:
            try:
                bot.edit_message_text(
                    chat_id,
                    message_id,
                    f"✅ Часовой пояс: <b>{escape(tz_name)}</b>\n\n"
                    "Он будет использоваться для новых напоминаний.",
                )
            except TelegramError:
                pass
        return

    if data.startswith("del:"):
        try:
            reminder_id = int(data.split(":", 1)[1])
        except ValueError:
            bot.answer_callback_query(callback_id, "Ошибка кнопки")
            return
        removed = db.cancel(reminder_id, chat_id)
        bot.answer_callback_query(callback_id, "🗑 Удалено" if removed else "Уже удалено")
        if message_text.startswith("📋") or message_text.startswith("📭"):
            new_text, new_keyboard = build_list_view(db, chat_id)
            try:
                bot.edit_message_text(chat_id, message_id, new_text, new_keyboard)
            except TelegramError:
                pass
        elif message_id:
            try:
                bot.edit_reply_markup(chat_id, message_id, None)
            except TelegramError:
                pass
        return

    if data.startswith("snz:"):
        parts = data.split(":")
        if len(parts) != 3:
            bot.answer_callback_query(callback_id, "Ошибка кнопки")
            return
        try:
            reminder_id = int(parts[1])
            minutes = int(parts[2])
        except ValueError:
            bot.answer_callback_query(callback_id, "Ошибка кнопки")
            return
        if db.snooze(reminder_id, chat_id, minutes) is not None:
            bot.answer_callback_query(callback_id, f"😴 Отложено на {minutes} мин")
            if message_id:
                try:
                    bot.edit_reply_markup(chat_id, message_id, None)
                except TelegramError:
                    pass
        else:
            bot.answer_callback_query(callback_id, "Напоминание уже недоступно")
        return

    if data.startswith("ok:"):
        bot.answer_callback_query(callback_id, "Отлично 👍")
        if message_id:
            try:
                bot.edit_reply_markup(chat_id, message_id, None)
            except TelegramError:
                pass
        return

    bot.answer_callback_query(callback_id)


def dispatch_update(bot: Bot, db: Database, update: dict) -> None:
    if "message" in update:
        handle_message(bot, db, update["message"])
    elif "callback_query" in update:
        handle_callback(bot, db, update["callback_query"])


# --------------------------------------------------------------------------- #
#  Планировщик напоминаний
# --------------------------------------------------------------------------- #

class ReminderScheduler(threading.Thread):
    def __init__(self, bot: Bot, db: Database):
        super().__init__(name="reminder-scheduler", daemon=True)
        self.bot = bot
        self.db = db
        self.stop_event = threading.Event()
        # id напоминания -> момент (monotonic), раньше которого не повторяем
        # отправку после временной сетевой ошибки.
        self._retry_after = {}

    def run(self) -> None:
        LOG.info("Планировщик запущен")
        while not self.stop_event.wait(1.0):
            try:
                self.tick()
            except Exception:
                LOG.exception("Ошибка в планировщике")

    def tick(self) -> None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        monotonic_now = time.monotonic()
        for reminder in self.db.due(now):
            if self._retry_after.get(reminder.id, 0) > monotonic_now:
                continue
            self.fire(reminder, now)

    def fire(self, reminder: Reminder, now_utc: datetime) -> None:
        late = now_utc - reminder.due_utc
        text = (
            "⏰ <b>Напоминание</b>\n\n"
            f"{escape(reminder.text)}\n\n"
            f"🕒 {escape(human_when(reminder.due_utc, reminder.tz))}"
        )
        if reminder.repeat != "none":
            text += f"\n🔁 {escape(repeat_label(reminder))}"
        if late > timedelta(minutes=10):
            text += "\n\n⚠️ <i>Доставлено с задержкой</i>"

        if reminder.repeat == "none":
            keyboard = {
                "inline_keyboard": [
                    [{"text": "😴 Отложить на 10 мин", "callback_data": f"snz:{reminder.id}:10"}],
                    [
                        {"text": "✅ Готово", "callback_data": f"ok:{reminder.id}"},
                        {"text": "🗑 Удалить", "callback_data": f"del:{reminder.id}"},
                    ],
                ]
            }
        else:
            keyboard = {
                "inline_keyboard": [
                    [
                        {"text": "✅ Готово", "callback_data": f"ok:{reminder.id}"},
                        {"text": "🗑 Удалить серию", "callback_data": f"del:{reminder.id}"},
                    ]
                ]
            }

        try:
            self.bot.send_message(reminder.chat_id, text, keyboard)
            self._retry_after.pop(reminder.id, None)
        except FatalBotError:
            raise
        except TelegramError as exc:
            LOG.warning("Не удалось отправить напоминание %s: %s", reminder.id, exc)
            # 400/403 — чат недоступен (бот заблокирован, чат удалён).
            # Повторять бессмысленно. Остальные ошибки — временные.
            if exc.code not in (400, 403):
                self._retry_after[reminder.id] = time.monotonic() + 60
                return
        except Exception:
            LOG.exception("Неожиданная ошибка при отправке напоминания %s", reminder.id)
            self._retry_after[reminder.id] = time.monotonic() + 60
            return

        if reminder.repeat == "none":
            self.db.mark_done(reminder.id)
            return

        tz = ZoneInfo(reminder.tz)
        local_due = reminder.due_utc.astimezone(tz)
        nxt = next_occurrence(local_due, reminder.repeat, reminder.weekday)
        now_local = now_utc.astimezone(tz)
        guard = 0
        while nxt <= now_local and guard < 3000:
            nxt = next_occurrence(nxt, reminder.repeat, reminder.weekday)
            guard += 1
        self.db.reschedule(reminder.id, nxt.astimezone(timezone.utc))


# --------------------------------------------------------------------------- #
#  Запуск
# --------------------------------------------------------------------------- #

def setup_logging() -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)
    try:
        file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError:
        pass


def resolve_token(args: argparse.Namespace) -> Optional[str]:
    token = (args.token or os.environ.get("BOT_TOKEN") or "").strip()
    if not token and TOKEN_FILE.exists():
        try:
            token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            token = ""
    if token:
        return token

    print(
        "\nТокен бота не найден.\n\n"
        "1. Откройте Telegram и напишите @BotFather\n"
        "2. Отправьте /newbot и придумайте имя\n"
        "3. Скопируйте токен вида 123456789:AAEi...\n"
        "4. Вставьте его ниже (или сохраните в файл token.txt рядом с bot.py)\n"
    )
    if sys.stdin is None or not sys.stdin.isatty():
        print("Запустите бота в консоли, чтобы ввести токен (или сохраните его в token.txt).")
        LOG.error("Токен не найден: запустите run.bat или сохраните токен в token.txt")
        return None
    try:
        token = input("Токен: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if token:
        try:
            TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
            print(f"Токен сохранён в {TOKEN_FILE}")
        except OSError:
            print("Не удалось сохранить токен в файл, но бот продолжит работу.")
    return token or None


def configure_commands(bot: Bot) -> None:
    commands = [
        {"command": "start", "description": "Запуск и меню"},
        {"command": "help", "description": "Как пользоваться"},
        {"command": "list", "description": "Мои напоминания"},
        {"command": "cancel", "description": "Удалить напоминание по номеру"},
        {"command": "timezone", "description": "Часовой пояс"},
    ]
    try:
        bot.set_my_commands(commands)
    except TelegramError as exc:
        LOG.warning("Не удалось обновить меню команд: %s", exc)


def run_polling(bot: Bot, db: Database) -> None:
    offset: Optional[int] = None
    try:
        pending = bot.get_updates(offset=-1, timeout=0)
        if pending:
            offset = pending[-1]["update_id"] + 1
    except (TelegramError, FatalBotError) as exc:
        LOG.warning("Не удалось получить последние обновления: %s", exc)

    LOG.info("Начинаю принимать сообщения")
    while True:
        try:
            updates = bot.get_updates(offset=offset, timeout=50)
        except FatalBotError:
            raise
        except TelegramError as exc:
            LOG.warning("Ошибка получения обновлений: %s", exc)
            time.sleep(3)
            continue
        except KeyboardInterrupt:
            raise
        except Exception:
            LOG.exception("Неожиданная ошибка получения обновлений")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            try:
                dispatch_update(bot, db, update)
            except FatalBotError:
                raise
            except Exception:
                LOG.exception("Ошибка обработки обновления %s", update.get("update_id"))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Телеграм-бот-напоминалка")
    parser.add_argument("--token", help="токен бота (иначе token.txt или BOT_TOKEN)")
    parser.add_argument("--db", help="путь к базе SQLite")
    args = parser.parse_args(argv)

    # Запуск через pythonw.exe: потоков вывода может не быть вовсе.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    setup_logging()
    ensure_timezone_database()

    token = resolve_token(args)
    if not token:
        print("Без токена бот не может работать. Запуск отменён.")
        return 1

    db = Database(args.db or DB_PATH)
    bot = Bot(token)

    try:
        me = bot.get_me()
    except FatalBotError as exc:
        LOG.error("%s", exc)
        print(f"\n{exc}\n")
        return 2
    except TelegramError as exc:
        LOG.error("Не удалось подключиться к Telegram: %s", exc)
        print("\nНе удалось подключиться к Telegram. Проверьте интернет и токен.\n")
        return 2

    username = me.get("username", "bot")
    LOG.info("Бот @%s запущен", username)
    print(f"\nБот @{username} запущен. Откройте Telegram и отправьте /start.")
    print("Остановить: Ctrl+C\n")
    configure_commands(bot)

    scheduler = ReminderScheduler(bot, db)
    scheduler.start()

    try:
        run_polling(bot, db)
    except KeyboardInterrupt:
        print("\nОстанавливаюсь…")
    except FatalBotError as exc:
        LOG.error("%s", exc)
        print(f"\n{exc}\n")
        return 3
    finally:
        scheduler.stop_event.set()
        scheduler.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
