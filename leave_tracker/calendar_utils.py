"""Working-day arithmetic: weekends and company holidays do not count as leave."""
from datetime import date, timedelta


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def is_working_day(d: date, holidays: set[date]) -> bool:
    return d.weekday() < 5 and d not in holidays


def working_days(start: date, end: date, holidays: set[date]) -> list[date]:
    return [d for d in daterange(start, end) if is_working_day(d, holidays)]


def add_working_days(start: date, count: int, holidays: set[date]) -> date:
    """Return the end date of a leave of `count` working days that begins on the first working day >= start."""
    d = start
    while not is_working_day(d, holidays):
        d += timedelta(days=1)
    taken = 1
    while taken < count:
        d += timedelta(days=1)
        if is_working_day(d, holidays):
            taken += 1
    return d


def overlaps(a_start: date, a_end: date, b_start: date, b_end: date) -> bool:
    return a_start <= b_end and b_start <= a_end
