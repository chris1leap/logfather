"""Chart label helpers."""
from datetime import date

from logfather.ui.charts import day_heading


def test_day_heading_ordinals():
    assert day_heading(date(2026, 5, 18)) == "Mon 18th"
    assert day_heading(date(2026, 9, 1)) == "Tue 1st"
    assert day_heading(date(2026, 9, 2)) == "Wed 2nd"
    assert day_heading(date(2026, 9, 3)) == "Thu 3rd"
    assert day_heading(date(2026, 9, 11)) == "Fri 11th"
    assert day_heading(date(2026, 9, 12)) == "Sat 12th"
    assert day_heading(date(2026, 9, 13)) == "Sun 13th"
    assert day_heading(date(2026, 9, 22)) == "Tue 22nd"
