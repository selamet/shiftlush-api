"""URL layout.

The version lives in the path — `/api/v1/...` — rather than in a header or a
query parameter. Header versioning reads cleaner on paper and is worse in
practice: the log line does not say which version was called, you cannot try it
from a browser, CDN caching gets confusing, and pinning a mobile client becomes
awkward. The path solves all four.

`/health` and `/ready` sit outside the version prefix on purpose: they are
infrastructure, not part of the contract.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularRedocView

from core.views import health, not_found, ready, server_error

# Django answers an unresolvable URL itself, before DRF is involved, so these
# are the only way those two responses become JSON like every other error.
handler404 = not_found
handler500 = server_error

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health", health, name="health"),
    path("ready", ready, name="ready"),
    path(
        "api/v1/",
        include(
            [
                path("auth/", include("apps.users.api.v1.auth_urls")),
                path("", include("apps.companies.api.v1.urls")),
                path("", include("apps.users.api.v1.urls")),
                path("", include("apps.address.api.v1.urls")),
                path("", include("apps.customers.api.v1.urls")),
                path("", include("apps.properties.api.v1.urls")),
                path("", include("apps.elevators.api.v1.urls")),
                path("", include("apps.contracts.api.v1.urls")),
                path("", include("apps.attachments.api.v1.urls")),
                path("", include("apps.audit.api.v1.urls")),
            ]
        ),
    ),
]

# Both are development-only, and for `/schema/` that is a deliberate departure
# from the 8.6 inventory rather than `/docs/` dragging it along. The deviations
# table carries the argument; it is not the one 8.13 makes.
#
# 8.13 closes `/docs/` because the schema exposes every field name and business
# rule. That reason does not survive contact with this repository: it is public,
# and `openapi/v1.yaml` on main is the same document sitting at a URL anyone can
# fetch. Nothing is kept secret by refusing to serve it here.
#
# What is left is that a production `/schema/` would have no reader and two
# costs. No reader, because the frontend stopped asking a running backend for
# the contract — `npm run api:sync` pulls the published file, which is what lets
# 14.1's "the frontend build must not depend on a running backend" hold. The
# costs: it is a second copy of a document CI already gates on, free to disagree
# with the committed one the moment a deploy lags behind main, and
# SpectacularAPIView builds it by walking every serializer on each request — an
# unauthenticated endpoint doing real work, on a three-worker container sharing
# a box with four other applications, behind a 20/min/IP limit sized for cheap
# requests.
#
# Serving it would mean caching it and restricting it, to arrive at a slower
# copy of a static file that already exists. Development keeps both: that is
# where the generator and a browser actually want them.
if settings.DEBUG:
    urlpatterns += [
        path("schema/", SpectacularAPIView.as_view(), name="schema"),
        path("docs/", SpectacularRedocView.as_view(url_name="schema"), name="docs"),
    ]
