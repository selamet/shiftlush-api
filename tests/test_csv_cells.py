"""Spreadsheet formula injection: a typed text cell never runs as a formula."""

from __future__ import annotations

import pytest

from core.csv_cells import safe_cell


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('=HYPERLINK("http://x")', '\'=HYPERLINK("http://x")'),
        ("+90 555", "'+90 555"),
        ("-1+1", "'-1+1"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("\tTab", "'\tTab"),
        ("\rReturn", "'\rReturn"),
        ("Yılmaz Asansör", "Yılmaz Asansör"),
        ("a=b", "a=b"),
        ("", ""),
    ],
)
def test_a_cell_that_starts_like_a_formula_is_quoted(raw: str, expected: str) -> None:
    assert safe_cell(raw) == expected
