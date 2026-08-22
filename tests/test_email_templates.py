"""What the e-mails actually contain.

Nothing tested the rendered output before this. The invitation tests asserted
that a message was sent and that its body carried a working link, both of which
were true while every message also carried a paragraph of template comment
printed above the greeting — because Django's `{# #}` comment is single-line
only, and a multi-line one is not a comment at all.

These are the three messages a person outside the company receives. They are the
first thing anyone sees of the product, and they are the only part of the
backend that speaks Turkish to a human.
"""

from __future__ import annotations

import re

import pytest
from django.template.loader import render_to_string

TEMPLATES = ["invitation", "password_reset", "email_verification"]

CONTEXT = {
    "first_name": "Nur",
    "company_name": "Yükseliş Asansör",
    "url": "https://shiftlush.selamet.dev/invitation/abc123",
    "valid_for_hours": 72,
}

#: Anything that looks like unrendered template syntax reaching the reader.
LEAKED_SYNTAX = re.compile(r"\{[#{%]")


@pytest.mark.parametrize("template", TEMPLATES)
@pytest.mark.parametrize("part", ["subject.txt", "body.txt", "body.html"])
def test_no_template_syntax_reaches_the_reader(template: str, part: str) -> None:
    rendered = render_to_string(f"email/{template}/{part}", CONTEXT)

    found = LEAKED_SYNTAX.search(rendered)
    assert found is None, (
        f"email/{template}/{part} renders template syntax at "
        f"{rendered[max(0, found.start() - 40) : found.start() + 60]!r}"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_html_starts_as_a_document(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.html", CONTEXT).lstrip()
    # Anything before the doctype is text the client renders above the message.
    assert rendered.lower().startswith("<!doctype html>")


@pytest.mark.parametrize("template", TEMPLATES)
def test_both_parts_carry_the_link(template: str) -> None:
    for part in ("body.txt", "body.html"):
        rendered = render_to_string(f"email/{template}/{part}", CONTEXT)
        # The plain-text part is not decoration: a client that refuses HTML
        # shows it instead, and a link only in the HTML half is a dead end.
        assert CONTEXT["url"] in rendered, part


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_subject_is_one_line(template: str) -> None:
    subject = render_to_string(f"email/{template}/subject.txt", CONTEXT)
    # A newline in a subject header is either dropped or, historically, a way to
    # inject further headers.
    assert "\n" not in subject.strip()
    assert subject.strip()


def test_a_name_with_markup_in_it_is_escaped() -> None:
    rendered = render_to_string(
        "email/invitation/body.html", {**CONTEXT, "first_name": "<script>alert(1)</script>"}
    )
    assert "<script>" not in rendered
