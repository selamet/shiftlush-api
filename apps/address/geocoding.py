"""Reverse geocoding, from this API rather than from the browser.

The address picker could call Nominatim directly. It does not, for the three
reasons the specification gives (8.6): answers can be cached here instead of
being re-fetched by every visitor, the provider's quota is ours to control
rather than the public's to spend, and replacing the provider is a change to
this file instead of a change to the frontend.

Nothing the provider sends reaches a client. Its JSON is reduced to `Place` —
candidate names for the three administrative levels we actually store — and
`apps.address.matching` turns that into ids from our own tables. Forwarding the
raw response would be the shorter route and would end the abstraction on the
spot: a client that can read `osm_id` will read it, and then the provider is no
longer replaceable.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.cache import cache
from django.utils.module_loading import import_string

logger = logging.getLogger(__name__)

#: Decimal places kept in the cache key. Four is about eleven metres, which is
#: below the size of anything this endpoint can resolve — so a pin nudged by a
#: few pixels is answered from the cache instead of costing another call.
COORDINATE_PRECISION = 4

#: Enough for one address. The provider is not hostile, but it is not ours
#: either, and an unbounded read is an unbounded read.
MAX_RESPONSE_BYTES = 64 * 1024

#: Building level. `addressdetails` returns the whole hierarchy at any zoom;
#: this decides how precisely the point itself is resolved, and anything
#: coarser starts naming the district when the pin is on a house.
REVERSE_ZOOM = 18

#: OSM does not file Turkish administrative levels under one consistent key.
#: Every one of these has been seen carrying a district, so all of them are
#: collected and scored rather than read in a fixed order — a guess at the
#: order silently drops the level for whichever regions disagree with it.
PROVINCE_KEYS = ("province", "state", "city")
DISTRICT_KEYS = ("town", "city_district", "county", "district", "municipality", "suburb")
NEIGHBORHOOD_KEYS = ("neighbourhood", "quarter", "suburb", "village", "hamlet", "residential")


class GeocoderUnavailable(Exception):
    """The provider did not answer, or answered with something unusable.

    Distinct from "there is nothing at this point", which is an ordinary answer
    and comes back as `None`. This one means the lookup could not be performed
    and is worth retrying later.
    """


@dataclass(frozen=True)
class Place:
    """One point, as the names a provider offers for it.

    A tuple per level rather than one string each: see `DISTRICT_KEYS`. The
    caller scores every candidate and keeps the best.
    """

    country_code: str
    provinces: tuple[str, ...] = ()
    districts: tuple[str, ...] = ()
    neighborhoods: tuple[str, ...] = ()


class ReverseGeocoder(Protocol):
    """What a provider has to offer to be swappable.

    Returning `None` means the provider answered and there is nothing there —
    a pin dropped in the Aegean. Raising `GeocoderUnavailable` means it did not
    answer. The two lead to different HTTP statuses, so they are different
    outcomes here rather than one empty result.
    """

    def reverse(self, latitude: float, longitude: float) -> Place | None: ...


class NominatimGeocoder:
    """OpenStreetMap's public reverse geocoder.

    Its usage policy asks for an identifying `User-Agent` with a way to make
    contact, and refuses requests that arrive without one — so the header is
    configuration rather than a constant, and a deployment that fills it in
    with something meaningless is breaking the terms it runs under, not a rule
    of ours. The policy also caps traffic at one request a second, which is why
    the cache in this module and the throttle on the view are both part of
    using this provider at all rather than optimisations.
    """

    def reverse(self, latitude: float, longitude: float) -> Place | None:
        payload = self._get(
            {
                "format": "jsonv2",
                "lat": f"{latitude:.7f}",
                "lon": f"{longitude:.7f}",
                "zoom": REVERSE_ZOOM,
                "addressdetails": 1,
                # The address table stores Turkish names. Asking for anything
                # else guarantees every comparison downstream fails.
                "accept-language": "tr",
            }
        )

        # How Nominatim says "nothing here". Not an outage: the pin is in the
        # sea, and the honest answer is an empty form.
        if "error" in payload or "address" not in payload:
            return None

        address = payload["address"]
        if not isinstance(address, dict):
            raise GeocoderUnavailable("address was not an object")

        return Place(
            country_code=str(address.get("country_code", "")),
            provinces=_candidates(address, PROVINCE_KEYS),
            districts=_candidates(address, DISTRICT_KEYS),
            neighborhoods=_candidates(address, NEIGHBORHOOD_KEYS),
        )

    def _get(self, query: dict[str, Any]) -> dict[str, Any]:
        base = settings.GEOCODING_URL
        if not base.startswith("https://"):
            # A provider reached over plain HTTP would let anything on the path
            # rewrite an address the user is about to save.
            raise GeocoderUnavailable("the configured provider URL is not https")

        request = Request(  # noqa: S310 - the scheme is checked one line above
            f"{base}?{urlencode(query)}",
            headers={"User-Agent": settings.GEOCODING_USER_AGENT, "Accept": "application/json"},
        )

        try:
            # Without this the socket waits forever and takes a worker with it:
            # one slow provider becomes one hung request, and enough of them
            # become an API that answers nothing at all.
            with urlopen(request, timeout=settings.GEOCODING_TIMEOUT_SECONDS) as response:  # noqa: S310
                body = response.read(MAX_RESPONSE_BYTES)
        except (URLError, TimeoutError, OSError) as exc:
            # TimeoutError arrives bare from the socket and wrapped in URLError
            # from the opener, depending on where the clock runs out.
            raise GeocoderUnavailable(str(exc)) from exc

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            # A 200 carrying an HTML error page is how a rate limit or a proxy
            # in the way usually presents itself.
            raise GeocoderUnavailable("provider did not return JSON") from exc

        if not isinstance(payload, dict):
            raise GeocoderUnavailable("provider returned an unexpected shape")
        return payload


def _candidates(address: dict[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    """Every distinct non-empty value among `keys`, in the order given."""
    seen: dict[str, None] = {}
    for key in keys:
        value = address.get(key)
        if isinstance(value, str) and value.strip():
            seen.setdefault(value.strip(), None)
    return tuple(seen)


def get_geocoder() -> ReverseGeocoder:
    """The configured provider.

    Built per call rather than held as a module singleton: it carries no
    connection and no state, and a cached instance would ignore a settings
    override, which is exactly the thing a test wants to do.
    """
    geocoder: ReverseGeocoder = import_string(settings.GEOCODING_PROVIDER)()
    return geocoder


def reverse_geocode(latitude: float, longitude: float) -> Place | None:
    """Look a point up, through the cache.

    "Nothing there" is cached alongside real answers. It has to be: a pin
    dragged across the coastline produces a run of empty results, and leaving
    those uncached would spend the provider's quota fastest in exactly the case
    where it buys nothing. Failures are not cached — they are the one outcome
    that a retry can legitimately change.
    """
    key = (
        f"geocode:reverse:{latitude:.{COORDINATE_PRECISION}f}:{longitude:.{COORDINATE_PRECISION}f}"
    )

    cached = _cache_get(key)
    if cached is not None:
        return _from_cache(cached)

    place = get_geocoder().reverse(latitude, longitude)
    _cache_set(key, _to_cache(place))
    return place


def _to_cache(place: Place | None) -> dict[str, Any]:
    # Wrapped rather than stored bare, so a cached "nothing here" is not
    # indistinguishable from a cache miss.
    if place is None:
        return {"place": None}
    return {
        "place": {
            "country_code": place.country_code,
            "provinces": list(place.provinces),
            "districts": list(place.districts),
            "neighborhoods": list(place.neighborhoods),
        }
    }


def _from_cache(entry: dict[str, Any]) -> Place | None:
    stored = entry.get("place")
    if not stored:
        return None
    return Place(
        country_code=stored["country_code"],
        provinces=tuple(stored["provinces"]),
        districts=tuple(stored["districts"]),
        neighborhoods=tuple(stored["neighborhoods"]),
    )


def _cache_get(key: str) -> dict[str, Any] | None:
    # A cache that is down should cost us a provider call, not the request. The
    # failure is logged rather than swallowed: silently paying full price for
    # every lookup is the kind of outage that only shows up on the invoice.
    try:
        entry: dict[str, Any] | None = cache.get(key)
    except Exception:
        logger.warning("Geocoding cache unreadable", exc_info=True)
        return None
    return entry


def _cache_set(key: str, entry: dict[str, Any]) -> None:
    try:
        cache.set(key, entry, timeout=settings.GEOCODING_CACHE_TTL_SECONDS)
    except Exception:
        logger.warning("Geocoding cache unwritable", exc_info=True)
