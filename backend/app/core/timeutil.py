from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import jdatetime

TZ = ZoneInfo("Asia/Tehran")


@dataclass(frozen=True)
class JalaliDate:
    year: int
    month: int
    day: int

    def iso(self) -> str:
        return f"{self.year:04d}/{self.month:02d}/{self.day:02d}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_tehran(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def today_tehran(now: datetime | None = None) -> date:
    return now_tehran(now).date()


def parse_jalali(value: str) -> JalaliDate:
    text = value.strip().replace("-", "/")
    parts = text.split("/")
    if len(parts) != 3:
        raise ValueError("تاریخ شمسی نامعتبر است")
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        jdatetime.date(year, month, day)
    except Exception as exc:
        raise ValueError("تاریخ شمسی نامعتبر است") from exc
    return JalaliDate(year, month, day)


def jalali_to_gregorian(value: JalaliDate) -> date:
    return jdatetime.date(value.year, value.month, value.day).togregorian()


def gregorian_to_jalali(value: date) -> JalaliDate:
    converted = jdatetime.date.fromgregorian(date=value)
    return JalaliDate(converted.year, converted.month, converted.day)


def parse_jalali_to_date(value: str) -> date:
    return jalali_to_gregorian(parse_jalali(value))


def end_of_day_ts(value: date) -> int:
    moment = datetime.combine(value, time(23, 59, 59), tzinfo=TZ)
    return int(moment.timestamp())


def ts_to_tehran_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), TZ).date()


def format_jalali_date(value: date) -> str:
    return gregorian_to_jalali(value).iso()


def format_jalali_ts(ts: int) -> str:
    moment = datetime.fromtimestamp(int(ts), TZ).replace(tzinfo=None)
    converted = jdatetime.datetime.fromgregorian(datetime=moment)
    return (
        f"{converted.year:04d}/{converted.month:02d}/{converted.day:02d} "
        f"{converted.hour:02d}:{converted.minute:02d}"
    )


def format_datetime_jalali(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return format_jalali_ts(int(moment.timestamp()))


def need_days(end: date, base: date) -> int:
    return max(0, (end - base).days)


def days_left(expired_at: int, now_ts: int | None = None) -> int:
    if now_ts is None:
        now_ts = int(now_tehran().timestamp())
    return max(0, (int(expired_at) - int(now_ts)) // 86400)


def extend_base_date(expired_at: int | None, now: datetime | None = None) -> date:
    current = now_tehran(now)
    today = current.date()
    if expired_at is None or int(expired_at) <= int(current.timestamp()):
        return today
    return ts_to_tehran_date(int(expired_at))


def add_days(value: date, days: int) -> date:
    return value + timedelta(days=days)
