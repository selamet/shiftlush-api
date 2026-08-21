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


def add_error_codes(result: Any, **kwargs: Any) -> Any:
    """Put the error-code vocabulary into the contract.

    Every code the API can return, generated from `core.error_codes` rather
    than typed into a list by hand. A hand-written list is the version that
    drifts: the code is added, the schema is not, and the frontend renders a
    blank message for an error nobody can reproduce.

    Together with the error envelope below this makes the codes part of the
    generated TypeScript, so a client can switch on them exhaustively.
    """
    from core.error_codes import ErrorCode

    components = result.setdefault("components", {}).setdefault("schemas", {})
    components["ErrorCode"] = {
        "type": "string",
        "enum": [code.value for code in ErrorCode],
        "description": "\n".join(f"* `{code.value}` - {code.label}" for code in ErrorCode),
    }
    components["ErrorResponse"] = {
        "type": "object",
        "description": (
            "The shape of every error response. There is no `message` field: "
            "the backend sends codes, and the words live in the client's "
            "translation file, so wording changes in one place and adding a "
            "language does not touch the API."
        ),
        "properties": {
            "error": {
                "type": "object",
                "properties": {
                    "code": {"$ref": "#/components/schemas/ErrorCode"},
                    "request_id": {"type": "string"},
                    "details": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "code": {"type": "string"},
                            },
                            "required": ["code"],
                        },
                    },
                },
                "required": ["code", "request_id"],
            }
        },
        "required": ["error"],
    }
    return result


def restore_language(result: Any, **kwargs: Any) -> Any:
    # There is no LocaleMiddleware here, so an activation would otherwise stick
    # to the thread and outlive the request that served /schema/.
    if _previous:
        translation.activate(_previous.pop() or "tr")
    return result
