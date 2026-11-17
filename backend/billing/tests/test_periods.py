from datetime import date

from billing.periods import Period, add_months, due_periods, period


def test_months_clamp_to_the_end_of_a_shorter_month() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2028, 1, 31), 1) == date(2028, 2, 29)
    assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)


def test_periods_are_anchored_at_the_start_day_and_do_not_drift() -> None:
    start = date(2026, 1, 31)
    assert period(start, "month", 0) == Period(date(2026, 1, 31), date(2026, 2, 27))
    assert period(start, "month", 1) == Period(date(2026, 2, 28), date(2026, 3, 30))
    assert period(start, "month", 2) == Period(date(2026, 3, 31), date(2026, 4, 29))


def test_yearly_periods() -> None:
    assert period(date(2026, 9, 17), "year", 1) == Period(date(2027, 9, 17), date(2028, 9, 16))


def test_due_periods_bill_in_advance_up_to_today() -> None:
    starts = date(2026, 7, 15)
    due = list(due_periods(starts, "month", today=date(2026, 9, 15), ends_on=None))
    assert [row.start for row in due] == [date(2026, 7, 15), date(2026, 8, 15), date(2026, 9, 15)]


def test_nothing_is_due_before_the_start() -> None:
    assert (
        list(due_periods(date(2026, 10, 1), "month", today=date(2026, 9, 30), ends_on=None)) == []
    )


def test_ends_on_stops_new_periods_without_proration() -> None:
    due = list(
        due_periods(date(2026, 1, 1), "month", today=date(2026, 12, 31), ends_on=date(2026, 3, 10))
    )
    assert [row.start for row in due] == [date(2026, 1, 1), date(2026, 2, 1), date(2026, 3, 1)]
    assert due[-1].end == date(2026, 3, 31)
