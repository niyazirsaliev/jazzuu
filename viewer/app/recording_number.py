"""Read-side helpers for the permanent recording code (N-0042).

The archive writer allocates the code; the viewer only ever reads it. Two
things are worth having in one place. The viewer and the writer are deployed
independently, so the viewer will meet an archive whose schema predates the
column and must keep serving rather than 500 the whole feed over a cosmetic
field. And whatever reaches the client is checked against the code shape
first: this column is rendered into the page and offered to the clipboard, so
a value that is not a code is dropped rather than displayed.

Deliberately free of framework imports — it is exercised directly against
SQLite in the tests, on both schemas.
"""
import re

FIELD = "recording_number"

# The canonical shape, and nothing else. Normalisation is limited to trimming
# and case; a value that needs more than that is not a code.
_CODE_RE = re.compile(r"^[A-Za-z]-\d{4,6}$")


def select_expr(columns) -> str:
    """SQL for reading the code, tolerating an archive that has no such column."""
    return FIELD if FIELD in columns else f"NULL AS {FIELD}"


def public(value):
    """The code to show a client, or None if there isn't a well-formed one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _CODE_RE.match(code) else None
