"""Every company starts with its five roles.

A receiver rather than a call in the registration service: companies are made
by registration, the demo command and tests alike, and every one of them needs
roles before its first user can be given one.
"""

from __future__ import annotations

from typing import Any

from django.db.models.signals import post_save
from django.dispatch import receiver

from apps.companies.models import Company
from apps.users.roles import seed_company_roles


@receiver(post_save, sender=Company, dispatch_uid="users.seed_company_roles")
def seed_roles_for_new_company(
    sender: Any, instance: Company, created: bool, raw: bool = False, **kwargs: Any
) -> None:
    # A fixture brings its own roles, if it wants them.
    if created and not raw:
        seed_company_roles(instance)
