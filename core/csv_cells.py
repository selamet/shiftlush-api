"""Text cells that a spreadsheet will never run as a formula.

Excel, LibreOffice and Google Sheets read a cell that starts with `=`, `+`,
`-` or `@` (or a tab or carriage return, which some of them skip first) as a
formula, so a customer named `=HYPERLINK("http://…")` becomes a live link in
the accountant's export. The OWASP remedy: a leading `'`, which the
spreadsheet shows as text and does not print.

Only for free text. A number the exporter formats itself, such as "-12,60",
must stay a number, so it never passes through here.
"""

from __future__ import annotations

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value: str) -> str:
    """`value`, with a leading `'` when a spreadsheet would read it as a formula."""
    return f"'{value}" if value.startswith(_FORMULA_STARTS) else value
