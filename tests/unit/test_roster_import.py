"""Parsing a class list out of whatever the lecturer uploads.

The property that matters: one unusable row must never cost the other 199.
Every case below is a shape we have actually seen in an exported class list —
a title row above the header, "Surname, Firstname", an Excel BOM, a stray
"N/A" in the email column.
"""

import csv
import io

import pytest

from app.core.roster import (
    RosterFormatError,
    _split_full_name,
    parse_roster,
)


def csv_bytes(rows, encoding="utf-8"):
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode(encoding)


class TestHeaderDetection:
    def test_finds_a_plain_header(self):
        out = parse_roster(
            csv_bytes([
                ["First name", "Last name", "Email"],
                ["Ama", "Mensah", "ama@ug.edu.gh"],
            ]),
            "class.csv",
        )
        assert [r.email for r in out.rows] == ["ama@ug.edu.gh"]
        assert out.rows[0].first_name == "Ama"
        assert out.rows[0].last_name == "Mensah"

    def test_skips_a_title_row_above_the_header(self):
        """Exported class lists routinely carry a course title and a blank
        line before the real header."""
        out = parse_roster(
            csv_bytes([
                ["MATH 201 — Calculus II, Semester 1"],
                [],
                ["Name", "Email Address"],
                ["Kwame Nkrumah", "kwame@ug.edu.gh"],
            ]),
            "class.csv",
        )
        assert [r.email for r in out.rows] == ["kwame@ug.edu.gh"]
        assert (out.rows[0].first_name, out.rows[0].last_name) == ("Kwame", "Nkrumah")

    @pytest.mark.parametrize(
        "header", ["Email", "E-mail", "Email Address", "Student Email", "mail"]
    )
    def test_accepts_the_email_column_by_any_of_its_names(self, header):
        out = parse_roster(csv_bytes([[header], ["a@ug.edu.gh"]]), "c.csv")
        assert [r.email for r in out.rows] == ["a@ug.edu.gh"]

    def test_headerless_file_is_still_usable(self):
        """A two-column export with no header at all is common enough that
        rejecting it would send the lecturer away to edit the file."""
        out = parse_roster(
            csv_bytes([["Ama Mensah", "ama@ug.edu.gh"], ["Kofi Boateng", "kofi@ug.edu.gh"]]),
            "c.csv",
        )
        assert [r.email for r in out.rows] == ["ama@ug.edu.gh", "kofi@ug.edu.gh"]
        assert out.rows[0].first_name == "Ama"

    def test_no_email_column_is_a_clear_error(self):
        with pytest.raises(RosterFormatError, match="email"):
            parse_roster(csv_bytes([["Name", "Index number"], ["Ama", "10123"]]), "c.csv")

    def test_excel_utf8_bom_does_not_hide_the_header(self):
        """Excel's "CSV UTF-8" writes a BOM. Decoded as plain utf-8 it becomes
        part of the first cell and the email column stops being recognised."""
        content = "﻿Email,Name\nama@ug.edu.gh,Ama\n".encode("utf-8")
        out = parse_roster(content, "c.csv")
        assert [r.email for r in out.rows] == ["ama@ug.edu.gh"]

    def test_semicolon_delimited_file_is_read(self):
        content = b"Email;First name;Last name\nama@ug.edu.gh;Ama;Mensah\n"
        out = parse_roster(content, "c.csv")
        assert out.rows[0].last_name == "Mensah"


class TestRowsAreSkippedNotFatal:
    def test_one_bad_address_does_not_lose_the_file(self):
        out = parse_roster(
            csv_bytes([
                ["Email"],
                ["ama@ug.edu.gh"],
                ["N/A"],
                ["kofi@ug.edu.gh"],
            ]),
            "c.csv",
        )
        assert [r.email for r in out.rows] == ["ama@ug.edu.gh", "kofi@ug.edu.gh"]
        assert len(out.skipped) == 1
        assert "N/A" in out.skipped[0][1]

    def test_skipped_rows_report_the_spreadsheet_row_number(self):
        """The lecturer has to find the row in Excel. A zero-based index into
        our parsed list is useless for that."""
        out = parse_roster(
            csv_bytes([["Email"], ["ama@ug.edu.gh"], ["not-an-email"]]), "c.csv"
        )
        assert out.skipped[0][0] == 3

    def test_duplicate_addresses_are_reported_once_and_enrolled_once(self):
        out = parse_roster(
            csv_bytes([["Email"], ["ama@ug.edu.gh"], ["AMA@ug.edu.gh"]]), "c.csv"
        )
        assert len(out.rows) == 1
        assert "more than once" in out.skipped[0][1]

    def test_blank_rows_are_ignored_silently(self):
        """A trailing blank line is not something to report to the lecturer."""
        out = parse_roster(
            csv_bytes([["Email"], ["ama@ug.edu.gh"], [], ["", ""]]), "c.csv"
        )
        assert len(out.rows) == 1
        assert out.skipped == []


class TestNameSplitting:
    def test_two_part_name(self):
        assert _split_full_name("Ama Mensah") == ("Ama", "Mensah")

    def test_three_part_name_keeps_the_middle_with_the_surname(self):
        assert _split_full_name("Ama Serwaa Mensah") == ("Ama", "Serwaa Mensah")

    def test_comma_means_surname_first(self):
        """The only reliable signal of ordering in a roster."""
        assert _split_full_name("Mensah, Ama") == ("Ama", "Mensah")

    def test_single_word_is_a_first_name(self):
        assert _split_full_name("Ama") == ("Ama", None)

    def test_empty(self):
        assert _split_full_name("  ") == (None, None)


class TestFileTypes:
    def test_xlsx_is_parsed(self):
        openpyxl = pytest.importorskip("openpyxl")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["First name", "Last name", "Email"])
        sheet.append(["Ama", "Mensah", "ama@ug.edu.gh"])
        buf = io.BytesIO()
        workbook.save(buf)

        out = parse_roster(buf.getvalue(), "class.xlsx")
        assert [r.email for r in out.rows] == ["ama@ug.edu.gh"]

    def test_legacy_xls_is_refused_with_an_instruction(self):
        with pytest.raises(RosterFormatError, match="save"):
            parse_roster(b"\xd0\xcf\x11\xe0", "class.xls")

    def test_unknown_extension_is_refused(self):
        with pytest.raises(RosterFormatError, match="Unsupported"):
            parse_roster(b"whatever", "class.pdf")

    def test_empty_file(self):
        with pytest.raises(RosterFormatError):
            parse_roster(b"", "class.csv")
