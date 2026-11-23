"""Data Center: whole-day date ranges and CSV output."""

import csv
import io
from datetime import date

import pytest
from fastapi import HTTPException

from app.api.v1.lecturer.data_center import MAX_ROWS, day_bounds, to_csv


def test_end_date_is_inclusive_whole_days_in_utc():
    lo, hi = day_bounds(date(2026, 9, 1), date(2026, 9, 30))
    assert lo == "2026-09-01T00:00:00+00:00"
    assert hi == "2026-10-01T00:00:00+00:00"  # filtered with <, so all of 30 Sept is in


def test_open_ranges_and_a_backwards_range():
    assert day_bounds(None, None) == (None, None)
    assert day_bounds(date(2026, 9, 1), None)[1] is None
    with pytest.raises(HTTPException):
        day_bounds(date(2026, 9, 30), date(2026, 9, 1))


def test_csv_quotes_text_and_blanks_missing_values():
    out = to_csv(["email", "answer"], [["a@ug.edu.gh", 'He said "x, y"'], ["b@ug.edu.gh", None]])
    rows = list(csv.reader(io.StringIO(out)))
    assert rows == [["email", "answer"], ["a@ug.edu.gh", 'He said "x, y"'], ["b@ug.edu.gh", ""]]


def test_a_cut_download_says_so():
    out = to_csv(["n"], [[i] for i in range(MAX_ROWS)])
    assert out.rstrip().endswith("narrow the date range for the rest)")


def test_formula_cells_are_neutralised_but_numbers_are_not():
    from app.api.v1.lecturer.data_center import safe_cell

    for bad in ("=HYPERLINK(\"x\")", "+1+1", "-2x", "@SUM(A1)", "\tx", "\rx"):
        assert safe_cell(bad) == "'" + bad
    assert safe_cell("x = 2") == "x = 2"
    assert safe_cell(-3) == -3 and safe_cell(None) == ""


def test_soft_deleted_students_are_left_out():
    from app.api.v1.lecturer.data_center import _live

    people = {"a": {"deleted_at": None}, "b": {"deleted_at": "2026-10-01T00:00:00Z"}}
    rows = [{"student_id": "a"}, {"student_id": "b"}, {"student_id": "unknown"}]
    assert _live(rows, "student_id", people) == [{"student_id": "a"}, {"student_id": "unknown"}]
