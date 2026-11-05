"""A schedule's time in code's words, on its own clock (F1393, TRI1; Naveen 11-05).

A cron is read on its schedule's own clock (F948): the timezone the person named, else their own, which build_plan
and the change route fill; none is UTC, as every schedule before F948 is. What a card, a change line, the build's
summary and the schedules reader say about when a schedule runs is the cron read on that clock, in these words, never
the planner's own: `cron_words` gives the app's words (`workflowList.ts` `cronWords`, the Schedules screen's), and
`clash` finds a planner's words that name a time its cron does not run at.
"""
from __future__ import annotations

import re

from core.native_builder.render import zone_words

_DAYS = ["Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays", "Saturdays"]
_SHORT_DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def _ordinal(n: int) -> str:
    v = n % 100
    suffix = "th" if 10 < v < 14 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _day_list(dow: str) -> str | None:
    """A day-of-week field that is a list or a range of days ("1,3,5", "2-4"), as short names; None otherwise."""
    def day(s: str) -> str | None:
        return _SHORT_DAYS[int(s) % 7] if s.isdigit() and int(s) <= 7 else None

    if re.fullmatch(r"\d+-\d+", dow):
        a, b = (day(x) for x in dow.split("-"))
        return f"{a}–{b}" if a and b else None
    if re.fullmatch(r"\d+(,\d+)+", dow):
        names = [day(x) for x in dow.split(",")]
        return ", ".join(names) if all(names) else None  # type: ignore[arg-type]
    return None


def _utc_words(expr: str) -> str:
    """`workflowList.ts` `utcCronWords`, word for word: the words with " UTC" where a clock is said; anything it
    can't say comes back as the expression itself."""
    raw = (expr or "").strip()
    parts = raw.split()
    if len(parts) != 5:
        return raw
    minute, hour, dom, mon, dow = parts
    two = lambda s: str(s).zfill(2)  # noqa: E731 — the app's `two`
    if mon != "*":
        if not mon.isdigit() or not 1 <= int(mon) <= 12 or dom != "*" or dow != "*":
            return raw
        inner = _utc_words(f"{minute} {hour} * * *")
        return f"{_MONTHS[int(mon) - 1]}, daily {inner[6:]}" if inner.startswith("Daily ") else raw
    every_day = dom == "*" and dow == "*"
    if minute == "*" and hour == "*" and every_day:
        return "Every minute"
    if (step := re.fullmatch(r"\*/(\d+)", minute)) and hour == "*" and every_day:
        n = int(step[1])
        return "Every minute" if n == 1 else f"Every {n} minutes"
    if minute.isdigit() and hour == "*" and every_day:
        return f"Hourly at :{two(minute)}"
    if minute.isdigit() and (step := re.fullmatch(r"\*/(\d+)", hour)) and every_day:
        n = int(step[1])
        at = "" if int(minute) == 0 else f" at :{two(minute)}"
        return f"{'Every hour' if n == 1 else f'Every {n} hours'}{at}"
    if minute.isdigit() and (span := re.fullmatch(r"(\d+)-(\d+)", hour)) and dom == "*" and dow in ("*", "1-5"):
        words = f"{two(span[1])}:{two(minute)}–{two(span[2])}:{two(minute)} UTC"
        return f"Weekdays, hourly {words}" if dow == "1-5" else f"Hourly {words}"
    if not minute.isdigit() or not hour.isdigit():
        return raw
    time = f"{two(hour)}:{two(minute)} UTC"
    if every_day:
        return f"Daily {time}"
    if dom == "*":
        if dow == "1-5":
            return f"Weekdays {time}"
        if dow in ("0,6", "6,0", "6-7"):
            return f"Weekends {time}"
        if dow.isdigit() and int(dow) <= 7:
            return f"{_DAYS[int(dow) % 7]} {time}"
        days = _day_list(dow)
        return f"{days} {time}" if days else raw
    if dow == "*" and dom.isdigit():
        return f"Monthly, {_ordinal(int(dom))} {time}"
    return raw


def cron_words(expr: str, timezone: str | None = None, *, clock: bool = True) -> str:
    """A cron in a few words on its clock ("Daily 07:00 Lisbon time"), as the app says it; `clock=False` leaves the
    clock out ("Daily 07:00") for a line that says it beside the cron."""
    words = _utc_words(expr)
    zone = zone_words(timezone) if clock else ""
    return re.sub(r" UTC\b", f" {zone}" if zone else "", words) if zone != "UTC" else words


_HM = re.compile(r"(?i)\b(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)(?![a-z])|\b(\d{1,2})[:.](\d{2})\b")


def said_times(text: str) -> set[tuple[int, int]]:
    """The clock times `text` names, as (hour, minute): "7am", "4 PM", "9.30pm", "noon", "midnight", and "7:00",
    which without am or pm may be either. A time it doesn't spell out ("at 7", "every morning") names none."""
    out: set[tuple[int, int]] = set()
    words = text or ""
    for m in _HM.finditer(words):
        if m[3]:
            hour, minute = int(m[1]) % 12, int(m[2] or 0)
            if m[3].lower().startswith("p"):
                hour += 12
            if hour < 24 and minute < 60:
                out.add((hour, minute))
        else:
            hour, minute = int(m[4]), int(m[5])
            if hour < 24 and minute < 60:
                out.add((hour, minute))
                if 1 <= hour <= 11:
                    out.add((hour + 12, minute))
    if re.search(r"(?i)\bnoon\b|\bmidday\b", words):
        out.add((12, 0))
    if re.search(r"(?i)\bmidnight\b", words):
        out.add((0, 0))
    return out


def _cron_times(expr: str) -> set[tuple[int, int]] | None:
    """The times of day a cron runs at, when its minute and hour are numbers or lists of them; None otherwise."""
    parts = (expr or "").split()
    if len(parts) != 5 or not re.fullmatch(r"\d+(,\d+)*", parts[0]) or not re.fullmatch(r"\d+(,\d+)*", parts[1]):
        return None
    return {(int(h), int(m)) for h in parts[1].split(",") for m in parts[0].split(",")}


def clash(says: str, cron: str, timezone: str | None) -> str | None:
    """What to send back when `says` names a time `cron` doesn't run at on its clock; None when they agree, or either
    says no time."""
    said, runs = said_times(says), _cron_times(cron)
    if not said or not runs or said & runs:
        return None
    first = sorted(said)[0] if len(said) == 1 else min(said, key=lambda t: (t[0] % 12, t[1]))
    zone = zone_words(timezone)
    at = ", ".join(f"{h:02d}:{m:02d}" for h, m in sorted(runs))
    return (f"its words say {first[0] % 12 or 12}:{first[1]:02d}, but its cron `{cron.strip()}` runs at {at} "
            f"{zone}: write the cron on the schedule's own clock ({zone}), the time as they said it, never converted "
            "to UTC")
