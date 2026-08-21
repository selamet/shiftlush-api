"""Turkish text handling.

One function, in one place, because the loader and the search have to agree
character for character. If they ever diverge, half the address table becomes
unreachable and nothing raises.
"""

from __future__ import annotations

# The dotted and dotless I are the whole problem. Python's str.lower() maps
# "İ" to "i" followed by a combining dot above — two code points — which then
# fails to match the plain "i" stored in the database, and fails silently.
# Substituting the letters first and lowercasing afterwards avoids it entirely.
TR_TRANSLATION = str.maketrans("İIıŞşĞğÜüÖöÇç", "IIiSsGgUuOoCc")


def normalize(text: str) -> str:
    """Fold Turkish text to a form that compares reliably."""
    return text.translate(TR_TRANSLATION).lower().strip()
