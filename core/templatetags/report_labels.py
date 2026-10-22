"""Turkish labels for the values a report prints, kept in core/data/."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from django import template

register = template.Library()

_LABELS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).resolve().parent.parent / "data" / "report_labels.json").read_text("utf-8")
)


@register.filter
def label(value: object, group: str) -> str:
    """The label of `value` in `group`; an unknown value is printed as it came."""
    return _LABELS.get(group, {}).get(str(value), str(value))


@register.filter
def image(images: Mapping[str, str], attachment_id: object) -> str:
    """The `data:` URI for an attachment id, or "" when there is none."""
    return images.get(str(attachment_id), "") if attachment_id else ""
