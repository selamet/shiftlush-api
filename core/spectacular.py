"""Schema generation hooks.

The API is configured for Turkish because invitation e-mails and the QR label
template are Turkish. Django activates that language for management commands
too, so the generated contract picked up DRF's own translated help strings for
paging and lookup parameters and carried them into the frontend's generated
client as documentation.

The contract is language-neutral by definition: it names fields and error codes,
never sentences meant for a user. So generation runs in English and puts the
language back afterwards.
"""

from __future__ import annotations

from typing import Any

from django.utils import translation

_previous: list[str | None] = []


def use_english(endpoints: Any, **kwargs: Any) -> Any:
    _previous.append(translation.get_language())
    translation.activate("en-us")
    return endpoints


def restore_language(result: Any, **kwargs: Any) -> Any:
    # There is no LocaleMiddleware here, so an activation would otherwise stick
    # to the thread and outlive the request that served /schema/.
    if _previous:
        translation.activate(_previous.pop() or "tr")
    return result
