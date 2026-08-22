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


#: The hidden block the inbox prints beside the subject line.
PREHEADER = re.compile(r'<div style="display:none;[^"]*">(.*?)</div>', re.S)

#: Everything after the preview sentence is padding that stops the client
#: pulling the greeting into the preview line as well.
PREHEADER_PADDING = re.compile(r"(&#847;|&zwnj;|&nbsp;|\s)+")

#: What is addressed to Outlook alone. There is more than one such block --
#: the head carries a font stack for it too -- so the button has to be found.
MSO_BLOCK = re.compile(r"<!--\[if mso\]>(.*?)<!\[endif\]-->", re.S)

#: Every colour in `shiftlush-web/src/styles/globals.css` that the mail uses.
#: A colour outside this set is a colour the product does not have.
DESIGN_SYSTEM_COLOURS = {
    "#5a3bd4",  # --primary
    "#4a2cbe",  # --sl-primary-hover
    "#f4f5f7",  # --background
    "#ffffff",  # --card
    "#16171c",  # --foreground
    "#5a5f6b",  # --muted-foreground
    "#6e7381",  # --sl-subtle
    "#e4e6eb",  # --sl-border-subtle
    "#fdf2dc",  # --sl-warning-bg
    "#7a4a00",  # --sl-warning-fg
    "#af7d00",  # --sl-label-yellow
}

HEX_COLOUR = re.compile(r"#[0-9a-fA-F]{6}\b")


@pytest.mark.parametrize("template", TEMPLATES)
def test_every_message_sets_its_preview_text(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.html", CONTEXT)

    block = PREHEADER.search(rendered)
    assert block is not None, "the hidden preview block is gone"

    sentence = PREHEADER_PADDING.sub(" ", block.group(1)).strip()
    # Left empty, the client takes the greeting instead, and every message
    # previews as "Merhaba Nur," — indistinguishable in a list of three.
    assert sentence, f"email/{template}/body.html previews as nothing"
    assert block.start() < rendered.index("<h1"), "the preview text follows visible content"


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_preview_text_says_something_the_subject_does_not(template: str) -> None:
    subject = render_to_string(f"email/{template}/subject.txt", CONTEXT).strip()
    block = PREHEADER.search(render_to_string(f"email/{template}/body.html", CONTEXT))
    assert block is not None
    sentence = PREHEADER_PADDING.sub(" ", block.group(1)).strip()

    # The two are printed side by side. Repeating the subject spends the only
    # line of context the reader gets before opening anything.
    assert sentence != subject


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_address_is_readable_and_not_only_a_target(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.html", CONTEXT)

    # A reader whose client stops the button, or who is forwarding the message
    # on as text, needs the address itself and not just something to press.
    assert f">{CONTEXT['url']}<" in rendered


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_button_survives_outlook(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.html", CONTEXT)

    # Word's rendering engine drops the padding and the radius off an anchor,
    # so Outlook is drawn a VML rectangle instead. Both halves carry the link:
    # losing the one Outlook reads leaves that client with no button at all.
    blocks = MSO_BLOCK.findall(rendered)
    assert blocks, "nothing is addressed to Outlook any more"

    drawn = [block for block in blocks if "v:roundrect" in block]
    assert len(drawn) == 1, "Outlook is drawn no button, or more than one"
    assert CONTEXT["url"] in drawn[0]


@pytest.mark.parametrize("template", TEMPLATES)
def test_no_colour_outside_the_design_system(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.html", CONTEXT)

    found = {colour.lower() for colour in HEX_COLOUR.findall(rendered)}
    assert found <= DESIGN_SYSTEM_COLOURS, (
        f"email/{template}/body.html uses {sorted(found - DESIGN_SYSTEM_COLOURS)}, "
        f"which is not in shiftlush-web/src/styles/globals.css"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_plain_text_part_names_the_product(template: str) -> None:
    rendered = render_to_string(f"email/{template}/body.txt", CONTEXT)

    # The HTML half says ShiftLush in the band and the footer. A client showing
    # the text half would otherwise get an unsigned message about a password.
    assert "ShiftLush" in rendered
