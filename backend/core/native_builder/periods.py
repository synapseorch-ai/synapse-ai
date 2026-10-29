"""A period the person did not pin down, asked once in code with its dates on their clock (F1210, BLD1; CAP3).

"Last week's sales" is two different weeks: the calendar week before (Monday to Sunday) or the seven days to
yesterday. A planner picked one without saying so, and a run on the other week's numbers looked right and was not.
So before a build is planned, code finds each relative period in the request (last / this / next week, month,
quarter or year; the past N days, weeks or months; "recent") that the person did not define in the same message, and
asks one question with concrete dates, worked out from today on the person's clock. Their answer is a rule a run
works out from its own date, never today's dates: it goes to the planner with the question, and the review says it.

`question(text, timezone)` is the question, or None; `meanings(asked, answer)` reads the answer back as rules.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

#: last / this / next week, month, quarter or year ("previous", "past" and "current" are the same words).
_RELATIVE = re.compile(r"\b(last|previous|past|this|current|next)\s+(week|month|quarter|year)\b", re.I)
#: the past N days, weeks or months.
_SPAN = re.compile(r"\b(?:last|past|previous)\s+(\d{1,3}|two|three|four|five|six|seven|ten|fourteen|thirty)\s+"
                   r"(days|weeks|months)\b", re.I)
_RECENT = re.compile(r"\brecent(?:ly)?\b", re.I)
_MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*"
#: Words that say which days a period is: a date, a range of weekdays, "calendar", "rolling", "to date"...
_DEFINED = re.compile(
    rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTHS}\b|\b{_MONTHS}\s+\d{{1,2}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b"
    r"|\b(?:mon|sun)(?:day)?\s*(?:-|–|—|to|through|until)\s*(?:sun|sat)(?:urday|day)?\b"
    r"|\bcalendar\b|\brolling\b|\bto date\b|\bfiscal\b|\bfinancial\b|\biso week\b", re.I)
_NUMBERS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10, "fourteen": 14, "thirty": 30}
_SAME = {"previous": "last", "past": "last", "current": "this"}


@dataclass
class Choice:
    #: The rule a run works out from its own date.
    rule: str
    #: What it is today, in dates.
    today: str


@dataclass
class Period:
    said: str
    choices: list[Choice]


def _now(timezone: str | None) -> date:
    from zoneinfo import ZoneInfo

    try:
        return datetime.now(ZoneInfo(timezone or "UTC")).date()
    except Exception:  # noqa: BLE001 — a name that is no timezone reads as UTC, as the schedule does
        return datetime.now(ZoneInfo("UTC")).date()


def _day(d: date, today: date) -> str:
    return f"{d:%a} {d.day} {d:%b}" + ("" if d.year == today.year else f" {d.year}")


def _span(a: date, b: date, today: date) -> str:
    return _day(a, today) if a == b else f"{_day(a, today)} – {_day(b, today)}"


def _months_back(d: date, n: int) -> date:
    """The first of the month n months before d's."""
    y, m = divmod(d.year * 12 + d.month - 1 - n, 12)
    return date(y, m + 1, 1)


def _shift(d: date, n: int) -> date:
    """The same day n months before d (after, for a negative n), on the month's last day where it has fewer."""
    from calendar import monthrange

    first = _months_back(d, n)
    return first.replace(day=min(d.day, monthrange(first.year, first.month)[1]))


def _choices(which: str, unit: str, today: date) -> list[Choice]:
    yesterday = today - timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    data = Choice(f"the latest full {unit} in the data itself", "whatever the data's newest full " + unit + " is")
    if unit == "week":
        if which == "last":
            return [Choice("the calendar week before the run's date, Monday to Sunday",
                           _span(monday - timedelta(days=7), monday - timedelta(days=1), today)),
                    Choice("the 7 days up to the day before the run",
                           _span(today - timedelta(days=7), yesterday, today)), data]
        if which == "this":
            return [Choice("the run's own calendar week so far, from its Monday to the run's date",
                           _span(monday, today, today)),
                    Choice("the 7 days up to the run's date", _span(today - timedelta(days=6), today, today))]
        return [Choice("the calendar week after the run's, Monday to Sunday",
                       _span(monday + timedelta(days=7), monday + timedelta(days=13), today)),
                Choice("the 7 days from the day after the run",
                       _span(today + timedelta(days=1), today + timedelta(days=7), today))]
    if unit == "month":
        first = today.replace(day=1)
        if which == "last":
            return [Choice("the calendar month before the run's",
                           _span(_months_back(today, 1), first - timedelta(days=1), today)),
                    Choice("the 30 days up to the day before the run",
                           _span(today - timedelta(days=30), yesterday, today)), data]
        if which == "this":
            return [Choice("the run's own calendar month so far, from its 1st to the run's date",
                           _span(first, today, today)),
                    Choice("the 30 days up to the run's date", _span(today - timedelta(days=29), today, today))]
        nxt = _months_back(today, -1)
        return [Choice("the calendar month after the run's", _span(nxt, _months_back(today, -2) - timedelta(days=1),
                                                                   today)),
                Choice("the 30 days from the day after the run",
                       _span(today + timedelta(days=1), today + timedelta(days=30), today))]
    if unit == "quarter":
        start = date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
        if which == "last":
            before = _months_back(start, 3)
            return [Choice("the calendar quarter before the run's (Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec)",
                           _span(before, start - timedelta(days=1), today)),
                    Choice("the 3 months up to the day before the run",
                           _span(_shift(today, 3), yesterday, today)), data]
        if which == "this":
            return [Choice("the run's own calendar quarter so far", _span(start, today, today)),
                    Choice("the 3 months up to the run's date",
                           _span(_shift(today, 3) + timedelta(days=1), today, today))]
        after = _months_back(start, -3)
        return [Choice("the calendar quarter after the run's", _span(after, _months_back(after, -3)
                                                                       - timedelta(days=1), today)),
                Choice("the 3 months from the day after the run",
                       _span(today + timedelta(days=1), _shift(today, -3), today))]
    # year
    if which == "last":
        return [Choice("the calendar year before the run's", f"1 Jan – 31 Dec {today.year - 1}"),
                Choice("the 12 months up to the day before the run",
                       _span(_shift(today, 12), yesterday, today)), data]
    if which == "this":
        return [Choice("the run's own calendar year so far", _span(date(today.year, 1, 1), today, today)),
                Choice("the 12 months up to the run's date",
                       _span(_shift(today, 12) + timedelta(days=1), today, today))]
    return [Choice("the calendar year after the run's", f"1 Jan – 31 Dec {today.year + 1}"),
            Choice("the 12 months from the day after the run",
                   _span(today + timedelta(days=1), _shift(today, -12), today))]


def _span_choices(n: int, unit: str, today: date) -> list[Choice]:
    yesterday = today - timedelta(days=1)
    if unit == "days":
        return [Choice(f"the {n} days up to and including the run's date",
                       _span(today - timedelta(days=n - 1), today, today)),
                Choice(f"the {n} full days before the run's date", _span(today - timedelta(days=n), yesterday, today))]
    if unit == "weeks":
        monday = today - timedelta(days=today.weekday())
        return [Choice(f"the {n} calendar weeks before the run's, Monday to Sunday",
                       _span(monday - timedelta(days=7 * n), monday - timedelta(days=1), today)),
                Choice(f"the {7 * n} days up to the day before the run",
                       _span(today - timedelta(days=7 * n), yesterday, today))]
    first = today.replace(day=1)
    return [Choice(f"the {n} calendar months before the run's",
                   _span(_months_back(today, n), first - timedelta(days=1), today)),
            Choice(f"the {n} months up to the day before the run",
                   _span(_shift(today, n), yesterday, today))]


def find(text: str, today: date) -> list[Period]:
    """Each relative period in `text` that the same message does not define, once each, in the order said."""
    text = str(text or "")
    if _DEFINED.search(text):
        return []
    found: dict[str, Period] = {}
    for m in _RELATIVE.finditer(text):
        which, unit = _SAME.get(m.group(1).lower(), m.group(1).lower()), m.group(2).lower()
        said = f"{which} {unit}"
        found.setdefault(said, Period(said, _choices(which, unit, today)))
    for m in _SPAN.finditer(text):
        raw, unit = m.group(1).lower(), m.group(2).lower()
        n = int(raw) if raw.isdigit() else _NUMBERS[raw]
        if n < 1:
            continue
        said = f"the past {n} {unit}"
        found.setdefault(said, Period(said, _span_choices(n, unit, today)))
    if _RECENT.search(text) and not found:
        found["recent"] = Period("recent", [
            Choice("the 7 days up to the run's date", _span(today - timedelta(days=6), today, today)),
            Choice("the 30 days up to the run's date", _span(today - timedelta(days=29), today, today)),
            Choice("everything since the workflow's last run", "since its last run (the first run: the last 7 days)")])
    return list(found.values())


@dataclass
class Question:
    text: str
    periods: list[Period]

    def dump(self) -> list[dict[str, Any]]:
        return [{"said": p.said, "choices": [{"rule": c.rule, "today": c.today} for c in p.choices]}
                for p in self.periods]


def question(text: str, timezone: str | None) -> Question | None:
    """The one question for every undefined period in `text`, its choices numbered across them, or None."""
    today = _now(timezone)
    periods = find(text, today)
    if not periods:
        return None
    lines, n = [], 0
    for p in periods:
        lines.append(f'When you say "{p.said}", which days do you mean?')
        for c in p.choices:
            n += 1
            lines.append(f"{n}. {c.rule[0].upper()}{c.rule[1:]}: today, {c.today}")
    if len(periods) > 1:
        lines.append("Answer with one number for each, or say it in your own words.")
    return Question("\n".join(lines), periods)


def meanings(asked: list[dict], answer: str, timezone: str | None) -> list[str]:
    """The person's answer as one rule per period: a number picks that choice; anything else is their own words."""
    said = str(answer or "").strip()
    picks = [int(x) for x in re.findall(r"\b\d{1,2}\b", said)]
    rules, n = [], 0
    clock = f", on the {timezone} clock" if timezone else ""
    for p in asked:
        choices = p.get("choices") or []
        numbers = range(n + 1, n + len(choices) + 1)
        n += len(choices)
        pick = next((k for k in picks if k in numbers), None)
        if pick is not None:
            c = choices[pick - numbers.start]
            rules.append(f'"{p["said"]}" means {c["rule"]}{clock}, worked out from each run\'s own date (today that '
                         f'is {c["today"]}); never write today\'s dates into a step')
        else:
            rules.append(f'"{p["said"]}" means what the person said: "{said}", worked out from each run\'s own '
                         "date; never write today's dates into a step")
    return rules
