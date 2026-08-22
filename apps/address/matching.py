"""Turning a provider's place names into rows we own.

The specification states the rule this module exists to obey (9.4):

    Nominatim's neighbourhood name may not match the one in your database
    exactly. Do fuzzy matching (trigram similarity > 0.4); if there is no match,
    leave the fields empty and let the user choose. Auto-filling the WRONG
    neighbourhood is worse than leaving it blank.

Everything below follows from the second sentence. A wrong neighbourhood is
saved, printed on a QR label, and dispatched to — and nobody re-checks a field
that filled itself in. An empty one is a dropdown the user was going to touch
anyway. So this module refuses in three places where a looser one would guess:
below the threshold, on a tie between two equally good rows, and for any level
whose parent did not match.

Trigram similarity is `pg_trgm`'s, computed here rather than by the database.
That is deliberate. The alternative — `TrigramSimilarity` in an annotation —
works only on PostgreSQL, needs an extension this schema does not install yet,
and would leave every local run exercising a different code path from the one
CI and production exercise. Since the cascade has already narrowed the
candidates to one province's districts or one district's neighbourhoods, there
is no index to gain from and nothing to pay for the honesty. `test_geocode.py`
checks the implementation against the real `similarity()` when the suite runs
on PostgreSQL with the extension available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.conf import settings
from django.db.models import QuerySet

from apps.address.models import District, Neighborhood, Province
from core.text import normalize

if TYPE_CHECKING:
    from apps.address.geocoding import Place

#: `pg_trgm` splits on anything that is not alphanumeric, so this does too.
#: Underscore is excluded, which `\w` would not do.
_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Words that name what kind of settlement it is rather than which settlement.
#: The address table abbreviates ("Barbaros Mah."), Nominatim spells it out
#: ("Barbaros Mahallesi"), and one is not a different place from the other.
#: Dropping them before comparing turns a fuzzy match into an exact one, which
#: is worth more than it looks: it is the difference between a confidence of
#: 1.00 and one close enough to the threshold to be luck.
#:
#: Held to the forms that only ever appear as a suffix. `merkez` is a real name
#: in dozens of districts and is not on this list; neither is a bare `il` or
#: `ilce`, which could plausibly be part of one.
GENERIC_WORDS = frozenset(
    {
        "mah",
        "mahalle",
        "mahallesi",
        "koy",
        "koyu",
        "belde",
        "beldesi",
        "mezra",
        "mezrasi",
        "ili",
        "ilcesi",
        "belediye",
        "belediyesi",
        "buyuksehir",
    }
)


@dataclass(frozen=True)
class Match:
    """One administrative record the provider's name was resolved to."""

    id: int
    name: str
    #: 1.0 means the two names are the same once folded and stripped of the
    #: words above — not that the strings were identical.
    confidence: float


@dataclass(frozen=True)
class AddressMatch:
    """What the endpoint answers with: ids, or an explicit nothing."""

    province: Match | None = None
    district: Match | None = None
    neighborhood: Match | None = None

    @property
    def unmatched(self) -> list[str]:
        """The levels the client has to ask the user about.

        Redundant with the three nulls above, and deliberately so. A client
        that reads a null as "not filled in yet" behaves differently from one
        that reads it as "we looked and found nothing", and only one of those
        prompts the user. Naming the levels leaves no room for the first
        reading.
        """
        return [
            level
            for level, match in (
                ("province", self.province),
                ("district", self.district),
                ("neighborhood", self.neighborhood),
            )
            if match is None
        ]


def trigrams(text: str) -> frozenset[str]:
    """The trigram set `pg_trgm` would produce.

    Each word is padded with two spaces in front and one behind before being
    cut into threes, which is what makes short words comparable at all and why
    a word of length n yields n + 1 trigrams rather than n - 2.
    """
    grams: set[str] = set()
    for word in _WORD.findall(text.lower()):
        padded = f"  {word} "
        grams.update(padded[index : index + 3] for index in range(len(padded) - 2))
    return frozenset(grams)


def similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """`pg_trgm`'s measure: shared trigrams over the union of both sets."""
    if not left or not right:
        return 0.0
    shared = len(left & right)
    return shared / (len(left) + len(right) - shared)


def trigram_similarity(left: str, right: str) -> float:
    """`similarity(left, right)` as PostgreSQL computes it."""
    return similarity(trigrams(left), trigrams(right))


def comparison_key(name: str) -> str:
    """Fold a place name to the form two spellings of it agree on."""
    words = [word for word in _WORD.findall(normalize(name)) if word not in GENERIC_WORDS]
    # A name made of nothing but generic words is a name we have no better
    # reading of; comparing it whole beats comparing it to an empty string.
    return " ".join(words) or normalize(name)


def match_place(place: Place | None) -> AddressMatch:
    """Resolve a provider's place to our own ids, one level at a time.

    The cascade is strict: a level whose parent did not match is not looked up
    at all. District names repeat across the country — dozens of provinces have
    a Merkez — so a district matched without knowing its province is a coin
    flip dressed up as an answer. It is also what the client needs, since the
    form cannot select a district before a province either.
    """
    if place is None:
        return AddressMatch()

    # Turkey is the whole of the address table. A pin on the Bulgarian side of
    # the border has no row that could be right, so the levels are reported
    # unmatched without troubling the database.
    if place.country_code.lower() != "tr":
        return AddressMatch()

    province = _best(Province.objects.all(), place.provinces)
    if province is None:
        return AddressMatch()

    district = _best(District.objects.filter(province_id=province.id), place.districts)
    if district is None:
        return AddressMatch(province=province)

    neighborhood = _best(Neighborhood.objects.filter(district_id=district.id), place.neighborhoods)
    return AddressMatch(province=province, district=district, neighborhood=neighborhood)


def _best(
    rows: QuerySet[Province] | QuerySet[District] | QuerySet[Neighborhood],
    candidates: tuple[str, ...],
) -> Match | None:
    """The one row a candidate name clearly means, or nothing.

    "Clearly" is doing the work. Two rows scoring the same is not a near miss
    to be broken by row order — it is the case where filling the field in is
    most likely to be wrong and least likely to be noticed, because the answer
    looks as confident as any other.
    """
    keys = [trigrams(comparison_key(name)) for name in candidates if name.strip()]
    if not keys:
        return None

    scored = [
        (max(similarity(trigrams(comparison_key(name)), key) for key in keys), row_id, name)
        for row_id, name in rows.values_list("id", "name")
    ]
    if not scored:
        return None

    # By score, then by id: the order has to be settled by something, and an
    # id is at least stable between runs and between engines.
    scored.sort(key=lambda row: (-row[0], row[1]))

    score, row_id, name = scored[0]
    if score < settings.GEOCODING_MATCH_THRESHOLD:
        return None
    if len(scored) > 1 and scored[1][0] == score:
        return None

    return Match(id=row_id, name=name, confidence=round(score, 2))
